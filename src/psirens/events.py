"""Event detection: what changed in the belt since the operator last looked.

ADDED in 1.7.0 (the Shift Brief, proposal of 16 November 2026, approved by the
owner the same day). PSIRENS answered "where is everything?" and could not
answer "what changed?". This module finds, for every stored object:

  STARTED / STOPPED / EW   a significant change in fitted longitude drift rate
                           across a split (an E-W manoeuvre, a drift that
                           stopped, or a rate change between two drifts);
  NS                       a step change in inclination (an N-S manoeuvre);
  LEAVING                  the newest fixes departing from the track fitted to
                           the ones before them (an early warning only);
  QUIET                    no fix for a long time while the rest of the belt is
                           current and the object normally reports often.

HOW AN E-W EVENT IS CONFIRMED. At every candidate split (every STEP_H hours)
the drift rate is fitted independently either side, over WIN_H hours each,
with the same `astro.drift_fit` the inspector uses, so the libration is fitted
out rather than mistaken for motion. The split is an event only when the two
rates differ by more than a physical floor AND by `sigma` combined standard
errors. The best split wins.

THE EDGE RULE, found by the prototype in itself. Evidence that peaks at the
LAST split that could be tested means the "after" window may straddle a burn
that happened later still, so both the time and the new rate are wrong, and
confidently so. The prototype CONFIRMED a 1.0 deg/day burn 17 hours early at
0.31 deg/day this way. Such a split is never confirmed: it falls through to
the early-warning test and is reported, at most, as SUSPECTED.

THRESHOLDS ARE STARTING POINTS (owner decision, 16 November 2026), tuned on a
synthetic belt with libration, provider bias and scatter, PENDING calibration
against the live store. They are environment-overridable via `Config`.

COST. Computed once per change of the store, in the refresh thread, and cached
by `EventEngine`; never once per request. Only the last HISTORY_H hours of
each object are read, because an older sample cannot inform a split inside
the lookback.
"""

from __future__ import annotations

import bisect
import math
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .astro import (DriftFit, drift_fit, model_coefficients, model_value,
                    resolves_libration, unwrap)

WIN_H = 48.0            # each side of a candidate split
STEP_H = 3.0            # spacing of candidate splits
LOOKBACK_H = 96.0       # an event older than this is not news
HISTORY_H = LOOKBACK_H + WIN_H
EW_MIN_POINTS = 20      # an object with fewer fixes than this is not tested
EW_MIN_AFTER_H = 24.0   # a confirmed rate needs a full libration cycle after it
NS_FIRST_H = 24.0       # earliest N-S split after the first fix
NS_LAST_H = 12.0        # latest N-S split before the last fix
NS_BEFORE_MIN = 8
NS_AFTER_MIN = 6
EARLY_SCAN_H = 24       # hours of hourly early-warning checks, ending at the last fix
EARLY_TRAIN_H = 72.0
EARLY_FRESH_H = 6.0
EARLY_FRESH = 3
EARLY_TRAIN_MIN = 20
EARLY_DEV_FLOOR = 0.03  # deg: below this a departure is inside provider scatter
QUIET_CADENCE_DIVISOR = 3.0   # "normally reports often": median step <= quiet/3
QUIET_CADENCE_MIN = 6
EVIDENCE_MAX_POINTS = 240
# Burn timing (1.7.11, owner approval of 23 November 2026). A burn changes the
# drift RATE, not the longitude, so the trend fitted before it and the trend
# fitted after it meet at the burn. The winning 3-hour split is refined to that
# crossing, re-fitted about the estimate CROSS_ITERATIONS times, and refused
# (the split stands) if it lands more than CROSS_CLAMP_H from the split.
# MEASURED on 393 synthetic burns through this detector: median error at
# 0.3 deg/day fell from 1.1 h (split) to 0.2 h (crossing).
CROSS_ITERATIONS = 3
CROSS_CLAMP_H = 2 * STEP_H
CROSS_MIN_POINTS = 8
# The stated half-width is CROSS_COVERAGE_K standard errors, the factor that
# covers 95% of true burn times on the synthetic belt (owner decision: widen to
# measured coverage, not the nominal 2 sigma, which covered about 91%).
# MEASURED by proposals/2026-11-23-ew-timing/prototype/coverage.py on this
# function: 95th percentile of |error| / sigma = 2.583 on 360 burns (seed 7),
# rounded up to 2.6; on an independent 360 (seed 8) 2.6 covers 98.3%. Plain
# 2 sigma covered 90.6%.
CROSS_COVERAGE_K = 2.6

