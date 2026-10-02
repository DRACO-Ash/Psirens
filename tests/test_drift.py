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


# -- the fit, and the libration it exists to reject (1.6.11) ----------------
#
# Reported live on 2 October 2026: TJS-17 read -3.82 deg/day against a total
# track excursion of 5.9 deg, which that rate would sweep in 1.54 days. The
# number was a two-point difference over a 30-minute baseline, which divides
# by 1/48 of a day and so multiplies ANY longitude disagreement by 48.
import math

from psirens.astro import MIN_DRIFT_SPAN_H, drift_fit

_SIDEREAL_DAY = 0.9972695787  # days
_TJS17_E = 0.0021955          # measured, from the native line 2 on the panel


def _librating(e: float, lon0: float, *, days: float, step_h: float,
               drift: float = 0.0) -> list[tuple[datetime, float]]:
    """A GEO object whose longitude oscillates by 2e radians once per sidereal
    day, with an optional genuine secular drift on top."""
    amp = math.degrees(2.0 * e)
    out = []
    t = 0.0
    while t <= days:
        lon = lon0 + drift * t + amp * math.sin(2.0 * math.pi * t / _SIDEREAL_DAY)
        out.append((T0 + timedelta(days=t), lon))
        t += step_h / 24.0
    return out


def _two_point(samples):
    """The pre-1.6.11 method: newest against the previous sample."""
    (t0, lon0), (t1, lon1) = samples[-2], samples[-1]
    dlon = (lon1 - lon0 + 180.0) % 360.0 - 180.0
    return dlon / ((t1 - t0).total_seconds() / 86400.0)


def test_the_libration_really_does_fool_a_short_baseline():
    """The red case for the fit. If this stopped being true the tests below
    would pass against data that holds no trap, and prove nothing."""
    samples = _librating(_TJS17_E, 158.65, days=3.0, step_h=0.5)
    assert abs(_two_point(samples)) > 1.0, (
        "a 30-minute difference on an e=0.0022 object must read as drift")


def test_an_eccentric_geo_object_is_not_drifting():
    """Same data, fitted: a station-kept object reads as station-kept. The
    libration is MODELLED, not averaged over, so it leaves neither a rate nor
    a residual."""
    fit = drift_fit(_librating(_TJS17_E, 158.65, days=3.0, step_h=0.5))
    assert fit is not None and fit.harmonic
    assert abs(fit.rate_deg_per_day) < 0.001, (
        f"libration leaked into the rate: {fit.rate_deg_per_day:.4f} deg/day")
    assert fit.residual_deg < 0.001


def test_real_scatter_still_shows_in_the_residual():
    """The residual must not be decorative: provider disagreement the model
    cannot explain has to be reported."""
    base = _librating(_TJS17_E, 158.65, days=3.0, step_h=0.5)
    jittered = [(t, lon + (0.04 if k % 2 else -0.04))
                for k, (t, lon) in enumerate(base)]
    fit = drift_fit(jittered)
    assert fit is not None
    assert abs(fit.rate_deg_per_day) < 0.01
    assert 0.03 < fit.residual_deg < 0.05


def test_daily_fixes_fall_back_to_the_plain_line():
    """One fix a day aliases the sidereal libration badly, so the harmonic is
    not claimed. The rate is still recovered."""
    daily = [(T0 + timedelta(days=k), 60.0 + 0.5 * k) for k in range(8)]
    fit = drift_fit(daily, window_days=10.0)
    assert fit is not None and fit.harmonic is False
    assert abs(fit.rate_deg_per_day - 0.5) < 1e-6


def test_a_genuine_drift_survives_the_fit():
    """The fit must not flatten real motion along with the libration."""
    fit = drift_fit(_librating(_TJS17_E, 158.65, days=3.0, step_h=0.5, drift=-0.5))
    assert fit is not None
    assert abs(fit.rate_deg_per_day + 0.5) < 0.05


def test_provider_scatter_does_not_become_drift():
    """Consecutive fixes can be two providers' views of the same object. That
    difference is bias, not motion, and a short baseline multiplies it."""
    samples = [(T0 + timedelta(hours=k), 60.0 + (0.04 if k % 2 else -0.04))
               for k in range(73)]
    assert abs(_two_point(samples)) > 1.0      # the trap is present
    fit = drift_fit(samples)
    assert fit is not None and abs(fit.rate_deg_per_day) < 0.01


def test_a_dense_recent_burst_has_no_usable_baseline():
    """An hour of fixes is not a baseline. None is the honest answer."""
    samples = [(T0 + timedelta(minutes=5 * k), 60.0 + 0.001 * k) for k in range(13)]
    assert drift_fit(samples) is None


def test_the_window_widens_rather_than_refusing():
    """A dense recent burst with older history behind it fits over the history
    instead of returning nothing."""
    old = [(T0 + timedelta(days=k), 60.0 + k) for k in range(4)]
    burst = [(T0 + timedelta(days=3, minutes=5 * k), 63.0) for k in range(1, 13)]
    fit = drift_fit(old + burst)
    assert fit is not None
    assert fit.span_hours >= MIN_DRIFT_SPAN_H
    assert abs(fit.rate_deg_per_day - 1.0) < 0.2


def test_the_fit_reports_its_baseline_and_count():
    fit = drift_fit(_librating(_TJS17_E, 158.65, days=3.0, step_h=1.0))
    assert fit is not None
    assert fit.points == 73
    assert abs(fit.span_hours - 72.0) < 0.01
