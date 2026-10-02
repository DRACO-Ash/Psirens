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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


_MAX_GEO_DRIFT = 18.0     # deg/day; real GEO longitude rate tops out ~14, above is an artifact
DRIFT_WINDOW_DAYS = 3.0   # trailing window the rate is fitted over
MIN_DRIFT_SPAN_H = 24.0   # one full sidereal day: a shorter baseline measures libration, not drift
SIDEREAL_DAY_DAYS = 0.9972695787  # the libration period, not the solar day
_MIN_DRIFT_POINTS = 2
_HARMONIC_MIN_POINTS = 8      # 4 unknowns; fewer than this and the fit is not worth it
_HARMONIC_MAX_STEP_D = 0.25   # 6 hours: coarser than this and the libration is aliased
_OMEGA = 2.0 * math.pi / SIDEREAL_DAY_DAYS
_SINGULAR = 1e-12


@dataclass(frozen=True)
class DriftFit:
    """A fitted longitude rate, with enough context to judge it.

    `residual_deg` is the RMS scatter of the samples about the fitted model.
    It is the honest companion to the rate: a station-kept object with a tight
    line reads near zero, while a noisy multi-provider series or a mid-window
    manoeuvre shows a large residual and the rate should be read as a summary
    rather than a measurement. `harmonic` says whether the once-per-sidereal-
    day libration was fitted out or merely averaged over.
    """

    rate_deg_per_day: float
    residual_deg: float
    points: int
    span_hours: float
    harmonic: bool


def _unwrap(samples: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Longitudes made continuous across the +/-180 seam.

    A fit cannot be done on wrapped angles: an object crossing the seam would
    otherwise appear to jump 360 degrees and dominate the slope. Each step is
    taken as the shortest angular difference and accumulated.
    """
    out: list[tuple[datetime, float]] = []
    prev: float | None = None
    offset = 0.0
    for when, lon in samples:
        if prev is not None:
            step = (lon - prev + 180.0) % 360.0 - 180.0
            offset += step - (lon - prev)
        out.append((when, lon + offset))
        prev = lon
    return out


def _pivot_row(aug: list[list[float]], col: int, n: int) -> int:
    """Index of the row with the largest magnitude in `col`, at or below it.

    Extracted so `col` arrives as a PARAMETER rather than being captured from
    the enclosing loop. A lambda that closes over a loop variable reads the
    value the variable holds when the lambda RUNS, not when it was written,
    which is a real defect anywhere the lambda outlives the iteration. Code
    Quality raised it here (astro.py:174, 2 October 2026) even though the
    lambda was consumed immediately and the behaviour was correct; the rule is
    about the shape, not this instance.
    """
    best = col
    for row in range(col + 1, n):
        if abs(aug[row][col]) > abs(aug[best][col]):
            best = row
    return best


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. None when singular.

    Four unknowns at most, from normal equations built on columns of 1, t and
    a sine/cosine pair. Deliberately explicit rather than pulling in a linear
    algebra dependency for a 4x4 solve.
    """
    n = len(rhs)
    aug = [list(row) + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = _pivot_row(aug, col, n)
        if abs(aug[pivot][col]) < _SINGULAR:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, n):
            factor = aug[row][col] / aug[col][col]
            for c in range(col, n + 1):
                aug[row][c] -= factor * aug[col][c]
    out = [0.0] * n
    for row in reversed(range(n)):
        acc = aug[row][n] - sum(aug[row][c] * out[c] for c in range(row + 1, n))
        out[row] = acc / aug[row][row]
    return out


def _basis(x: float, harmonic: bool) -> list[float]:
    """Model columns: a line, optionally plus one sidereal-day libration."""
    if harmonic:
        return [1.0, x, math.sin(_OMEGA * x), math.cos(_OMEGA * x)]
    return [1.0, x]


def _fit(xs: list[float], ys: list[float],
         harmonic: bool) -> tuple[float, float] | None:
    """Least-squares slope and RMS residual, or None when the system is
    singular (identical epochs, or a basis the sampling cannot resolve)."""
    rows = [_basis(x, harmonic) for x in xs]
    width = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(width)]
           for i in range(width)]
    atb = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(width)]
    beta = _solve(ata, atb)
    if beta is None:
        return None
    err = [y - sum(b * c for b, c in zip(beta, r)) for r, y in zip(rows, ys)]
    return beta[1], math.sqrt(sum(e * e for e in err) / len(err))


