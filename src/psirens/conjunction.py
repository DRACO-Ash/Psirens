"""Closest-approach screening and TLE export for stored objects.

Closest approach is a coarse time scan refined around the minimum, computing
the 3D TEME range between two states propagated by the same validated Vallado
SGP4 (`sgp4`) already used for sub-satellite longitude. Nothing here is a
hand-rolled propagator.

TLE lines are served NATIVE where the provider pushed one: UDL carries
`line1`/`line2` on its elset records, and those are the provider's own fix.
Reconstruction via `sgp4.exporter.export_tle` remains as the fallback for
records without a usable native line, and is labelled as such in the
provenance so a reconstruction is never mistaken for a provider fix. Which
provider's element set is chosen, when several hold a fix for one object, is
decided by the selection policy in `tle.py`.

Inputs are the stored element sets (see `tle.candidates_of`). An object
without one is simply skipped: it cannot be propagated, and this module never
invents elements.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from sgp4.api import WGS72, Satrec, jday
from sgp4.exporter import export_tle

from .astro import _days_since_1949
from .tle import DEFAULT_PRIORITY, provenance_of, select_elset, validate_native

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


def _classification(marking: str) -> str:
    first = (str(marking or "U").strip() or "U")[0].upper()
    return first if first in ("U", "C", "S", "T") else "U"


def build_satrec(el: dict) -> Satrec:
    """Build an SGP4 Satrec from stored mean elements. Angles in the elset are
    degrees; sgp4init wants radians and mean motion in rad/min."""
    epoch = _parse_epoch(el["epoch"])
    no_kozai = float(el["mean_motion_rev_per_day"]) * _TWO_PI / 1440.0
    s = Satrec()
    s.sgp4init(
        WGS72, "i", _satnum(el.get("sat_no")), _days_since_1949(epoch),
        float(el.get("bstar", 0.0)), 0.0, 0.0,
        float(el["eccentricity"]), math.radians(float(el["argp_deg"])),
        math.radians(float(el["inclination_deg"])),
        math.radians(float(el["mean_anomaly_deg"])), no_kozai,
        math.radians(float(el["raan_deg"])),
    )
    # attributes export_tle needs beyond the orbital init
    s.classification = _classification(el.get("classification", "U"))
    s.intldesg = str(el.get("intl_desig", "") or "")[:8]
    s.elnum = 1
    s.revnum = 0
    s.ephtype = 0
    return s


def tle_lines(el: dict) -> tuple[str, str] | None:
    """Export a classic two-line element set for one object, or None on failure.

    This is now the FALLBACK path, used only where the provider pushed no
    usable native line. Note the loss it implies: the international designator
    and rev number are not carried by the UDL elset and export blank/zero. The
    orbital fields and checksums are exact.
    """
    try:
        return export_tle(build_satrec(el))
    except (ValueError, OverflowError, KeyError, TypeError) as exc:
        _log.warning("TLE export failed for %s: %s", el.get("sat_no"), exc)
        return None


def tle_for(obj: dict,
            priority: tuple[str, ...] = DEFAULT_PRIORITY) -> tuple[dict | None, list[str] | None, dict | None]:
    """Choose an element set for an object and produce its two-line set.

    Returns `(elset, lines, provenance)`. The native provider line wins where
    one survives revalidation; otherwise the lines are reconstructed and the
    provenance says so, so the interface never presents a reconstruction as a
    provider fix.

    The returned element set is also what the caller propagates. Serving one
    provider's line while screening from another's would put a range on screen
    that the displayed elements do not produce, which is worse than either
    choice made consistently.
    """
    el = select_elset(obj, priority)
    if el is None:
        return None, None, None
    native = validate_native(el.get("line1"), el.get("line2"), el.get("sat_no"))
    if native is not None:
        return el, list(native), provenance_of(el, native=True)
    lines = tle_lines(el)
    return el, (list(lines) if lines else None), provenance_of(el, native=False)


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


def _neighbour_entry(target_sat: Satrec, oid: str, obj: dict, start: datetime,
                     window_hours: int,
                     priority: tuple[str, ...] = DEFAULT_PRIORITY) -> dict | None:
    el, lines, prov = tle_for(obj, priority)
    if not el:
        return None
    try:
        sat = build_satrec(el)
    except (ValueError, OverflowError, KeyError, TypeError):
        return None
    min_km, tca, now_km = closest_approach(target_sat, sat, start,
                                            window_hours=window_hours)
    entry = {
        "id": oid,
        "name": obj.get("name", oid),
        "sub_lon_deg": _sub_lon(obj),
        "range_now_km": None if now_km is None else round(now_km, 1),
        "min_km": None if min_km is None else round(min_km, 1),
        "tca": tca.astimezone(timezone.utc).isoformat() if tca else None,
        "tle": lines,
        "tle_provenance": prov,
    }
    return entry


def conjunctions_for(store_data: dict, target_id: str, *,
                     half_width_deg: float = 10.0, window_hours: int = 168,
                     cap: int = 20, now: datetime | None = None,
                     priority: tuple[str, ...] = DEFAULT_PRIORITY) -> dict:
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
    if target is None or t_el is None:
        return {"target": None, "neighbours": [], "window_hours": window_hours,
                "computed_at": now.isoformat(),
                "detail": "target has no element set"}
    target_sat = build_satrec(t_el)
    t_lon = _sub_lon(target)
    neighbours: list[dict] = []
    for oid, obj in objects.items():
        if oid == target_id:
            continue
        lon = _sub_lon(obj)
        if lon is None or t_lon is None or _sep_deg(lon, t_lon) > half_width_deg:
            continue
        entry = _neighbour_entry(target_sat, oid, obj, now, window_hours, priority)
        if entry is not None:
            neighbours.append(entry)
    neighbours.sort(key=lambda e: (e["range_now_km"] is None, e["range_now_km"] or 0.0))
    return {
        "target": {"id": target_id, "name": target.get("name", target_id),
                   "tle": t_tle, "tle_provenance": t_prov},
        "neighbours": neighbours[:cap],
        "window_hours": window_hours,
        "computed_at": now.isoformat(),
    }
