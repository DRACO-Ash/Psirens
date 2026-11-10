"""Coplanar-angle screening for the GEO belt.

The coplanar angle between two orbits is the angle between their orbital planes,
i.e. the mutual (relative) inclination. From each orbit's inclination i and right
ascension of the ascending node (RAAN) Omega:

    cos(theta) = cos(i1)cos(i2) + sin(i1)sin(i2)cos(Omega1 - Omega2)

theta = 0 means the two objects share an orbital plane (co-planar). A small theta
combined with a small longitude separation is the classic co-orbital / RPO
geometry an operator watches for: same plane, same slot, able to close.

The formula is the angle between the two orbit-normal unit vectors; it is
validated in the tests against that vector method to < 1e-9 deg, and against the
closed cases (identical planes -> 0; equal inclination, opposite nodes -> 2i;
equatorial vs i -> i).

Inputs are the stored `elset` dicts (latest mean elements per object) and the
latest sample's sub-satellite longitude. Both are already retained. An object
without an elset is skipped: it cannot define a plane, and this module never
invents one. History tracks (more than one point per object) become available
once RAAN is retained per sample; the payload shape already carries a points
list so that is a data-only upgrade, not an interface change.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import display_name


def coplanar_angle_deg(inc1: float, raan1: float,
                       inc2: float, raan2: float) -> float:
    """Angle between two orbital planes (deg), from inclinations and RAANs."""
    ia, ib = math.radians(inc1), math.radians(inc2)
    cos_theta = (math.cos(ia) * math.cos(ib)
                 + math.sin(ia) * math.sin(ib) * math.cos(math.radians(raan1 - raan2)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))


def longitude_difference_deg(lon_a: float, lon_b: float) -> float:
    """Shortest absolute sub-satellite longitude separation (0..180 deg)."""
    return abs((lon_a - lon_b + 180.0) % 360.0 - 180.0)


@dataclass(frozen=True)
class OrbitPlane:
    """An orbital plane, defined by inclination and RAAN (degrees)."""
    inclination_deg: float
    raan_deg: float

    def normal(self) -> tuple[float, float, float]:
        """Unit normal to the plane (the orbit angular-momentum direction)."""
        i, o = math.radians(self.inclination_deg), math.radians(self.raan_deg)
        return (math.sin(i) * math.sin(o), -math.sin(i) * math.cos(o), math.cos(i))

    def angle_to(self, other: "OrbitPlane") -> float:
        return coplanar_angle_deg(self.inclination_deg, self.raan_deg,
                                  other.inclination_deg, other.raan_deg)


@dataclass
class CoplanarPoint:
    coplanar_deg: float
    lon_diff_deg: float
    epoch: str | None = None


@dataclass
class CoplanarTrack:
    object_id: str
    name: str
    points: list[CoplanarPoint]   # oldest -> newest; length 1 until per-sample RAAN
    group: str | None = None      # constellation/family, for marker shape (optional)

    @property
    def latest(self) -> CoplanarPoint:
        return self.points[-1]


def _plane_of(obj: dict) -> OrbitPlane | None:
    el = obj.get("elset")
    if not el:
        return None
    try:
        return OrbitPlane(float(el["inclination_deg"]), float(el["raan_deg"]))
    except (KeyError, TypeError, ValueError):
        return None


def _sub_lon(obj: dict) -> float | None:
    samples = obj.get("samples") or []
    return samples[-1]["sub_lon_deg"] if samples else None


def _latest_epoch(obj: dict) -> str | None:
    samples = obj.get("samples") or []
    return samples[-1].get("epoch") if samples else None


def _parse_epoch(raw) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _plane_rows(obj: dict) -> list[tuple]:
    """(when, plane, sub_lon, epoch) for every sample that carries RAAN.

    Samples written before 1.6.17 have no `raan_deg` and are SKIPPED, not
    guessed at. Inclination alone cannot define a plane, and holding RAAN at
    its present value to fill the gap would draw a confident line through a
    quantity nobody measured. A shorter honest trail beats a long invented one.
    """
    rows = []
    for s in obj.get("samples") or []:
        raan, inc, lon = s.get("raan_deg"), s.get("inclination_deg"), s.get("sub_lon_deg")
        if raan is None or inc is None or lon is None:
            continue
        when = _parse_epoch(s.get("epoch"))
        if when is not None:
            rows.append((when, OrbitPlane(float(inc), float(raan)),
                         float(lon), s.get("epoch")))
    rows.sort(key=lambda r: r[0])
    return rows


def _nearest_row(rows: list[tuple], times: list[datetime],
                 when: datetime, tol: timedelta) -> tuple | None:
    """The row closest in time to `when`, or None if the closest is beyond tol.

    Two objects are sampled at different epochs by different providers, so a
    co-planar point is only honest when both planes were measured at about the
    same time. Outside the tolerance there is no point, rather than a point
    pairing readings hours apart.
    """
    i = bisect.bisect_left(times, when)
    best = None
    for j in (i - 1, i):
        if j < 0 or j >= len(rows):
            continue
        gap = abs(rows[j][0] - when)
        if best is None or gap < best[0]:
            best = (gap, rows[j])
    if best is None or best[0] > tol:
        return None
    return best[1]


def _trail(p_rows: list[tuple], p_times: list[datetime], obj: dict,
           pair_hours: float, max_points: int, skip_epoch: str | None) -> list:
    """Historic co-planar points for one neighbour, oldest first."""
    rows = _plane_rows(obj)
    if not rows or not p_rows:
        return []
    tol = timedelta(hours=pair_hours)
    out = []
    for when, plane, lon, epoch in rows[-max_points:]:
        if epoch == skip_epoch:
            continue           # the head point below already covers this epoch
        match = _nearest_row(p_rows, p_times, when, tol)
        if match is None:
            continue
        out.append(CoplanarPoint(round(match[1].angle_to(plane), 4),
                                 round(longitude_difference_deg(lon, match[2]), 4),
                                 epoch))
    return out


def coplanar_for(store_data: dict, primary_id: str, *,
                 half_width_deg: float = 20.0, cap: int = 40,
                 now: datetime | None = None,
                 names: dict | None = None,
                 max_points: int = 60, pair_hours: float = 6.0) -> dict:
    """Coplanar angle and absolute longitude difference of every stored object
    within +/-half_width longitude of the primary that carries an element set.
    Sorted nearest-plane first (then nearest in longitude) and capped."""
    now = now or datetime.now(timezone.utc)
    objects = store_data.get("objects", {})
    primary = objects.get(primary_id)
    if primary is None:
        return {"primary": None, "tracks": [], "half_width_deg": half_width_deg,
                "computed_at": now.isoformat(), "detail": "unknown primary"}
    p_plane = _plane_of(primary)
    p_lon = _sub_lon(primary)
    if p_plane is None or p_lon is None:
        return {"primary": None, "tracks": [], "half_width_deg": half_width_deg,
                "computed_at": now.isoformat(),
                "detail": "primary has no element set"}
    # The primary's own sampled planes, for pairing the history against.
    p_rows = _plane_rows(primary)
    p_times = [r[0] for r in p_rows]
    tracks: list[CoplanarTrack] = []
    for oid, obj in objects.items():
        if oid == primary_id:
            continue
        plane = _plane_of(obj)
        lon = _sub_lon(obj)
        if plane is None or lon is None:
            continue
        lon_diff = longitude_difference_deg(lon, p_lon)
        if lon_diff > half_width_deg:
            continue
        # The HEAD point stays elset-derived: the newest fix overall, the same
        # source the plot head and the RA needle use. That invariant is why
        # changing TLE_SOURCE_PRIORITY cannot move the plot and this chart
        # apart, and a sample-derived head would quietly break it.
        head = CoplanarPoint(round(p_plane.angle_to(plane), 4),
                             round(lon_diff, 4), _latest_epoch(obj))
        trail = _trail(p_rows, p_times, obj, pair_hours, max_points,
                       _latest_epoch(obj))
        tracks.append(CoplanarTrack(
            oid, display_name(oid, obj.get("name"), names), trail + [head]))
    tracks.sort(key=lambda t: (t.latest.coplanar_deg, t.latest.lon_diff_deg))
    return {
        "primary": {
            "id": primary_id,
            "name": display_name(primary_id, primary.get("name"), names),
            "inclination_deg": round(p_plane.inclination_deg, 4),
            "raan_deg": round(p_plane.raan_deg, 4),
            "sub_lon_deg": round(p_lon, 4),
        },
        "tracks": [
            {"id": t.object_id, "name": t.name, "group": t.group,
             "points": [{"coplanar_deg": p.coplanar_deg,
                         "lon_diff_deg": p.lon_diff_deg, "epoch": p.epoch}
                        for p in t.points]}
            for t in tracks[:cap]
        ],
        "half_width_deg": half_width_deg,
        "computed_at": now.isoformat(),
    }
