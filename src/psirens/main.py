"""PSIRENS server: the createApp factory and the ASGI `app`.

Runtime contract (App Store): reads PORT (default 8080), binds 0.0.0.0 via the
gunicorn CMD, returns 200 unauthenticated at `/` and `/healthz`, runs non-root.
The operator Environment Variables tab must stay EMPTY for a code-defaults run.

Handlers are module-level and read their dependencies from `request.app.state`,
which keeps the create_app factory small and each unit independently testable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .agreement import (MAX_RESIDUAL_DEG, MODE_ORDER, Resolver, drift_withheld,
                        policy_from, resolve_data)
from .astro import drift_fit
from .config import Config, load_config
from .models import VIEW_MODES, DataMode, ManualElsetIn
from .conjunction import conjunctions_for
from .coplanar import coplanar_for
from .events import EventEngine, Thresholds, latest_splits
from .logjson import RequestLog, configure_logging
from .tle import parse_priority
from .hrr import HrrStore
from .refresh import Refresher
from .security import RateLimiter, SingleFlight, token_ok
from .sources import DemoElsetSource, ManualElsetSource, UDLElsetSource
from .store import Store, sample_provenance

# The platform logging standard (1.7.1): one JSON object per line on stdout.
# basicConfig used to install a plain-text handler on STDERR, which is what the
# Logging check reported. See logjson.py.
configure_logging()
_log = logging.getLogger("psirens")

_STATIC = os.path.join(os.path.dirname(__file__), "static")
_PNG = "image/png"
_JSON = "application/json"
_RATE_LIMIT = "rate limit"
_NOT_FOUND = "not found"

# Served assets, whitelisted. Paths are precomputed from LITERAL filenames at
# import, so a request never constructs a filesystem path from user input; the
# request only supplies a dictionary key.
_ASSET_MEDIA: dict[str, str] = {
    "icon-512.png": _PNG,
    "icon-192.png": _PNG,
    "favicon-32.png": _PNG,
    "apple-touch-icon.png": _PNG,
    "psirens-banner.png": _PNG,
    "manifest.webmanifest": "application/manifest+json",
    "hrr-geo.json": _JSON,  # GEO subset of the JCO HRR list
    # The Operator Guide and its figures. Every figure is a screenshot of this
    # interface, captured by tools/guide_shots.sh; recapture them when the
    # layout changes rather than redrawing them by hand.
    "guide.html": "text/html; charset=utf-8",
    "guide-overview.png": _PNG,
    "guide-top.png": _PNG,
    "guide-bar.png": _PNG,
    "guide-rail.png": _PNG,
    "guide-plot.png": _PNG,
    "guide-legend.png": _PNG,
    "guide-inspector.png": _PNG,
    "guide-coplanar.png": _PNG,
    "guide-country.png": _PNG,
    "guide-stale.png": _PNG,
    "guide-brief.png": _PNG,   # the Shift Brief card (1.7.0)
    "guide-focus.png": _PNG,   # focus mode on an E-W manoeuvre (1.7.0)
}
_ASSET_PATHS: dict[str, str] = {name: os.path.join(_STATIC, name) for name in _ASSET_MEDIA}

# Classification ranking so the banner shows the most restrictive marking present.
_RANK = {"U": 0, "UNCLASSIFIED": 0, "CUI": 1, "C": 2, "S": 3, "TS": 4}


def _banner(markings: list[str], default: str = "UNCLASSIFIED") -> str:
    best, best_rank = default, -1
    for m in markings:
        r = _RANK.get(m.upper().split("//")[0], 0)
        if r > best_rank:
            best_rank, best = r, m
    # Show a readable word for the unclassified case rather than a bare "U".
    return "UNCLASSIFIED" if best_rank <= 0 else best


def _rank_of(oid: str, hrr_map: dict) -> int:
    """The object's priority rank, defaulting to the lowest.

    An object absent from the high-interest list, or present without a usable
    rank, is BACKGROUND (5). That matches what the interface has always shown
    and keeps the server and the client agreeing on one number.
    """
    entry = hrr_map.get(oid) or {}
    rank = entry.get("rank")
    return 5 if rank is None else int(rank)


def _empty_drift(stamp: str | None, reason: str) -> dict:
    return {"drift_deg_per_day": None, "drift_residual_deg": None,
            "drift_fit_hours": None, "drift_fit_points": None,
            "drift_fit_harmonic": None, "drift_since_event": stamp,
            "drift_withheld_reason": reason}


def _drift_block(pairs: list[tuple[datetime, float]],
                 since: datetime | None = None,
                 ceiling: float = MAX_RESIDUAL_DEG,
                 withheld: str | None = None) -> dict:
    """The fitted drift rate with the context needed to read it.

    A rate on its own is not interpretable, which is how a 30-minute finite
    difference reading -3.82 deg/day sat on an inspector panel beside a total
    track excursion of 5.9 deg without contradicting itself out loud. The
    residual and the baseline travel with the number.

    NEVER ACROSS A MANOEUVRE (1.7.0). `since` is the newest CONFIRMED E-W
    event for the object, and only fixes after it are fitted. A trailing 72 h
    fit that straddles a burn averages "on station" with "drifting" and
    reports neither: 0.22 deg/day for an object drifting at 0.59 deg/day,
    measured on the scenario store while prototyping the Shift Brief.
    `drift_since_event` says when the fit was cut, so the reader knows.

    NEVER WHEN THE FIT IS POOR (1.7.5). A fit whose RMS residual exceeds
    `ceiling` does not describe the data it was fitted to, and the rate it
    returns is an artefact of the fit. COSMOS 2589 published -12.96 deg/day
    at 19.2 deg rms. The rate is withheld, the residual and baseline are kept
    so the reader can see why, and `drift_withheld_reason` says so. Every
    withheld rate carries a reason; a null rate never stands unexplained.
    """
    if since is not None:
        pairs = [p for p in pairs if p[0] >= since]
    stamp = since.isoformat() if since is not None else None
    if withheld:
        return _empty_drift(stamp, withheld)
    fit = drift_fit(pairs)
    if fit is None:
        return _empty_drift(stamp, "no usable baseline: under 24 h of fixes from "
                                   "the track provider, or an impossible rate")
    poor = fit.residual_deg > ceiling
    return {
        "drift_since_event": stamp,
        "drift_deg_per_day": None if poor else fit.rate_deg_per_day,
        "drift_residual_deg": fit.residual_deg,
        "drift_fit_hours": fit.span_hours,
        "drift_fit_points": fit.points,
        "drift_fit_harmonic": fit.harmonic,
        "drift_withheld_reason": (f"the fit misses its data by {fit.residual_deg:.2f} deg rms, "
                                  f"over the {ceiling:g} deg ceiling") if poor else None,
    }


# The element-set consensus and the observed track disagree when their rates
# differ by more than this, deg/day: the "on station" readout's own threshold.
CROSSCHECK_DEG_PER_DAY = 0.05


def _drift_payload(observed: dict, cons: dict | None) -> dict:
    """The published drift rate (1.7.8): the CONSENSUS of the agreeing
    providers' own element sets whenever there is one, and the observed track
    fit beside it as a cross-check, never in place of it.

    Owner, 17 November 2026: "now we have sorted the provider states, we need
    to come to a consensus on the actual drift rate, I cannot have nothing".
    The observed fit needs 24 h of ONE provider's tagged fixes, so after 1.7.5
    every object read "no usable baseline" until that history built up. The
    element sets need no history: each states its own mean motion, and
    `agreement.elset_rate` turns it into a rate by SGP4 over one orbit.

    `drift_residual_deg`, `drift_fit_hours`, `drift_fit_points` and
    `drift_fit_harmonic` keep describing the observed fit, and are null when
    there is none; `drift_basis` says which one `drift_deg_per_day` is.
    """
    out = {**observed,
           "drift_observed_deg_per_day": observed["drift_deg_per_day"],
           "drift_observed_withheld_reason": observed["drift_withheld_reason"],
           "drift_consensus": cons, "drift_basis": None, "drift_crosscheck": None}
    obs = observed["drift_deg_per_day"]
    if cons is None:
        out["drift_basis"] = "observed_fit" if obs is not None else None
        return out
    rate = cons["rate_deg_per_day"]
    out.update(drift_deg_per_day=rate, drift_basis="element_sets", drift_withheld_reason=None)
    if obs is not None:
        diff = obs - rate
        out["drift_crosscheck"] = {"difference_deg_per_day": round(diff, 4),
                                   "agrees": abs(diff) <= CROSSCHECK_DEG_PER_DAY}
    return out


def _stream_for(rec: dict, modes: set[DataMode]) -> tuple[str | None, list[dict]]:
    """The one stream this view shows: REAL before any other mode, and within
    a mode only the provider the agreement check chose (1.7.5)."""
    streams = rec.get("mode_streams") or {}
    for mode in MODE_ORDER:
        if streams.get(mode) and DataMode(mode) in modes:
            return mode, streams[mode]
    return None, []


def _markings(rec: dict, samples: list[dict]) -> list[str]:
    legacy = rec.get("classification_marking")
    marks = set()
    for s in samples:
        prov = sample_provenance(rec, s)
        mark = prov["classification_marking"] if prov else legacy
        if mark:
            marks.add(mark)
    return sorted(marks)


def _track_entry(oid: str, rec: dict, modes: set[DataMode], since: datetime | None,
                 ceiling: float = MAX_RESIDUAL_DEG) -> dict | None:
    """One track, from ONE provider's samples (1.7.5). Every provider seen is
    listed with its status and its own fit; none is blended into the track."""
    mode, samples = _stream_for(rec, modes)
    if not samples or mode is None:
        return None
    block = (rec.get("agreement") or {}).get(mode) or {}
    marks = _markings(rec, samples)
    primary = block.get("primary")
    pairs = [(datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in samples]
    el = (rec.get("mode_elsets") or {}).get(mode) or {}
    return {
        "object_id": oid,
        "name": rec.get("name", oid),
        "data_mode": mode,
        "classification_marking": " | ".join(marks) if marks else None,
        "classification_markings": marks,
        "source": primary or "untagged",
        "providers": block.get("providers") or {},
        "head_source": primary,
        "agreement": {k: v for k, v in block.items() if k != "providers"},
        "provenance": rec.get("provenance") or [],
        "origin": rec.get("origin", ""),
        "target": rec.get("target"),
        "samples": samples,
        **_drift_payload(_drift_block(pairs, since, ceiling, drift_withheld(block)),
                         block.get("consensus")),
        "ra_deg": el.get("raan_deg"),
    }


def _tracks_payload(cfg: Config, store: Store | Resolver, view: str,
                    modes: set[DataMode],
                    ranks: set[int] | None = None,
                    hrr_map: dict | None = None,
                    splits: dict[str, datetime] | None = None) -> dict:
    """Build the tracks payload, optionally limited to a set of priority ranks.

    The rank filter exists to keep first paint small: ranks 4 and 5 are the
    bulk of the list (444 of 594 objects in the bundled snapshot) and are not
    what an operator opens the plot to look at. They are fetched on demand.

    No filter is applied when `ranks` is None, so any other consumer of this
    API sees the whole picture.

    There is deliberately NO escape hatch for an empty high-interest list. The
    first version of this had one, and it was dead logic: the list seeds from a
    bundled snapshot of 594 objects and is never empty, so the branch could not
    fire and was never exercised. An object absent from the list is BACKGROUND,
    including every object in the offline demo population, and a default view
    of ranks 1-3 therefore shows nothing offline. The interface says so rather
    than the server quietly widening the request.
    """
    data = store.load()
    if not data.get("resolved"):
        data = resolve_data(data, policy_from(cfg))
    hrr_map = hrr_map or {}
    splits = splits or {}
    active_ranks = ranks or None
    tracks, markings = [], []
    for oid, rec in data.get("objects", {}).items():
        if active_ranks is not None and _rank_of(oid, hrr_map) not in active_ranks:
            continue
        track = _track_entry(oid, rec, modes, splits.get(oid),
                             cfg.drift_max_residual_deg)
        if track is not None:
            tracks.append(track)
            markings.extend(track["classification_markings"])
    return {
        "view": view,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification_banner": _banner(markings),
        "bounds": {
            "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
            "inc_min": cfg.inc_min, "inc_max": cfg.inc_max,
        },
        "count": len(tracks),
        "ranks": sorted(active_ranks) if active_ranks else None,
        "stale_after_hours": cfg.stale_after_hours,
        "tracks": tracks,
    }


# --------------------------------------------------------------------------
# Cross-cutting helpers and dependencies (module-level; read app.state)
# --------------------------------------------------------------------------
def _err(status: int, desc: str) -> dict:
    """One OpenAPI error-response entry; keeps the 'description' literal single."""
    return {status: {"description": desc}}


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "anon"


def _cors(resp: Response, cfg: Config) -> Response:
    if cfg.allowed_origin and cfg.allowed_origin != "*":
        resp.headers["Access-Control-Allow-Origin"] = cfg.allowed_origin
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _global_gate(request: Request) -> None:
    if not request.app.state.global_rl.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT)


def _require_token(request: Request,
                   authorization: Annotated[str | None, Header()] = None) -> None:
    cfg: Config = request.app.state.cfg
    if not cfg.team_token:
        return  # single-user local mode, auth off
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not token_ok(given, cfg.team_token):
        raise HTTPException(status_code=401, detail="unauthorized")


# --------------------------------------------------------------------------
# Route handlers
# --------------------------------------------------------------------------
def _index(request: Request) -> HTMLResponse:
    with open(os.path.join(_STATIC, "index.html"), encoding="utf-8") as fh:
        return _cors(HTMLResponse(fh.read()), request.app.state.cfg)


def _favicon(request: Request) -> FileResponse:
    resp = FileResponse(_ASSET_PATHS["favicon-32.png"], media_type=_PNG)
    return _cors(resp, request.app.state.cfg)


def _static_asset(request: Request, name: str) -> Response:
    path = _ASSET_PATHS.get(name)  # value is a precomputed, trusted constant
    if path is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return _cors(FileResponse(path, media_type=_ASSET_MEDIA[name]), request.app.state.cfg)


def _healthz(request: Request) -> JSONResponse:
    # Liveness: the server is up and serving. Storage writability is reported
    # as a degraded detail, NOT a 503, so a read-only pod filesystem cannot make
    # the platform tear down an otherwise-healthy pod.
    ok, detail = request.app.state.store.probe_write()
    return JSONResponse({
        "status": "ok",
        "storage": "writable" if ok else "degraded",
        "detail": detail,
    })


def _readyz(request: Request) -> dict:
    """Readiness, plus a staleness report that deliberately does NOT fail it.

    Stale data is not unreadiness: the app is serving a correct last-known-good
    picture and answering every route. Returning 503 here would have the
    platform restart a healthy container, which fixes no feed and throws away
    the store. The alarm belongs in the payload, the logs and the interface.
    """
    refresher: Refresher = request.app.state.refresher
    health = refresher.health()
    return {"status": "ready", "data_stale": health["stale"], **health}


def _parse_modes(view: str, modes: str) -> set[DataMode]:
    if not modes.strip():
        return set(VIEW_MODES[view])
    wanted: set[DataMode] = set()
    for m in modes.split(","):
        try:
            wanted.add(DataMode(m.strip().upper()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"bad mode {m!r}") from exc
    return wanted


def _parse_ranks(raw: str) -> set[int] | None:
    """Parse `ranks=1,2,3`. Empty means no filter; anything outside 1-5 is a
    client error rather than a silently ignored value."""
    if not raw.strip():
        return None
    out: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            rank = int(token)
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail=f"bad rank {token!r}") from exc
        if rank < 1 or rank > 5:
            raise HTTPException(status_code=400,
                                detail=f"rank {rank} out of range 1-5")
        out.add(rank)
    return out or None


def _tracks(request: Request, view: str = "combined", modes: str = "",
            ranks: str = "",
            if_none_match: Annotated[str | None, Header()] = None) -> Response:
    if view not in VIEW_MODES:
        raise HTTPException(status_code=400, detail="unknown view")
    cfg: Config = request.app.state.cfg
    wanted = _parse_modes(view, modes)
    hrr_map = request.app.state.hrr.map()
    found = request.app.state.events.current(request.app.state.resolver)
    payload = _tracks_payload(cfg, request.app.state.resolver, view, wanted,
                              _parse_ranks(ranks), hrr_map,
                              latest_splits(found["events"]))
    body = json.dumps(payload, separators=(",", ":"))
    # ETag is a CACHE KEY (not security): a non-crypto checksum of the stable
    # content, so an unchanged dataset returns 304 even though generated_at moves.
    etag = _etag(payload, ("view", "classification_banner", "bounds", "count",
                           "ranks", "stale_after_hours", "tracks"))
    if if_none_match == etag:
        return _cors(Response(status_code=304), cfg)
    resp = Response(content=body, media_type=_JSON)
    resp.headers["ETag"] = etag
    return _cors(resp, cfg)


def _etag(payload: dict, keys: tuple[str, ...]) -> str:
    """A CACHE KEY (not security): a non-crypto checksum of the stable content,
    so an unchanged dataset returns 304 even though generated_at moves."""
    stable = json.dumps({k: payload[k] for k in keys},
                        separators=(",", ":"), sort_keys=True)
    return '"' + format(zlib.crc32(stable.encode()) & 0xFFFFFFFF, "08x") + '"'


def _event_view(ev: dict, names: dict[str, str], hrr_map: dict) -> dict:
    """One event as served: the HRR name, rank and country joined on, for the
    1.6.1 reason that the server is the naming authority for every consumer."""
    oid = ev["object_id"]
    entry = hrr_map.get(oid) or {}
    out = {**ev, "name": names.get(oid) or oid, "rank": _rank_of(oid, hrr_map),
           "country": entry.get("country")}
    return out


def _events(request: Request, view: str = "combined", modes: str = "",
            ranks: str = "",
            if_none_match: Annotated[str | None, Header()] = None) -> Response:
    """What changed: manoeuvres, early warnings and silences, newest first
    (1.7.0). Filtered by view and rank exactly as /api/tracks is, so the Shift
    Brief never lists an object the plot is not showing."""
    if view not in VIEW_MODES:
        raise HTTPException(status_code=400, detail="unknown view")
    cfg: Config = request.app.state.cfg
    wanted = _parse_modes(view, modes)
    rank_set = _parse_ranks(ranks)
    hrr_map = request.app.state.hrr.map()
    found = request.app.state.events.current(request.app.state.resolver)
    names = _hrr_names(request)
    events = []
    for ev in found["events"]:
        try:
            mode = DataMode(ev.get("data_mode", "REAL"))
        except ValueError:
            mode = DataMode.REAL
        if mode in wanted and (rank_set is None or _rank_of(ev["object_id"], hrr_map) in rank_set):
            events.append(_event_view(ev, names, hrr_map))
    payload = {
        "view": view, "ranks": sorted(rank_set) if rank_set else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_epoch": found["reference_epoch"],
        "belt_current": found["belt_current"],
        "lookback_hours": found["lookback_hours"],
        "thresholds": found["thresholds"],
        "compute_seconds": found.get("compute_seconds"),
        "count": len(events), "events": events,
    }
    etag = _etag(payload, ("view", "ranks", "reference_epoch", "belt_current",
                           "thresholds", "count", "events"))
    if if_none_match == etag:
        return _cors(Response(status_code=304), cfg)
    resp = Response(content=json.dumps(payload, separators=(",", ":")),
                    media_type=_JSON)
    resp.headers["ETag"] = etag
    return _cors(resp, cfg)


def _meta(request: Request) -> JSONResponse:
    cfg: Config = request.app.state.cfg
    refresher: Refresher = request.app.state.refresher
    return _cors(JSONResponse({
        # The running build, read from the app rather than a third copy of the
        # string. A live /api/meta on 26 September 2026 could prove only
        # "1.6.6 or later", because the block it carried did not exist before
        # that; which build is actually serving should not be an inference.
        "version": request.app.version,
        "classification_default": "UNCLASSIFIED",
        "views": {k: [m.value for m in v] for k, v in VIEW_MODES.items()},
        "refresh_seconds": cfg.refresh_seconds,
        "retention_days": cfg.retention_days,
        "refresh_lookback_hours": cfg.refresh_lookback_hours,
        "manual_count": len(request.app.state.manual.list_active()),
        # Ingest health. `last_refresh` alone is a trap: it advances on every
        # tick whether or not a single record arrived, which is exactly how a
        # fortnight of truncated responses went unnoticed.
        **refresher.health(),
    }), cfg)


def _add_manual(request: Request, elset: ManualElsetIn) -> JSONResponse:
    if not request.app.state.strict_rl.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT)
    request.app.state.manual.add(elset)
    request.app.state.refresher.merge_one(request.app.state.manual)  # deterministic
    return _cors(JSONResponse({"status": "added", "object_id": elset.object_id}),
                 request.app.state.cfg)


def _del_manual(request: Request, object_id: str) -> JSONResponse:
    if not request.app.state.manual.remove(object_id):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    request.app.state.store.remove_object(object_id)  # drop from the plot immediately
    return _cors(JSONResponse({"status": "removed", "object_id": object_id}),
                 request.app.state.cfg)


def _refresh(request: Request) -> JSONResponse:
    if not request.app.state.strict_rl.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT)
    return _cors(JSONResponse(request.app.state.refresher.run_once(force_hrr=True)),
                 request.app.state.cfg)


def _parse_dt(raw: str) -> datetime:
    """Parse an ISO datetime; a naive value is treated as UTC (operators work in
    Zulu). Raises ValueError on anything unparseable."""
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_window(q, now: datetime) -> tuple[datetime, datetime]:
    """Resolve the pull window from the query: an explicit start+end scenario
    range if both are given, else a relative `hours` lookback ending now."""
    start_s, end_s = q.get("start"), q.get("end")
    has_start, has_end = bool(start_s), bool(end_s)
    if has_start != has_end:
        raise HTTPException(status_code=400, detail="start and end must both be set")
    if has_start and has_end:
        try:
            start, end = _parse_dt(start_s), _parse_dt(end_s)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad datetime") from exc
        if start >= end:
            raise HTTPException(status_code=400, detail="start must be before end")
        if end - start > timedelta(days=366):
            raise HTTPException(status_code=400, detail="range exceeds 366 days")
        return start, end
    try:
        hours = int(q.get("hours", "24"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="hours must be an integer") from exc
    hours = max(1, min(hours, 24 * 366))  # 1 hour .. 1 year
    return now - timedelta(hours=hours), now


def _pull(request: Request) -> JSONResponse:
    """Operator-defined pull. mode in {real, sim, combined}. Window is either an
    explicit start+end scenario range or a relative `hours` lookback. Rate-limited
    like refresh."""
    if not request.app.state.strict_rl.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail=_RATE_LIMIT)
    q = request.query_params
    mode = q.get("mode", "real").strip().lower()
    if mode not in VIEW_MODES:
        raise HTTPException(status_code=400, detail="unknown mode")
    start, end = _resolve_window(q, datetime.now(timezone.utc))
    result = request.app.state.refresher.pull_window(mode=mode, start=start, end=end)
    return _cors(JSONResponse(result), request.app.state.cfg)


def _hrr_names(request: Request) -> dict[str, str]:
    """satNo -> JCO common name. The HRR list is the naming authority; without
    it a UDL record shows its catalogue number twice."""
    return request.app.state.hrr.names()


def _coplanar(request: Request) -> JSONResponse:
    """Coplanar angle vs |longitude difference| for the primary's neighbourhood.
    On-demand: computed when the operator opens the Co-Planar view."""
    cfg = request.app.state.cfg
    target = request.query_params.get("target", "").strip()
    if not target:
        return _cors(JSONResponse({"detail": "target required"}, status_code=400), cfg)
    data = request.app.state.resolver.load()
    payload = coplanar_for(data, target, half_width_deg=cfg.coplanar_half_width_deg,
                           names=_hrr_names(request),
                           max_points=cfg.coplanar_max_points,
                           pair_hours=cfg.coplanar_pair_hours)
    return _cors(JSONResponse(payload), cfg)


def _conjunctions(request: Request) -> JSONResponse:
    """Closest approach and TLEs for the target and its +/-10deg neighbours.
    On-demand: computed when an object is selected, not on every refresh."""
    cfg = request.app.state.cfg
    target = request.query_params.get("target", "").strip()
    if not target:
        return _cors(JSONResponse({"detail": "target required"}, status_code=400), cfg)
    data = request.app.state.resolver.load()
    payload = conjunctions_for(data, target, window_hours=cfg.conj_window_hours,
                               priority=parse_priority(cfg.tle_source_priority),
                               names=_hrr_names(request))
    return _cors(JSONResponse(payload), cfg)


def _hrr(request: Request) -> JSONResponse:
    """Current dynamic HRR GEO list (satNo -> name, country, rank) plus its
    marking, source and generation time. The SPA joins tracks to this and plots
    HRR objects only."""
    return _cors(JSONResponse(request.app.state.hrr.as_payload()),
                 request.app.state.cfg)


# --------------------------------------------------------------------------
# Lifespan and factory
# --------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI):
    store: Store = app.state.store
    refresher: Refresher = app.state.refresher
    ok, detail = store.probe_write()
    _log.info("storage boot verdict: %s (%s)", "WRITABLE" if ok else "UNWRITABLE", detail)
    # Seed and refresh in the background. Startup must NEVER block on a source:
    # a slow or unreachable UDL host must not stall readiness and get the pod
    # torn down. The scheduler runs its first refresh immediately, off the loop.
    stop = asyncio.Event()
    task = (asyncio.create_task(refresher.scheduler(stop))
            if app.state.cfg.scheduler_enabled else None)
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            task.cancel()


def _build_sources(cfg: Config, http_client: httpx.Client | None,
                   manual: ManualElsetSource) -> list:
    sources: list = []
    if cfg.udl_enabled:
        sources.append(UDLElsetSource(cfg, client=http_client))
    sources.append(manual)
    # NEVER beside live UDL (1.7.4). Until then DEMO_MODE=true added the
    # synthetic belt alongside UDL, and four of its objects carried real
    # high-interest catalogue numbers labelled REAL.
    if not cfg.udl_enabled:
        sources.append(DemoElsetSource())
    elif cfg.demo_mode:
        _log.warning("DEMO_MODE is set but UDL is enabled: the synthetic demo "
                     "belt is NOT loaded against a live feed")
    return sources


def create_app(cfg: Config | None = None,
               sources: list | None = None,
               http_client: httpx.Client | None = None) -> FastAPI:
    cfg = cfg or load_config()

    # Fail closed in production: a token with a wildcard origin refuses to start.
    if cfg.team_token and cfg.allowed_origin in ("", "*"):
        raise RuntimeError(
            "Refusing to start: TEAM_TOKEN is set but ALLOWED_ORIGIN is "
            "unset or '*'. Set ALLOWED_ORIGIN to the app's real origin."
        )

    store = Store(cfg.storage_dir())
    if cfg.udl_enabled:
        store.purge_synthetic()   # nothing synthetic survives into a live store
    manual = ManualElsetSource(cfg.storage_dir())
    hrr = HrrStore(cfg, http_client=http_client,
                   static_fallback=_ASSET_PATHS["hrr-geo.json"])
    if sources is None:
        sources = _build_sources(cfg, http_client, manual)
    refresher = Refresher(cfg, store, sources, SingleFlight(), hrr=hrr)
    engine = EventEngine(Thresholds(ew_floor=cfg.event_ew_floor,
                                    ns_floor=cfg.event_ns_floor,
                                    sigma=cfg.event_sigma,
                                    quiet_hours=cfg.event_quiet_hours,
                                    max_residual=cfg.drift_max_residual_deg),
                         cfg.stale_after_hours)

    # ONE resolved view of the store (1.7.5): the plot, the TLE, co-planar,
    # the conjunction screen and the Shift Brief all read the provider the
    # agreement check chose, so no two of them can disagree about which.
    resolver = Resolver(store, policy_from(cfg))

    def _warm_events() -> dict:
        return engine.current(resolver)

    refresher.on_cycle = _warm_events

    app = FastAPI(title="PSIRENS", version="1.7.10", lifespan=_lifespan)
    # Request context for every log line, and one standard request line. The
    # X-Auth-Request-Email header becomes user.email, as the standard requires;
    # LOG_USER_EMAIL=false switches that off (see config.py).
    app.add_middleware(RequestLog, log_email=cfg.log_user_email)
    app.state.cfg = cfg
    app.state.store = store
    app.state.manual = manual
    app.state.refresher = refresher
    app.state.hrr = hrr
    app.state.events = engine
    app.state.resolver = resolver
    app.state.global_rl = RateLimiter(limit=120, window_s=60.0)
    app.state.strict_rl = RateLimiter(limit=6, window_s=60.0)

    _rl = _err(429, "rate limited")
    app.add_api_route("/healthz", _healthz, methods=["GET"])
    app.add_api_route("/readyz", _readyz, methods=["GET"])
    app.add_api_route("/", _index, methods=["GET"])
    app.add_api_route("/favicon.ico", _favicon, methods=["GET"])
    app.add_api_route("/static/{name}", _static_asset, methods=["GET"],
                      responses=_err(404, "unknown asset"))
    app.add_api_route("/api/tracks", _tracks, methods=["GET"],
                      dependencies=[Depends(_global_gate)],
                      responses={**_err(400, "unknown view or mode"), **_rl})
    app.add_api_route("/api/meta", _meta, methods=["GET"])
    app.add_api_route("/api/hrr", _hrr, methods=["GET"],
                      dependencies=[Depends(_global_gate)])
    app.add_api_route("/api/conjunctions", _conjunctions, methods=["GET"],
                      dependencies=[Depends(_global_gate)])
    app.add_api_route("/api/coplanar", _coplanar, methods=["GET"],
                      dependencies=[Depends(_global_gate)])
    app.add_api_route("/api/events", _events, methods=["GET"],
                      dependencies=[Depends(_global_gate)],
                      responses={**_err(400, "unknown view, mode or rank"), **_rl})
    app.add_api_route("/api/manual-elset", _add_manual, methods=["POST"],
                      dependencies=[Depends(_require_token)], responses=_rl)
    app.add_api_route("/api/manual-elset/{object_id}", _del_manual, methods=["DELETE"],
                      dependencies=[Depends(_require_token)], responses=_err(404, _NOT_FOUND))
    app.add_api_route("/api/refresh", _refresh, methods=["POST"],
                      dependencies=[Depends(_require_token)], responses=_rl)
    app.add_api_route("/api/pull", _pull, methods=["POST"],
                      dependencies=[Depends(_require_token)], responses=_rl)
    return app


# ASGI entrypoint for gunicorn: `gunicorn psirens.main:app`
app = create_app()
