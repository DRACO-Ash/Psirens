"""Which provider drives each object, and whether the providers agree (1.7.5).

WHY THIS EXISTS. Until 1.7.5 every provider's fixes for one catalogue number
were pooled into one track. COSMOS 2589 (64467) showed what that costs:
NorthStar's element sets put it at 64E while five other providers, and
NorthStar's own state vectors, put it at 127.9E. The pooled track drew a line
between the two, fitted -12.96 deg/day through it at 19 deg rms, and the Shift
Brief CONFIRMED a manoeuvre that was a provider arriving. Owner rule, 17
November 2026: "The UDL is the primary source of information, we need to make
calculations based on that information. We are not in the business of making
this up."

WHAT HAPPENS HERE, per object and per data mode:

1. AGE-OUT. A provider's newest element set more than `max_lag_hours` older
   than the newest held is not considered (`tle.split_aged`).
2. AGREEMENT AT ONE INSTANT. Every remaining provider's own element set is
   propagated with SGP4 to the same time (the newest element-set epoch) and
   the sub-satellite longitudes are compared. A provider is IN AGREEMENT when
   it lies within `agree_deg` of at least half of the others. One that is not
   is SET ASIDE, with the distance stated. When nobody has a majority (two
   providers that disagree, or two camps of equal size) the object is
   DISPUTED: no provider is chosen as the truth, the drift rate is withheld
   and no event is raised. An element set that cannot be propagated as issued
   is UNVERIFIED: listed, never judged, and not preferred over one that was.
3. ONE PROVIDER DRIVES. The selection policy (commercial first by default,
   newest within class) picks among the providers left. That provider's own
   samples are the plotted track, its drift fit is the drift rate, its element
   set is the head, the needle, the TLE, the co-planar plane and the
   conjunction screen, and its track is what the Shift Brief reads. Every
   other provider is listed with its status and its own fit, never blended.
4. UNTAGGED HISTORY (stored before 1.7.4, provider unknown) is never mixed
   with tagged samples. It is shown only for an object that has nothing else,
   and then with the drift rate withheld and no events.

None of this decides WHY a provider disagrees. That is the provider's to
answer; this module only refuses to publish a calculation across it.

COST. `Resolver` caches the resolved store against the file's signature, the
same way `events.EventEngine` caches detection, and the refresh thread warms
both after every cycle. One SGP4 propagation per provider per object, plus one
drift fit per provider stream.
"""

from __future__ import annotations

import os
import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sgp4.api import jday

from .astro import drift_fit, earth_fixed_lon_deg
from .conjunction import build_satrec
from .store import sample_provenance
from .tle import (DEFAULT_PRIORITY, ELSET_MAX_LAG_HOURS, by_policy, epoch_of,
                  mode_of, parse_priority, policy_rank, propagation_refusal,
                  split_aged, usable_candidates)

AGREE_DEG = 1.0           # two providers further apart than this disagree
MAX_RESIDUAL_DEG = 1.0    # a drift fit missing its data by more is withheld
MODE_ORDER = ("REAL", "EXERCISE", "TEST", "SIMULATED")
UNTAGGED = "untagged"

AGREED = "agreed"         # at least two providers checked, a majority agrees
SINGLE = "single"         # one provider could be checked; nothing to compare
DISPUTED = "disputed"     # providers checked, no majority: nothing is chosen
UNCHECKED = "unchecked"   # no provider's element set could be propagated


@dataclass(frozen=True)
class Policy:
    priority: tuple[str, ...] = DEFAULT_PRIORITY
    agree_deg: float = AGREE_DEG
    max_lag_hours: float = ELSET_MAX_LAG_HOURS
    max_residual_deg: float = MAX_RESIDUAL_DEG


def policy_from(cfg: Any) -> Policy:
    return Policy(priority=parse_priority(cfg.tle_source_priority),
                  agree_deg=cfg.provider_agree_deg,
                  max_lag_hours=cfg.elset_max_lag_hours,
                  max_residual_deg=cfg.drift_max_residual_deg)


@dataclass
class Assessment:
    """The agreement check for one object in one data mode."""

    mode: str
    status: str = UNCHECKED
    common_epoch: str | None = None
    positions: dict[str, float] = field(default_factory=dict)
    set_aside: dict[str, str] = field(default_factory=dict)
    unverified: dict[str, str] = field(default_factory=dict)
    aged_out: dict[str, str] = field(default_factory=dict)
    eligible: list[dict] = field(default_factory=list)


