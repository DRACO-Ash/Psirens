"""Closest-approach screening and TLE export for stored objects.

Closest approach is a coarse time scan refined around the minimum, computing
the 3D TEME range between two states propagated by the same validated Vallado
SGP4 (`sgp4`) already used for sub-satellite longitude. Nothing here is a
hand-rolled propagator.

TLE lines are served ONLY as the provider issued them: UDL carries
`line1`/`line2` on its elset records, and those are the provider's own fix.
Until 1.7.4 a record without a usable native pair got a TLE rebuilt by
`sgp4.exporter`, with element number, rev number, ephemeris type, n-dot and
n-double-dot written in by this module. That was a TLE no provider issued, and
under the owner's rule of 17 November 2026 it is gone: no line is shown, and
the provenance says why.

Propagation is refused, with the reason, when the record does not state an
SGP4 ephemeris type or does not carry its own B* (`tle.propagation_refusal`).
Nothing is defaulted to make an element set screenable.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from sgp4.api import WGS72, Satrec, jday

from .astro import _days_since_1949
from .models import display_name
from .tle import (DEFAULT_PRIORITY, propagation_refusal, provenance_of, select_elset,
                  validate_native)

_log = logging.getLogger("psirens.conjunction")

_TWO_PI = 2.0 * math.pi
GEO_RADIUS_KM = 42164.0  # for reference/labels only, not used in the range calc


def _parse_epoch(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _satnum(sat_no) -> int:
    try:
        return int(str(sat_no))
    except (TypeError, ValueError):
        return 0  # non-numeric ids export with a zero catalogue number


NO_NATIVE_LINE = "no TLE line was issued by the provider for this element set"


def build_satrec(el: dict) -> Satrec:
    """Build an SGP4 Satrec from the record's own mean elements. Angles in the
    elset are degrees; sgp4init wants radians and mean motion in rad/min.

    Callers check `tle.propagation_refusal` first. B* is read from the record
    and never defaulted; n-dot and n-double-dot are passed as zero because
    SGP4 does not use them in propagation (they exist only in the TLE text).
    """
    epoch = _parse_epoch(el["epoch"])
    no_kozai = float(el["mean_motion_rev_per_day"]) * _TWO_PI / 1440.0
    s = Satrec()
    s.sgp4init(
        WGS72, "i", _satnum(el.get("sat_no")), _days_since_1949(epoch),
        float(el["bstar"]), 0.0, 0.0,
        float(el["eccentricity"]), math.radians(float(el["argp_deg"])),
        math.radians(float(el["inclination_deg"])),
        math.radians(float(el["mean_anomaly_deg"])), no_kozai,
        math.radians(float(el["raan_deg"])),
    )
    return s


def tle_for(obj: dict,
            priority: tuple[str, ...] = DEFAULT_PRIORITY) -> tuple[dict | None, list[str] | None, dict | None]:
    """Choose an element set for an object and return the provider's own lines.

    Returns `(elset, lines, provenance)`. `lines` is the native pair, or None
    when the provider issued none; the provenance then says so. The returned
    element set is also what the caller propagates, when it may be propagated
    at all (`provenance["propagation_refused"]` is None).
    """
    el = select_elset(obj, priority)
    if el is None:
        return None, None, None
    native = validate_native(el.get("line1"), el.get("line2"), el.get("sat_no"))
    prov = provenance_of(el, native=native is not None)
    if native is None:
        prov["tle_withheld_reason"] = NO_NATIVE_LINE
        return el, None, prov
    return el, list(native), prov


def _jday(dt: datetime) -> tuple[float, float]:
    return jday(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                dt.second + dt.microsecond / 1_000_000.0)


def _range_km(sa: Satrec, sb: Satrec, jd: float, fr: float) -> float | None:
    ea, ra, _ = sa.sgp4(jd, fr)
    eb, rb, _ = sb.sgp4(jd, fr)
    if ea != 0 or eb != 0:
        return None
    return math.sqrt((ra[0] - rb[0]) ** 2 + (ra[1] - rb[1]) ** 2 + (ra[2] - rb[2]) ** 2)


def _min_over(sa: Satrec, sb: Satrec, times: list[datetime]) -> tuple[float, datetime | None]:
    best_d, best_t = math.inf, None
    for t in times:
        jd, fr = _jday(t)
        d = _range_km(sa, sb, jd, fr)
        if d is not None and d < best_d:
            best_d, best_t = d, t
    return best_d, best_t


def closest_approach(sa: Satrec, sb: Satrec, start: datetime, *,
                     window_hours: int = 168, coarse_min: int = 30,
                     fine_min: int = 1) -> tuple[float | None, datetime | None, float | None]:
    """Minimum 3D range (km) and its time between two objects over the window,
    plus the current range. Coarse scan then a fine refinement around the
    coarse minimum. Returns (min_km, tca, range_now_km); None entries on
    propagation failure."""
    coarse = [start + timedelta(minutes=coarse_min * i)
              for i in range(int(window_hours * 60 / coarse_min) + 1)]
    best = _min_over(sa, sb, coarse)
    if best[1] is not None:
        lo = best[1] - timedelta(minutes=coarse_min)
        end = start + timedelta(hours=window_hours)
        fine = [lo + timedelta(minutes=fine_min * i)
                for i in range(int(2 * coarse_min / fine_min) + 1)]
        fine = [t for t in fine if start <= t <= end]
        fbest = _min_over(sa, sb, fine)
        if fbest[1] is not None and fbest[0] < best[0]:
            best = fbest
    jd, fr = _jday(start)
    now_r = _range_km(sa, sb, jd, fr)
    min_km = None if best[1] is None else best[0]
    return min_km, best[1], now_r


def _sub_lon(obj: dict) -> float | None:
    samples = obj.get("samples") or []
    return samples[-1]["sub_lon_deg"] if samples else None


def _sep_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _within(lon: float | None, ref: float | None, half_width_deg: float) -> bool:
    return lon is not None and ref is not None and _sep_deg(lon, ref) <= half_width_deg


def _neighbour_entry(target_sat: Satrec, oid: str, obj: dict, start: datetime,
                     window_hours: int,
                     priority: tuple[str, ...] = DEFAULT_PRIORITY,
                     names: dict | None = None) -> dict | None:
    el, lines, prov = tle_for(obj, priority)
    if not el or prov is None or prov["propagation_refused"] is not None:
        return None
    try:
        sat = build_satrec(el)
    except (ValueError, OverflowError, KeyError, TypeError):
        return None
    min_km, tca, now_km = closest_approach(target_sat, sat, start,
                                            window_hours=window_hours)
    entry = {
        "id": oid,
        "name": display_name(oid, obj.get("name"), names),
        "sub_lon_deg": _sub_lon(obj),
        "range_now_km": None if now_km is None else round(now_km, 1),
        "min_km": None if min_km is None else round(min_km, 1),
        "tca": tca.astimezone(timezone.utc).isoformat() if tca else None,
        "tle": lines,
        "tle_provenance": prov,
    }
    return entry


def _no_target(now: datetime, window_hours: int, detail: str) -> dict:
    """The empty-but-valid payload. Always JSON, always with a stated reason:
    the interface can render a cause rather than a shrug."""
    return {"target": None, "neighbours": [], "window_hours": window_hours,
            "computed_at": now.isoformat(), "detail": detail}


def conjunctions_for(store_data: dict, target_id: str, *,
                     half_width_deg: float = 10.0, window_hours: int = 168,
                     cap: int = 20, now: datetime | None = None,
                     priority: tuple[str, ...] = DEFAULT_PRIORITY,
                     names: dict | None = None) -> dict:
    """Compute closest approach and TLEs for the target and every stored object
    within +/-half_width longitude that carries an elset. Neighbours are sorted
    by current range (nearest first) and capped.

    `priority` is the TLE source selection policy; it decides which provider's
    element set is served and screened from where several hold a fix.
    """
    now = now or datetime.now(timezone.utc)
    objects = store_data.get("objects", {})
    target = objects.get(target_id)
    t_el, t_tle, t_prov = tle_for(target or {}, priority)
    if target is None or t_el is None or t_prov is None:
        return _no_target(now, window_hours,
                          "target has no marked element set from a provider")
    refused = t_prov["propagation_refused"]
    if refused is not None:
        # The provider's own line is still shown; only the screen is refused.
        out = _no_target(now, window_hours, f"not screened: {refused}")
        out["target"] = {"id": target_id,
                         "name": display_name(target_id, target.get("name"), names),
                         "tle": t_tle, "tle_provenance": t_prov}
        return out
    try:
        target_sat = build_satrec(t_el)
    except (ValueError, OverflowError, KeyError, TypeError) as exc:
        # Neighbours have always been guarded this way; the target was not,
        # so one malformed record took the whole request down as an
        # unhandled 500 with a non-JSON body, which the SPA could only
        # report as "service unavailable".
        _log.warning("target %s has an unusable element set: %s", target_id, exc)
        return _no_target(now, window_hours, "target element set is unusable")
    near = {oid: obj for oid, obj in objects.items()
            if oid != target_id and _within(_sub_lon(obj), _sub_lon(target), half_width_deg)}
    neighbours: list[dict] = []
    not_screened = 0
    for oid, obj in near.items():
        entry = _neighbour_entry(target_sat, oid, obj, now, window_hours,
                                 priority, names)
        if entry is None:
            not_screened += 1
        else:
            neighbours.append(entry)
    neighbours.sort(key=lambda e: (e["range_now_km"] is None, e["range_now_km"] or 0.0))
    return {
        "target": {"id": target_id,
                   "name": display_name(target_id, target.get("name"), names),
                   "tle": t_tle, "tle_provenance": t_prov},
        "neighbours": neighbours[:cap],
        # Nearby objects whose element set could not be propagated without an
        # assumption (no stated SGP4 type, no B*, or none at all). Counted and
        # said, so an empty neighbourhood is never mistaken for an empty belt.
        "not_screened": not_screened,
        "window_hours": window_hours,
        "computed_at": now.isoformat(),
    }
