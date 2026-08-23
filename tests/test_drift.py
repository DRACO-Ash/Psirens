"""drift_deg_per_day: physical rate, near-duplicate-epoch, and wrap handling."""
from datetime import datetime, timedelta, timezone

from psirens.astro import drift_deg_per_day

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _s(days, lon):
    return (T0 + timedelta(days=days), lon)


def test_none_with_one_sample():
    assert drift_deg_per_day([_s(0, 60.0)]) is None


def test_normal_drift_east():
    # +1 deg/day over 4 days
    d = drift_deg_per_day([_s(0, 60.0), _s(2, 62.0), _s(4, 64.0)])
    assert d is not None and abs(d - 1.0) < 1e-6


def test_seam_wrap_is_small_rate():
    # 179 -> -179 over 1 day is +2 deg, not -358
    d = drift_deg_per_day([_s(0, 179.0), _s(1, -179.0)])
    assert d is not None and abs(d - 2.0) < 1e-6


def test_near_duplicate_epochs_return_none():
    # two fixes 1 ms apart must not yield a huge rate
    a = (T0, 60.0)
    b = (T0 + timedelta(milliseconds=1), 60.26)
    assert drift_deg_per_day([a, b]) is None


def test_impossible_rate_returns_none():
    # 150 deg jump in one day is not physical for GEO -> artifact
    assert drift_deg_per_day([_s(0, 60.0), _s(1, -150.0)]) is None


def test_walks_back_past_near_duplicate():
    # latest pair is near-duplicate; a good earlier fix exists 3 days back
    samples = [_s(0, 60.0), _s(3, 63.0),
               (T0 + timedelta(days=3, milliseconds=1), 63.0002)]
    d = drift_deg_per_day(samples)
    assert d is not None and abs(d - 1.0) < 1e-3
