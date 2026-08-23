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

from .astro import drift_deg_per_day
from .config import Config, load_config
from .models import VIEW_MODES, DataMode, ManualElsetIn
from .conjunction import conjunctions_for
from .tle import parse_priority
from .hrr import HrrStore
from .refresh import Refresher
from .security import RateLimiter, SingleFlight, token_ok
from .sources import DemoElsetSource, ManualElsetSource, UDLElsetSource
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
_log = logging.getLogger("psirens")

_STATIC = os.path.join(os.path.dirname(__file__), "static")
_PNG = "image/png"
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
    "hrr-geo.json": "application/json",  # GEO subset of the JCO HRR list
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


def _tracks_payload(cfg: Config, store: Store, view: str,
                    modes: set[DataMode]) -> dict:
    data = store.load()
    tracks, markings = [], []
    for oid, rec in data.get("objects", {}).items():
        try:
            mode = DataMode(rec.get("data_mode", "REAL"))
        except ValueError:
            mode = DataMode.REAL
        if mode not in modes:
            continue
        samples = rec.get("samples", [])
        if not samples:
            continue
        pairs = [
            (datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in samples
        ]
        markings.append(rec.get("classification_marking", "U"))
        tracks.append({
            "object_id": oid,
            "name": rec.get("name", oid),
            "data_mode": mode.value,
            "classification_marking": rec.get("classification_marking", "U"),
            "source": rec.get("source", ""),
            "origin": rec.get("origin", ""),
            "target": rec.get("target"),
            "samples": samples,
            "drift_deg_per_day": drift_deg_per_day(pairs),
            "ra_deg": (rec.get("elset") or {}).get("raan_deg"),
        })
    return {
        "view": view,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification_banner": _banner(markings),
        "bounds": {
            "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
            "inc_min": cfg.inc_min, "inc_max": cfg.inc_max,
        },
        "count": len(tracks),
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
    refresher: Refresher = request.app.state.refresher
    last = refresher.last_run.isoformat() if refresher.last_run else None
    return {"status": "ready", "last_refresh": last}


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


def _tracks(request: Request, view: str = "combined", modes: str = "",
            if_none_match: Annotated[str | None, Header()] = None) -> Response:
    if view not in VIEW_MODES:
        raise HTTPException(status_code=400, detail="unknown view")
    cfg: Config = request.app.state.cfg
    wanted = _parse_modes(view, modes)
    payload = _tracks_payload(cfg, request.app.state.store, view, wanted)
    body = json.dumps(payload, separators=(",", ":"))
    # ETag is a CACHE KEY (not security): a non-crypto checksum of the stable
    # content, so an unchanged dataset returns 304 even though generated_at moves.
    stable = json.dumps(
        {k: payload[k] for k in ("view", "classification_banner", "bounds",
                                 "count", "tracks")},
        separators=(",", ":"), sort_keys=True,
    )
    etag = '"' + format(zlib.crc32(stable.encode()) & 0xFFFFFFFF, "08x") + '"'
    if if_none_match == etag:
        return _cors(Response(status_code=304), cfg)
    resp = Response(content=body, media_type="application/json")
    resp.headers["ETag"] = etag
    return _cors(resp, cfg)


def _meta(request: Request) -> JSONResponse:
    cfg: Config = request.app.state.cfg
    refresher: Refresher = request.app.state.refresher
    last = refresher.last_run.isoformat() if refresher.last_run else None
    return _cors(JSONResponse({
        "classification_default": "UNCLASSIFIED",
        "views": {k: [m.value for m in v] for k, v in VIEW_MODES.items()},
        "refresh_seconds": cfg.refresh_seconds,
        "retention_days": cfg.retention_days,
        "last_refresh": last,
        "manual_count": len(request.app.state.manual.list_active()),
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


def _conjunctions(request: Request) -> JSONResponse:
    """Closest approach and TLEs for the target and its +/-10deg neighbours.
    On-demand: computed when an object is selected, not on every refresh."""
    cfg = request.app.state.cfg
    target = request.query_params.get("target", "").strip()
    if not target:
        return _cors(JSONResponse({"detail": "target required"}, status_code=400), cfg)
    data = request.app.state.store.load()
    payload = conjunctions_for(data, target, window_hours=cfg.conj_window_hours,
                               priority=parse_priority(cfg.tle_source_priority))
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
    if cfg.demo_mode or not cfg.udl_enabled:
        sources.append(DemoElsetSource())
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
    manual = ManualElsetSource(cfg.storage_dir())
    hrr = HrrStore(cfg, http_client=http_client,
                   static_fallback=_ASSET_PATHS["hrr-geo.json"])
    if sources is None:
        sources = _build_sources(cfg, http_client, manual)
    refresher = Refresher(cfg, store, sources, SingleFlight(), hrr=hrr)

    app = FastAPI(title="PSIRENS", version="1.5.0", lifespan=_lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.manual = manual
    app.state.refresher = refresher
    app.state.hrr = hrr
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