def separation_deg(a: float, b: float) -> float:
    """Shortest angular distance between two longitudes."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _wrap(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


def centre_deg(lons: list[float]) -> float:
    """Median longitude, taken across the seam correctly."""
    ref = lons[0]
    return _wrap(ref + statistics.median(_wrap(v - ref) for v in lons))


def lon_at(el: dict, when: datetime) -> float | None:
    """Sub-satellite longitude of one provider's own element set, propagated
    by SGP4 to `when`. None when it cannot be propagated as issued."""
    if propagation_refusal(el) is not None:
        return None
    try:
        sat = build_satrec(el)
    except (ValueError, OverflowError, KeyError, TypeError):
        return None
    jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                  when.second + when.microsecond / 1_000_000.0)
    err, r, _ = sat.sgp4(jd, fr)
    if err != 0:
        return None
    return earth_fixed_lon_deg(r, jd, fr)


def _has_majority(name: str, positions: dict[str, float], agree_deg: float) -> bool:
    others = [q for q in positions if q != name]
    near = sum(1 for q in others if separation_deg(positions[name], positions[q]) <= agree_deg)
    return 2 * near >= len(others)


def judge(positions: dict[str, float], agree_deg: float) -> tuple[str, set[str]]:
    """(status, providers in agreement) for longitudes taken at one instant."""
    if not positions:
        return UNCHECKED, set()
    if len(positions) == 1:
        return SINGLE, set(positions)
    agreed = {p for p in positions if _has_majority(p, positions, agree_deg)}
    return (AGREED, agreed) if agreed else (DISPUTED, set())


def _set_aside_reasons(positions: dict[str, float], agreed: set[str]) -> dict[str, str]:
    centre = centre_deg([positions[p] for p in sorted(agreed)])
    return {p: (f"{separation_deg(lon, centre):.2f} deg from the {len(agreed)} "
                f"provider(s) that agree")
            for p, lon in positions.items() if p not in agreed}


def _locate(fresh: list[dict], when: datetime, a: Assessment) -> None:
    for el in fresh:
        source = str(el.get("source") or "")
        refused = propagation_refusal(el)
        lon = None if refused else lon_at(el, when)
        if lon is None:
            a.unverified[source] = refused or "SGP4 failed for this element set"
        else:
            a.positions[source] = lon


def assess(obj: dict, mode: str, policy: Policy) -> Assessment:
    """Age out, propagate to a common instant, and judge the providers."""
    a = Assessment(mode=mode)
    mine = [el for el in usable_candidates(obj) if mode_of(el) == mode]
    fresh, aged = split_aged(mine, policy.max_lag_hours)
    a.aged_out = {str(el.get("source") or ""): str(el.get("epoch")) for el in aged}
    when = max((w for w in map(epoch_of, fresh) if w is not None), default=None)
    if when is None:
        return a
    a.common_epoch = when.isoformat()
    _locate(fresh, when, a)
    a.status, agreed = judge(a.positions, policy.agree_deg)
    a.eligible = _eligible(a, fresh, agreed, policy)
    return a


def _eligible(a: Assessment, fresh: list[dict], agreed: set[str],
              policy: Policy) -> list[dict]:
    """Who may drive, in selection order. Agreement, where it was reached,
    narrows the field to the providers in it. Otherwise a provider that
    could be checked comes before one that could not."""
    if a.status == AGREED:
        a.set_aside = _set_aside_reasons(a.positions, agreed)
        return by_policy([el for el in fresh if el.get("source") in agreed], policy.priority)
    checked = [el for el in fresh if el.get("source") in a.positions]
    rest = [el for el in fresh if el.get("source") not in a.positions]
    return by_policy(checked, policy.priority) + by_policy(rest, policy.priority)


# --------------------------------------------------------------------------
# Streams: one provider's samples in one mode
# --------------------------------------------------------------------------
# Streams are keyed MODE first, then provider: {mode: {provider: samples}}.
# Until 1.7.6 they were keyed by a (provider, mode) tuple, and every loop over
# them unpacked a dict key, which Code Quality reads as a missed .items()
# (python:S7517, four times on the 1.7.5 upload). Keyed this way, the provider
# streams for one mode are a plain dict lookup and nothing is unpacked.
Streams = dict[str, dict[str, list[dict]]]


def streams_of(obj: dict) -> tuple[Streams, list[dict]]:
    """Samples grouped by mode, then provider, and the untagged remainder."""
    streams: Streams = {}
    untagged: list[dict] = []
    for s in obj.get("samples") or []:
        prov = sample_provenance(obj, s)
        if prov is None:
            untagged.append(s)
        else:
            streams.setdefault(prov["data_mode"], {}).setdefault(prov["source"], []).append(s)
    for by_provider in streams.values():
        for rows in by_provider.values():
            rows.sort(key=lambda s: s["epoch"])
    return streams, untagged


def _newest_ts(rows: list[dict]) -> float:
    return datetime.fromisoformat(rows[-1]["epoch"]).timestamp()


def _pick_uncatalogued(here: dict[str, list[dict]], names: list[str],
                       policy: Policy) -> str | None:
    """A provider with samples but no current element set drives only when no
    provider with one has samples. Policy class, then the newest last fix:
    one comparison on a compound key, not two sorts (python:S7508, S8517)."""
    if not names:
        return None
    return min(names, key=lambda n: (policy_rank(n, policy.priority), -_newest_ts(here[n])))


def choose_primary(a: Assessment, streams: Streams, policy: Policy) -> str | None:
    """The provider whose samples drive the object in this mode."""
    here = streams.get(a.mode, {})
    for el in a.eligible:
        if el.get("source") in here:
            return str(el.get("source"))
    if a.status == AGREED or a.eligible:
        return None   # the providers that may drive have no samples held
    excluded = set(a.set_aside) | set(a.aged_out) | set(a.unverified) | set(a.positions)
    names = [n for n in here if n not in excluded]
    return _pick_uncatalogued(here, names, policy)


def _fit_summary(rows: list[dict], policy: Policy) -> dict:
    pairs = [(datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in rows]
    fit = drift_fit(pairs)
    if fit is None:
        return {"drift_deg_per_day": None, "drift_residual_deg": None, "drift_withheld": None}
    poor = fit.residual_deg > policy.max_residual_deg
    return {"drift_deg_per_day": None if poor else round(fit.rate_deg_per_day, 4),
            "drift_residual_deg": round(fit.residual_deg, 4),
            "drift_withheld": "fit too poor" if poor else None}


def _status_note(name: str, a: Assessment, primary: str | None) -> tuple[str, str]:
    if name == primary:
        if a.status == DISPUTED:
            return "track", "drives the plot; the providers disagree, so no drift and no events"
        return "track", "drives the plot, drift, TLE, co-planar, screen and events"
    if name in a.set_aside:
        return "set aside", a.set_aside[name]
    if name in a.aged_out:
        return "aged out", f"newest element set {a.aged_out[name]} is too old"
    if name in a.unverified:
        return "unverified", a.unverified[name]
    if a.status == DISPUTED:
        return "disputed", "disagrees with another provider; neither is chosen"
    if a.status == AGREED and name in a.positions:
        return "agrees", "within the agreement limit; ranked below the track provider"
    if name in a.positions:
        return "not used", "ranked below the track provider by the selection policy"
    return "not used", "no current element set from this provider"


def provider_rows(a: Assessment, streams: Streams, primary: str | None,
                  policy: Policy, untagged: int) -> dict[str, dict]:
    """Every provider seen for the object in this mode, each with its own
    status, its own longitude at the common instant and its own fit."""
    here = streams.get(a.mode, {})
    names = (set(here) | set(a.positions) | set(a.set_aside)
             | set(a.unverified) | set(a.aged_out))
    out: dict[str, dict] = {}
    for name in sorted(names):
        rows = here.get(name, [])
        status, note = _status_note(name, a, primary)
        lon = a.positions.get(name)
        out[name] = {"count": len(rows), "status": status, "note": note,
                     "newest_epoch": rows[-1]["epoch"] if rows else None,
                     "lon_at_common_deg": None if lon is None else round(lon, 4),
                     **_fit_summary(rows, policy)}
    if untagged:
        out[UNTAGGED] = {"count": untagged, "status": "excluded",
                         "note": "stored before 1.7.4; provider unknown",
                         "newest_epoch": None, "lon_at_common_deg": None,
                         "drift_deg_per_day": None, "drift_residual_deg": None,
                         "drift_withheld": None}
    return out


def _block(a: Assessment, primary: str | None, rows: dict, policy: Policy,
           legacy: bool) -> dict:
    return {
        "mode": a.mode, "status": a.status, "primary": primary,
        "samples_untagged": legacy, "common_epoch": a.common_epoch,
        "agree_deg": policy.agree_deg, "max_lag_hours": policy.max_lag_hours,
        "positions": {k: round(v, 4) for k, v in sorted(a.positions.items())},
        "set_aside": dict(sorted(a.set_aside.items())),
        "unverified": dict(sorted(a.unverified.items())),
        "aged_out": dict(sorted(a.aged_out.items())),
        "providers": rows,
    }


def _modes_present(obj: dict, streams: Streams) -> list[str]:
    seen = set(streams) | {mode_of(el) for el in usable_candidates(obj)}
    return [m for m in MODE_ORDER if m in seen]


def _legacy_mode(obj: dict) -> str | None:
    mode = obj.get("data_mode")
    return mode if mode in MODE_ORDER else None


def _elset_for(a: Assessment, primary: str | None) -> dict | None:
    for el in a.eligible:
        if primary is None or el.get("source") == primary:
            return el
    return None


@dataclass
class _Resolved:
    streams: dict[str, list[dict]] = field(default_factory=dict)
    blocks: dict[str, dict] = field(default_factory=dict)
    elsets: dict[str, dict] = field(default_factory=dict)
    heard: dict[str, list[str]] = field(default_factory=dict)


def _resolve_mode(obj: dict, mode: str, streams: Streams, untagged: list[dict],
                  policy: Policy, out: _Resolved) -> None:
    a = assess(obj, mode, policy)
    primary = choose_primary(a, streams, policy)
    here = streams.get(mode, {})
    tagged_here = bool(here)
    legacy = not streams and bool(untagged) and _legacy_mode(obj) == mode
    rows = provider_rows(a, streams, primary, policy,
                         len(untagged) if tagged_here or legacy else 0)
    out.blocks[mode] = _block(a, primary, rows, policy, legacy)
    el = _elset_for(a, primary)
    if el is not None:
        out.elsets[mode] = el
    if primary is not None:
        out.streams[mode] = here[primary]
    elif legacy:
        out.streams[mode] = sorted(untagged, key=lambda s: s["epoch"])
    skip = set(a.set_aside) | set(a.aged_out)
    heard = sorted(s["epoch"] for name, rows_ in here.items()
                   if name not in skip for s in rows_)
    out.heard[mode] = heard or [s["epoch"] for s in out.streams.get(mode, [])]


def resolve(obj: dict, policy: Policy) -> dict:
    """The object as served: one provider per mode, never a blend.

    The returned record keeps every stored key, and replaces `samples`,
    `elset` and `elset_candidates` with the DEFAULT mode's (REAL first) so a
    consumer that reads those keys reads one provider. `mode_streams`,
    `agreement` and `heard` carry every mode for the tracks view and the
    QUIET detector.
    """
    streams, untagged = streams_of(obj)
    out = _Resolved()
    for mode in _modes_present(obj, streams) or [m for m in [_legacy_mode(obj)] if m]:
        _resolve_mode(obj, mode, streams, untagged, policy, out)
    default = next((m for m in MODE_ORDER if m in out.streams),
                   next((m for m in MODE_ORDER if m in out.blocks), None))
    rec = {k: v for k, v in obj.items()
           if k not in ("samples", "elset", "elset_candidates")}
    el = out.elsets.get(default) if default else None
    rec.update({
        "samples": out.streams.get(default, []) if default else [],
        "elset": el,
        "elset_candidates": {} if el is None else {"primary": el},
        "data_mode": default or obj.get("data_mode"),
        "mode_streams": out.streams,
        "mode_elsets": out.elsets,
        "agreement": out.blocks,
        "heard": out.heard.get(default, []) if default else [],
    })
    return rec


def resolve_data(data: dict, policy: Policy) -> dict:
    """Every object resolved. Marked so a consumer handed resolved data does
    not resolve it a second time."""
    objects = {oid: resolve(rec, policy)
               for oid, rec in (data.get("objects") or {}).items()}
    return {**{k: v for k, v in data.items() if k != "objects"},
            "objects": objects, "resolved": True}


def drift_withheld(block: dict | None) -> str | None:
    """Why no drift rate may be published for this mode, or None."""
    if not block:
        return None
    if block.get("samples_untagged"):
        return ("these positions were stored before 1.7.4 and their provider is "
                "unknown; a re-pull tags them")
    if block.get("status") == DISPUTED:
        return "the providers disagree on where this object is; none is chosen"
    return None


class Resolver:
    """Caches `resolve_data` against the store file's signature.

    Duck-types the store for `events.EventEngine` (`path`, `load`), so the
    Shift Brief reads exactly the resolved view the plot does.
    """

    def __init__(self, store: Any, policy: Policy):
        self.store = store
        self.policy = policy
        self.path = store.path
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._result: dict | None = None
        self.computations = 0

    def _signature(self) -> tuple:
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (None, None)

    def load(self) -> dict:
        key = self._signature()
        with self._lock:
            if self._result is not None and key == self._key:
                return self._result
            self._result = resolve_data(self.store.load(), self.policy)
            self._key = key
            self.computations += 1
            return self._result