def _resolves_libration(xs: list[float]) -> bool:
    """Whether the sampling can see the once-per-sidereal-day oscillation.

    Daily fixes alias it: at roughly one sample per solar day the sine column
    beats against the sampling with a period of about a year, so over a
    three-day window it is nearly collinear with the constant and linear
    columns and the fit becomes ill-conditioned. Below four samples per cycle
    the honest move is not to claim the harmonic.
    """
    if len(xs) < _HARMONIC_MIN_POINTS:
        return False
    steps = sorted(b - a for a, b in zip(xs, xs[1:]))
    median = steps[len(steps) // 2]
    return 0.0 < median <= _HARMONIC_MAX_STEP_D


def _fit_window(ordered: list[tuple[datetime, float]],
                window_days: float) -> list[tuple[datetime, float]] | None:
    """The samples to fit: the trailing window, widened to the full history if
    that window is too short.

    A dense burst of recent fixes spanning an hour is exactly the case the fit
    exists to refuse. Rather than return nothing while older fixes sit in the
    store, reach back for them; the residual then says how much to trust the
    longer baseline. None means the whole history is too short, which is an
    honest "not yet known".
    """
    def _span_h(rows: list[tuple[datetime, float]]) -> float:
        return (rows[-1][0] - rows[0][0]).total_seconds() / 3600.0

    recent = [s for s in ordered
              if (ordered[-1][0] - s[0]).total_seconds() <= window_days * 86400.0]
    for candidate in (recent, ordered):
        if len(candidate) >= _MIN_DRIFT_POINTS and _span_h(candidate) >= MIN_DRIFT_SPAN_H:
            return candidate
    return None


def drift_fit(samples: list[tuple[datetime, float]], *,
              window_days: float = DRIFT_WINDOW_DAYS) -> DriftFit | None:
    """Longitude drift rate (deg/day), fitted over a trailing window, with the
    RMS residual about the fitted model.

    WHY A FIT AND NOT A DIFFERENCE. Until 1.6.11 this took the newest sample
    against the most recent one at least 30 minutes earlier. That baseline
    divides by 1/48 of a day, so it multiplies ANY longitude disagreement by
    48: a 0.08 degree difference reads as 3.8 deg/day. Two things produce that
    disagreement with the object going nowhere. A slightly eccentric GEO orbit
    librates east-west by 2e radians once per SIDEREAL day, which at the
    measured e = 0.0022 of TJS-17 is an amplitude of 0.25 degrees and a peak
    rate of 1.58 deg/day. And consecutive samples can be two different
    providers' views of the same object minutes apart, where the difference is
    bias rather than motion. Reported live, 2 October 2026: TJS-17 showed
    -3.82 deg/day against a total track excursion of 5.9 degrees, which that
    rate would sweep in 1.54 days.

    WHAT IS FITTED. Where the sampling resolves the libration, the model is a
    line PLUS a sine and cosine at the sidereal frequency, so the oscillation
    is removed rather than averaged. MEASURED on a synthetic e = 0.0022
    object sampled every 30 minutes for three days with no true drift: a plain
    line returns -0.053 deg/day with a 0.172 deg residual, and the harmonic
    model returns 1.2e-13 deg/day with a 1.1e-13 residual. Where the sampling
    is too coarse to see the cycle (roughly daily fixes alias it badly), the
    plain line is used and `harmonic` says so.

    Returns None with too few samples, a baseline under MIN_DRIFT_SPAN_H, a
    singular system, or a rate that is physically impossible for a GEO object
    (a data artifact such as a mismatched historical elset) rather than
    asserting a garbage rate.
    """
    if len(samples) < _MIN_DRIFT_POINTS:
        return None
    window = _fit_window(sorted(samples, key=lambda s: s[0]), window_days)
    if window is None:
        return None
    points = _unwrap(window)
    start = points[0][0]
    xs = [(when - start).total_seconds() / 86400.0 for when, _ in points]
    ys = [lon for _, lon in points]
    harmonic = _resolves_libration(xs)
    fitted = _fit(xs, ys, harmonic) or _fit(xs, ys, False)
    if fitted is None or abs(fitted[0]) > _MAX_GEO_DRIFT:
        return None
    return DriftFit(rate_deg_per_day=fitted[0], residual_deg=fitted[1],
                    points=len(points), span_hours=xs[-1] * 24.0,
                    harmonic=harmonic)


def drift_deg_per_day(samples: list[tuple[datetime, float]]) -> float | None:
    """The fitted rate alone, for callers that want only the number."""
    fit = drift_fit(samples)
    return None if fit is None else fit.rate_deg_per_day
