"""Astrodynamics: sub-satellite longitude and drift rate from a mean elset.

Validated numerically (see tests/test_astro.py):
  * GMST against Vallado's 1992-08-20 worked example (152.5788 deg).
  * Sub-satellite longitude against derivable geostationary cases
    (angle-sum == GMST -> 0 deg; +90 deg -> +90 deg east) to < 0.01 deg.

We propagate with the standard Vallado SGP4 (the `sgp4` package) rather than a
hand-rolled propagator, deliberately: an in-house closed-form substitution is
exactly the class of defect that produced a large velocity error elsewhere in
the estate, and is not worth repeating for a plot. Recorded dependency reason.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sgp4.api import WGS72, Satrec
from sgp4.functions import jday

_log = logging.getLogger("psirens.astro")

_EPOCH_1949 = datetime(1949, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
_TWO_PI = 2.0 * math.pi


def gmst_rad(jd_ut1: float) -> float:
    """Greenwich Mean Sidereal Time in radians (IAU-82, Vallado).

    UT1 is approximated by UTC; the sub-degree UT1-UTC offset is negligible
    for a belt-knowledge plot and is not corrected here (stated, not hidden).
    """
    tut1 = (jd_ut1 - 2451545.0) / 36525.0
    gmst_sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * tut1
        + 0.093104 * tut1 * tut1
        - 6.2e-6 * tut1 * tut1 * tut1
    )
    gmst = math.radians((gmst_sec % 86400.0) / 240.0)  # 240 s of time == 1 deg
    return gmst % _TWO_PI


def _days_since_1949(epoch: datetime) -> float:
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    return (epoch - _EPOCH_1949).total_seconds() / 86400.0


def sub_longitude_deg(
    *,
    inclination_deg: float,
    eccentricity: float,
    raan_deg: float,
    argp_deg: float,
    mean_anomaly_deg: float,
    mean_motion_rev_per_day: float,
    bstar: float,
    epoch: datetime,
) -> float | None:
    """Sub-satellite longitude in degrees, range (-180, 180], or None on failure.

    Builds an SGP4 satellite record directly from mean elements, propagates to
    the elset epoch (tsince = 0), and rotates the TEME position into an
    Earth-fixed frame by GMST to read off the sub-point longitude.
    """
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    no_kozai = mean_motion_rev_per_day * _TWO_PI / 1440.0  # rad / minute
    sat = Satrec()
    try:
        sat.sgp4init(
            WGS72,
            "i",
            0,
            _days_since_1949(epoch),
            float(bstar),
            0.0,
            0.0,
            float(eccentricity),
            math.radians(argp_deg),
            math.radians(inclination_deg),
            math.radians(mean_anomaly_deg),
            no_kozai,
            math.radians(raan_deg),
        )
    except (ValueError, OverflowError) as exc:  # pragma: no cover - defensive
        _log.warning("sgp4init failed: %s", exc)
        return None

    jd, fr = jday(
        epoch.year,
        epoch.month,
        epoch.day,
        epoch.hour,
        epoch.minute,
        epoch.second + epoch.microsecond / 1_000_000.0,
    )
    err, r, _v = sat.sgp4(jd, fr)
    if err != 0:
        _log.debug("sgp4 propagation error code %s for epoch %s", err, epoch)
        return None

    theta = gmst_rad(jd + fr)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    x_ecef = r[0] * cos_t + r[1] * sin_t
    y_ecef = -r[0] * sin_t + r[1] * cos_t
    lon = math.degrees(math.atan2(y_ecef, x_ecef))
    return (lon + 180.0) % 360.0 - 180.0


def drift_deg_per_day(samples: list[tuple[datetime, float]]) -> float | None:
    """Longitude drift rate (deg/day) from the two most recent samples.

    Uses shortest angular difference so a wrap across the +/-180 seam does not
    register as a spurious ~360 deg/day jump. Returns None with < 2 samples.
    """
    if len(samples) < 2:
        return None
    (t0, lon0), (t1, lon1) = samples[-2], samples[-1]
    dt_days = (t1 - t0).total_seconds() / 86400.0
    if abs(dt_days) < 1e-9:
        return None
    dlon = (lon1 - lon0 + 180.0) % 360.0 - 180.0
    return dlon / dt_days
