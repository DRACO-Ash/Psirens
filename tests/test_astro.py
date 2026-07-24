"""Validated-physics tests. These are the assertions that keep the orbital
maths honest; a regression here must fail loudly."""

import math
from datetime import datetime, timedelta, timezone

from sgp4.functions import jday

from psirens.astro import drift_deg_per_day, gmst_rad, sub_longitude_deg


def test_gmst_matches_vallado_worked_example():
    # Vallado, 1992-08-20 12:14:00 UT1 -> GMST ~ 152.578787810 deg.
    jd, fr = jday(1992, 8, 20, 12, 14, 0.0)
    deg = math.degrees(gmst_rad(jd + fr))
    assert abs(deg - 152.578787810) < 1e-3


def _geo(angle_sum_deg, epoch):
    # i~0, e~0, GEO mean motion; place argument-of-latitude at angle_sum.
    return sub_longitude_deg(
        inclination_deg=0.001, eccentricity=0.0001,
        raan_deg=angle_sum_deg % 360.0, argp_deg=0.0, mean_anomaly_deg=0.0,
        mean_motion_rev_per_day=1.0027379093, bstar=0.0, epoch=epoch,
    )


def test_sublon_geostationary_derivable_cases():
    epoch = datetime(2026, 7, 1, tzinfo=timezone.utc)
    jd, fr = jday(2026, 7, 1, 0, 0, 0.0)
    gmst = math.degrees(gmst_rad(jd + fr))
    assert abs(_geo(gmst, epoch) - 0.0) < 0.02          # angle sum == GMST -> 0 deg
    assert abs(_geo(gmst + 90.0, epoch) - 90.0) < 0.02  # +90 deg -> +90 east


def test_sublon_in_range_and_not_none_for_typical_geo():
    epoch = datetime(2026, 7, 1, tzinfo=timezone.utc)
    lon = sub_longitude_deg(
        inclination_deg=3.1, eccentricity=0.0002, raan_deg=200.0,
        argp_deg=90.0, mean_anomaly_deg=45.0,
        mean_motion_rev_per_day=1.0027, bstar=0.0, epoch=epoch,
    )
    assert lon is not None and -180.0 < lon <= 180.0


def test_sublon_none_on_impossible_orbit():
    # Mean motion so high the perigee is inside the Earth -> propagation error.
    epoch = datetime(2026, 7, 1, tzinfo=timezone.utc)
    lon = sub_longitude_deg(
        inclination_deg=0.0, eccentricity=0.99, raan_deg=0.0, argp_deg=0.0,
        mean_anomaly_deg=0.0, mean_motion_rev_per_day=17.0, bstar=0.0, epoch=epoch,
    )
    assert lon is None


def test_drift_rate_sign_and_magnitude():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    samples = [(t0, 100.0), (t0 + timedelta(days=10), 109.0)]
    rate = drift_deg_per_day(samples)
    assert rate is not None and abs(rate - 0.9) < 1e-6


def test_drift_rate_handles_seam_wrap():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    # 179 -> -179 is +2 deg across the seam, not -358.
    samples = [(t0, 179.0), (t0 + timedelta(days=2), -179.0)]
    rate = drift_deg_per_day(samples)
    assert rate is not None and abs(rate - 1.0) < 1e-6


def test_drift_rate_none_with_one_sample():
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert drift_deg_per_day([(t0, 1.0)]) is None