KIND_EW = ("STARTED", "STOPPED", "EW")
CONFIRMED = "CONFIRMED"
SUSPECTED = "SUSPECTED"


@dataclass(frozen=True)
class Thresholds:
    ew_floor: float = 0.02     # deg/day
    ns_floor: float = 0.008    # deg
    sigma: float = 6.0
    quiet_hours: float = 12.0
    # A fit missing its data by more than this (deg rms) says nothing about a
    # rate, so it can neither confirm nor rule out a burn (1.7.5).
    max_residual: float = 1.0


@dataclass(frozen=True)
class Crossing:
    when: datetime
    sigma_hours: float


@dataclass(frozen=True)
class _Split:
    when: datetime
    before: DriftFit
    after: DriftFit
    z: float
    edge: bool = False


def _hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def _parse(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def series(samples: list[dict], field: str,
           since: datetime | None = None) -> list[tuple[datetime, float]]:
    """(epoch, value) pairs for one sample field, sorted, optionally recent only."""
    out = []
    for s in samples:
        value = s.get(field)
        if value is None or "epoch" not in s:
            continue
        when = _parse(s["epoch"])
        if since is None or when >= since:
            out.append((when, float(value)))
    out.sort(key=lambda p: p[0])
    return out


def _between(rows: list[tuple[datetime, float]], keys: list[datetime],
             lo: datetime, hi: datetime) -> list[tuple[datetime, float]]:
    """Rows with lo <= epoch < hi, by bisection on the pre-extracted epochs."""
    return rows[bisect.bisect_left(keys, lo):bisect.bisect_left(keys, hi)]


def rate_sigma(fit: DriftFit) -> float:
    """Standard error of a fitted slope: rms * sqrt(12) / (span * sqrt(n))."""
    span_d = max(fit.span_hours / 24.0, 1e-6)
    return fit.residual_deg * math.sqrt(12.0) / (span_d * math.sqrt(max(fit.points, 3)))


def _ew_candidate(lon: list[tuple[datetime, float]], keys: list[datetime],
                  t: datetime, th: Thresholds) -> _Split | None:
    win = timedelta(hours=WIN_H)
    fb = drift_fit(_between(lon, keys, t - win, t), window_days=WIN_H / 24.0)
    fa = drift_fit(_between(lon, keys, t, t + win), window_days=WIN_H / 24.0)
    if fb is None or fa is None or fa.span_hours < EW_MIN_AFTER_H:
        return None
    if max(fb.residual_deg, fa.residual_deg) > th.max_residual:
        return None
    delta = fa.rate_deg_per_day - fb.rate_deg_per_day
    sig = math.hypot(rate_sigma(fb), rate_sigma(fa)) or 1e-9
    z = abs(delta) / sig
    if abs(delta) <= th.ew_floor or z <= th.sigma:
        return None
    return _Split(when=t, before=fb, after=fa, z=z)


def ew_split(lon: list[tuple[datetime, float]], ref: datetime,
             th: Thresholds) -> _Split | None:
    """The most significant drift-rate change inside the lookback, or None.

    `lon` must already be unwrapped across the seam. The result carries
    `edge=True` when the evidence peaked at the last split that could be
    tested, and such a split must not be reported as CONFIRMED.
    """
    if len(lon) < EW_MIN_POINTS:
        return None
    keys = [p[0] for p in lon]
    step = timedelta(hours=STEP_H)
    t = max(lon[0][0] + timedelta(hours=WIN_H), ref - timedelta(hours=LOOKBACK_H))
    last = lon[-1][0] - timedelta(hours=EW_MIN_AFTER_H)
    best: _Split | None = None
    tested: datetime | None = None
    while t <= last:
        cand = _ew_candidate(lon, keys, t, th)
        tested = t
        if cand is not None and (best is None or cand.z > best.z):
            best = cand
        t += step
    if best is None or tested is None:
        return None
    # Constructed explicitly: dataclasses.replace is typed as returning the
    # generic DataclassInstance, which Code Quality read as a type mismatch
    # against this function's annotation (python:S5886, raised on 1.7.0).
    return _Split(when=best.when, before=best.before, after=best.after,
                  z=best.z, edge=(tested - best.when) <= step)


def _trend(lon: list[tuple[datetime, float]], keys: list[datetime],
           lo: datetime, hi: datetime, origin: datetime) -> tuple[float, float, float, int] | None:
    """(intercept at `origin`, rate deg/day, rms, points) of the fitted trend,
    libration modelled where the sampling resolves it."""
    rows = _between(lon, keys, lo, hi)
    if len(rows) < CROSS_MIN_POINTS:
        return None
    xs = [(w - origin).total_seconds() / 86400.0 for w, _ in rows]
    fitted = model_coefficients(xs, [v for _, v in rows], resolves_libration(xs))
    if fitted is None:
        return None
    coeffs, rms = fitted
    return coeffs[0], coeffs[1], rms, len(rows)


def ew_crossing(lon: list[tuple[datetime, float]], split_when: datetime) -> Crossing | None:
    """Where the before and after trends meet, refined about the estimate, or
    None when they do not meet within CROSS_CLAMP_H of the split (the split
    then stands). The standard error comes from the two intercepts at the
    window edge, about 2 rms / sqrt(n) each, over the change in rate."""
    keys = [p[0] for p in lon]
    win = timedelta(hours=WIN_H)
    t, sigma_h = split_when, 0.0
    for _ in range(CROSS_ITERATIONS):
        b = _trend(lon, keys, t - win, t, t)
        a = _trend(lon, keys, t, t + win, t)
        if b is None or a is None or abs(b[1] - a[1]) < 1e-9:
            return None
        s_int = math.hypot(2 * b[2] / math.sqrt(b[3]), 2 * a[2] / math.sqrt(a[3]))
        sigma_h = s_int / abs(b[1] - a[1]) * 24.0
        t = t + timedelta(days=(a[0] - b[0]) / (b[1] - a[1]))
        if abs(_hours(split_when, t)) > CROSS_CLAMP_H:
            return None
    return Crossing(when=t, sigma_hours=sigma_h)


def hour_key(when: datetime) -> str:
    """The epoch rounded to the nearest hour, which an E-W event id is keyed
    on (owner decision, 23 November 2026). MEASURED: over 9 hours of new data
    the id changed for 2 of 60 burns, against 34 of 60 on the 3-hour split."""
    base = when.replace(minute=0, second=0, microsecond=0)
    return (base + timedelta(hours=1) if when - base >= timedelta(minutes=30) else base).isoformat()


def ew_kind(before: float, after: float, floor: float) -> str:
    if abs(after) < floor <= abs(before):
        return "STOPPED"
    if abs(before) < floor:
        return "STARTED"
    return "EW"


def _ns_score(b: list[float], a: list[float]) -> tuple[float, float]:
    mb = statistics.median(b)
    d = statistics.median(a) - mb
    mad = statistics.median([abs(v - mb) for v in b]) * 1.4826 or 1e-4
    return d, abs(d) / (mad * math.sqrt(1 / len(a) + 1 / len(b)) * 1.25)


def ns_step(inc: list[tuple[datetime, float]], ref: datetime,
            th: Thresholds) -> tuple[datetime, float, float] | None:
    """The most significant inclination step inside the lookback: (when,
    step in deg, z). Medians over 48 hours: robust to provider scatter, and
    coarse in time, which the evidence block says."""
    if len(inc) < EW_MIN_POINTS:
        return None
    keys = [p[0] for p in inc]
    win = timedelta(hours=WIN_H)
    t = max(inc[0][0] + timedelta(hours=NS_FIRST_H), ref - timedelta(hours=LOOKBACK_H))
    last = inc[-1][0] - timedelta(hours=NS_LAST_H)
    best: tuple[datetime, float, float] | None = None
    while t <= last:
        b = [v for _, v in _between(inc, keys, t - win, t)]
        a = [v for _, v in _between(inc, keys, t, t + win)]
        if len(b) >= NS_BEFORE_MIN and len(a) >= NS_AFTER_MIN:
            d, z = _ns_score(b, a)
            if abs(d) > th.ns_floor and z > th.sigma and (best is None or z > best[2]):
                best = (t, d, z)
        t += timedelta(hours=STEP_H)
    return best


def _departs(lon: list[tuple[datetime, float]], keys: list[datetime],
             now: datetime, since: datetime | None, th: Thresholds) -> bool:
    lo = now - timedelta(hours=EARLY_TRAIN_H)
    if since is not None and since > lo:
        lo = since
    train = _between(lon, keys, lo, now - timedelta(hours=EARLY_FRESH_H))
    fresh = _between(lon, keys, now - timedelta(hours=EARLY_FRESH_H),
                     now + timedelta(seconds=1))[-EARLY_FRESH:]
    if len(train) < EARLY_TRAIN_MIN or len(fresh) < EARLY_FRESH:
        return False
    origin = train[0][0]
    xs = [(w - origin).total_seconds() / 86400.0 for w, _ in train]
    if not resolves_libration(xs):
        return False
    fitted = model_coefficients(xs, [v for _, v in train], True)
    if fitted is None:
        return False
    coeffs, rms = fitted
    dev = statistics.median(
        abs(v - model_value(coeffs, (w - origin).total_seconds() / 86400.0))
        for w, v in fresh)
    return dev > max(EARLY_DEV_FLOOR, th.sigma * rms)


def first_departure(lon: list[tuple[datetime, float]], since: datetime | None,
                    th: Thresholds) -> datetime | None:
    """Early warning: the first hourly check, over the last EARLY_SCAN_H
    hours of fixes, at which the newest fixes depart from the track fitted to
    the ones before them. `since` keeps the training data after the most
    recent confirmed event, because a track fitted across a burn predicts
    nothing."""
    if len(lon) < EARLY_TRAIN_MIN:
        return None
    keys = [p[0] for p in lon]
    now = lon[-1][0] - timedelta(hours=EARLY_SCAN_H)
    while now <= lon[-1][0]:
        if _departs(lon, keys, now, since, th):
            return now
        now += timedelta(hours=1)
    return None


def _cadence_hours(rows: list[tuple[datetime, float]]) -> float | None:
    if len(rows) < QUIET_CADENCE_MIN:
        return None
    steps = [_hours(a[0], b[0]) for a, b in zip(rows, rows[1:])]
    return statistics.median(steps)


def _thin(rows: list[tuple[datetime, float]]) -> list[list[Any]]:
    stride = max(1, math.ceil(len(rows) / EVIDENCE_MAX_POINTS))
    picked = rows[::stride]
    if rows and picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return [[w.isoformat(), round(v, 5)] for w, v in picked]


def _line(fit: DriftFit, lo: datetime, hi: datetime) -> dict:
    """The fitted line's end points, in the same unwrapped frame as the series."""
    origin = fit.origin or lo

    def at(w: datetime) -> float:
        return fit.intercept_deg + fit.rate_deg_per_day * (w - origin).total_seconds() / 86400.0

    return {"t0": lo.isoformat(), "y0": round(at(lo), 5),
            "t1": hi.isoformat(), "y1": round(at(hi), 5),
            "rate_deg_per_day": round(fit.rate_deg_per_day, 4),
            "residual_deg": round(fit.residual_deg, 4),
            "points": fit.points, "harmonic": fit.harmonic}


@dataclass(frozen=True)
class _Object:
    """One object as the detectors see it: the samples of the ONE provider
    the agreement check chose (1.7.5), never a blend. `heard` is every epoch
    at which any provider not set aside reported it, which is what silence
    is judged on. `trusted` is False when the providers disagree or the
    samples have no provider; such an object raises no manoeuvre event."""
    oid: str
    rec: dict
    lon: list[tuple[datetime, float]]
    inc: list[tuple[datetime, float]]
    heard: list[datetime] = field(default_factory=list)
    trusted: bool = True


def _base(obj: _Object, kind: str, tier: str, when: datetime,
          known: datetime | None = None, key: str | None = None) -> dict:
    """`detectable_from` is when the event first COULD have been reported: a
    burn is only confirmable once a day of fixes follows it. "New since you
    last looked" is judged against this, not the burn epoch, or a burn that
    happened before the operator's last visit but was confirmed after it would
    be filed as old news."""
    return {"id": f"{obj.oid}:{kind}:{key or when.isoformat()}",
            "object_id": obj.oid, "type": kind, "tier": tier,
            "epoch": when.isoformat(),
            "detectable_from": (known or when).isoformat(),
            "data_mode": obj.rec.get("data_mode", "REAL")}


def _ew_event(obj: _Object, split: _Split, ref: datetime, th: Thresholds) -> dict:
    rb, ra = split.before.rate_deg_per_day, split.after.rate_deg_per_day
    win = timedelta(hours=WIN_H)
    cross = ew_crossing(obj.lon, split.when)
    when = cross.when if cross is not None else split.when
    # Reportable once a day of fixes follows the burn AS TIMED and the split
    # the detector confirmed it at, whichever is later.
    ev = _base(obj, ew_kind(rb, ra, th.ew_floor), CONFIRMED, when,
               max(when, split.when) + timedelta(hours=EW_MIN_AFTER_H), hour_key(when))
    ev.update({
        "rate_before": round(rb, 4), "rate_after": round(ra, 4), "z": round(split.z, 1),
        "evidence": {
            "quantity": "longitude",
            "series": _thin([p for p in obj.lon if p[0] >= ref - timedelta(hours=LOOKBACK_H)]),
            "split": split.when.isoformat(),
            "before": _line(split.before, split.when - win, split.when),
            "after": _line(split.after, split.when, min(split.when + win, obj.lon[-1][0])),
            "method": "two independent 48 h fits, libration modelled where the sampling resolves it",
            "timed_by": "fit crossing" if cross is not None else "3-hour split",
            "crossing": None if cross is None else {
                "epoch": cross.when.isoformat(),
                "sigma_hours": round(cross.sigma_hours, 3),
                "half_width_hours": round(CROSS_COVERAGE_K * cross.sigma_hours, 2),
                "coverage": 0.95,
                "method": "where the before and after trends meet, refined about the estimate",
            },
        },
    })
    return ev


def _ns_event(obj: _Object, found: tuple[datetime, float, float], ref: datetime) -> dict:
    when, step, z = found
    win = timedelta(hours=WIN_H)
    keys = [p[0] for p in obj.inc]
    before = [v for _, v in _between(obj.inc, keys, when - win, when)]
    after = [v for _, v in _between(obj.inc, keys, when, when + win)]
    ev = _base(obj, "NS", CONFIRMED, when, when + timedelta(hours=NS_LAST_H))
    ev.update({
        "delta_inc_deg": round(step, 4), "z": round(z, 1),
        "evidence": {
            "quantity": "inclination",
            "series": _thin([p for p in obj.inc if p[0] >= ref - timedelta(hours=LOOKBACK_H)]),
            "split": when.isoformat(),
            "before": {"t0": (when - win).isoformat(), "t1": when.isoformat(),
                       "y0": round(statistics.median(before), 5),
                       "y1": round(statistics.median(before), 5)},
            "after": {"t0": when.isoformat(), "t1": min(when + win, obj.inc[-1][0]).isoformat(),
                      "y0": round(statistics.median(after), 5),
                      "y1": round(statistics.median(after), 5)},
            "method": "median step over 48 h windows; timing is coarse, of the order of hours",
        },
    })
    return ev


def _leaving_event(obj: _Object, when: datetime, ref: datetime) -> dict:
    ev = _base(obj, "LEAVING", SUSPECTED, when)
    ev["evidence"] = {
        "quantity": "longitude",
        "series": _thin([p for p in obj.lon if p[0] >= ref - timedelta(hours=LOOKBACK_H)]),
        "split": when.isoformat(),
        "method": "newest fixes depart from the track fitted to the 72 h before them; "
                  "confirms or clears once 24 h of fixes follow",
    }
    return ev


def _quiet_event(obj: _Object, belt_ref: datetime, th: Thresholds) -> dict | None:
    heard = obj.heard or [p[0] for p in obj.lon]
    if not heard:
        return None
    last = heard[-1]
    gap = _hours(last, belt_ref)
    if gap < th.quiet_hours or gap > LOOKBACK_H:
        return None
    cadence = _cadence_hours([(w, 0.0) for w in heard if w >= last - timedelta(hours=72)])
    if cadence is None or cadence > th.quiet_hours / QUIET_CADENCE_DIVISOR:
        return None
    ev = _base(obj, "QUIET", CONFIRMED, last, last + timedelta(hours=th.quiet_hours))
    ev.update({"age_hours": round(gap, 1), "usual_cadence_hours": round(cadence, 2)})
    return ev


def _object_events(obj: _Object, ref: datetime, th: Thresholds) -> list[dict]:
    out: list[dict] = []
    if not obj.trusted:
        return out
    split = ew_split(obj.lon, ref, th)
    confirmed = split if split is not None and not split.edge else None
    if confirmed is not None:
        out.append(_ew_event(obj, confirmed, ref, th))
    found = ns_step(obj.inc, ref, th)
    if found is not None:
        out.append(_ns_event(obj, found, ref))
    since = _parse(out[0]["epoch"]) if confirmed is not None else None
    departed = first_departure(obj.lon, since, th)
    if departed is not None and departed >= ref - timedelta(hours=LOOKBACK_H):
        out.append(_leaving_event(obj, departed, ref))
    return out


def _trusted(rec: dict) -> bool:
    block = (rec.get("agreement") or {}).get(rec.get("data_mode")) or {}
    return block.get("status") != "disputed" and not block.get("samples_untagged")


def _objects(data: dict, ref: datetime) -> list[_Object]:
    since = ref - timedelta(hours=HISTORY_H)
    out = []
    for oid, rec in (data.get("objects") or {}).items():
        samples = rec.get("samples") or []
        lon = unwrap(series(samples, "sub_lon_deg", since))
        heard = [w for w in (_parse(e) for e in rec.get("heard") or []) if w >= since]
        out.append(_Object(oid, rec, lon, series(samples, "inclination_deg", since),
                           heard, _trusted(rec)))
    return out


def _belt_reference(data: dict) -> datetime | None:
    """The newest fix held from any provider not set aside."""
    newest: datetime | None = None
    for rec in (data.get("objects") or {}).values():
        epochs = rec.get("heard") or [s["epoch"] for s in rec.get("samples") or [] if "epoch" in s]
        if epochs:
            when = _parse(max(epochs))
            if newest is None or when > newest:
                newest = when
    return newest


def detect(data: dict, th: Thresholds, *,
           now: datetime, stale_after_hours: float) -> dict:
    """Every event in the store, newest first, with the context to read them."""
    ref = _belt_reference(data)
    payload: dict = {"reference_epoch": ref.isoformat() if ref else None,
                     "lookback_hours": LOOKBACK_H,
                     "thresholds": {"ew_floor_deg_per_day": th.ew_floor,
                                    "ns_floor_deg": th.ns_floor, "sigma": th.sigma,
                                    "quiet_hours": th.quiet_hours,
                                    "max_residual_deg": th.max_residual},
                     "belt_current": False, "events": []}
    if ref is None:
        return payload
    belt_current = _hours(ref, now) <= stale_after_hours
    payload["belt_current"] = belt_current
    events: list[dict] = []
    # No "reaches X in N days" projection (removed 1.7.5, owner approval of
    # 17 November 2026): it extrapolated two fitted rates into a statement of
    # where an object WOULD be, which no provider issued.
    for obj in _objects(data, ref):
        events.extend(_object_events(obj, ref, th))
        quiet = _quiet_event(obj, ref, th) if belt_current else None
        if quiet is not None:
            events.append(quiet)
    events.sort(key=lambda e: e["epoch"], reverse=True)
    payload["events"] = events
    return payload


def latest_splits(events: list[dict]) -> dict[str, datetime]:
    """Object id -> epoch of its newest CONFIRMED E-W event. The drift rate on
    the plot and in the inspector is fitted after this point, never across it:
    a 72 h fit straddling a burn reported 0.22 deg/day for an object drifting
    at 0.59 deg/day (measured on the scenario store, 16 November 2026)."""
    out: dict[str, datetime] = {}
    for ev in events:
        if ev["type"] in KIND_EW and ev["tier"] == CONFIRMED:
            when = _parse(ev["epoch"])
            held = out.get(ev["object_id"])
            if held is None or when > held:
                out[ev["object_id"]] = when
    return out


class EventEngine:
    """Caches `detect` against the store's on-disk signature.

    The refresh thread calls `refresh` after every cycle, so requests normally
    find a warm cache. A request that arrives after some other write (a pull, a
    manual element set) recomputes once; every later request is a cache hit.
    `computations` counts the work so a test can prove it is not per request.
    """

    def __init__(self, th: Thresholds, stale_after_hours: float):
        self.th = th
        self.stale_after_hours = stale_after_hours
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._result: dict | None = None
        self.computations = 0

    @staticmethod
    def _signature(path: str) -> tuple:
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (None, None)

    def current(self, store: Any, now: datetime | None = None) -> dict:
        key = self._signature(store.path)
        with self._lock:
            if self._result is not None and key == self._key:
                return self._result
            started = time.perf_counter()
            result = detect(store.load(), self.th,
                            now=now or datetime.now(timezone.utc),
                            stale_after_hours=self.stale_after_hours)
            result["compute_seconds"] = round(time.perf_counter() - started, 3)
            self.computations += 1
            self._key, self._result = key, result
            return result
