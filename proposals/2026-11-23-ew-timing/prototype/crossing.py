#!/usr/bin/env python3
"""Prototype: time an E-W burn where the before and after fits cross.

Run from the repository root:  PYTHONPATH=src python3 proposals/2026-11-23-ew-timing/prototype/crossing.py

It drives the PRODUCTION detector (`events.ew_split`) on synthetic objects
whose burn time is known, then refines the winning 3-hour split to the point
where the two fitted drift lines meet. Physics: an impulsive E-W burn changes
the drift RATE, not the longitude, so the two linear trends intersect at the
burn. The libration terms are fitted out on each side exactly as the
inspector's drift fit does.

Writes results.json beside this file: accuracy of the grid stamp and of the
crossing, the stability of each as new data arrives, and one worked example
for the proposal's chart. Nothing here ships.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psirens import events
from psirens.astro import model_coefficients, resolves_libration

OUT = Path(__file__).with_name("results.json")
T0 = datetime(2026, 11, 10, tzinfo=timezone.utc)
SIDEREAL_H = 23.9344696
WIN = timedelta(hours=events.WIN_H)
CLAMP_H = 2 * events.STEP_H      # never move further than two grid steps
ITERATIONS = 3


def synth(rng: random.Random, t_true: datetime, r_before: float, r_after: float,
          end: datetime) -> list[tuple[datetime, float]]:
    """A GEO longitude series: drift, a burn at t_true, libration, scatter,
    irregular cadence. Longitude is continuous through the burn."""
    amp = rng.uniform(0.03, 0.25)            # deg, eccentricity libration
    phase = rng.uniform(0, 2 * math.pi)
    noise = rng.uniform(0.003, 0.015)        # deg, provider scatter
    t, out = T0, []
    while t < end:
        h_true = (t - t_true).total_seconds() / 86400.0
        trend = r_before * h_true if h_true < 0 else r_after * h_true
        lib = amp * math.sin(2 * math.pi * (t - T0).total_seconds() / 3600 / SIDEREAL_H + phase)
        out.append((t, 100.0 + trend + lib + rng.gauss(0, noise)))
        t += timedelta(minutes=rng.uniform(30, 120))
    return out


def _side(lon, lo, hi, origin):
    rows = [(w, v) for w, v in lon if lo <= w < hi]
    xs = [(w - origin).total_seconds() / 86400.0 for w, _ in rows]
    if len(xs) < 8:
        return None
    fitted = model_coefficients(xs, [v for _, v in rows], resolves_libration(xs))
    if fitted is None:
        return None
    coeffs, rms = fitted
    return coeffs[0], coeffs[1], rms, len(xs)


def crossing(lon, split: datetime):
    """(epoch, sigma_hours) where the two trend lines meet, refined by
    re-splitting at the estimate, or None when the lines do not cross near the
    split (then the grid stamp stands)."""
    t = split
    sigma_h = None
    for _ in range(ITERATIONS):
        b = _side(lon, t - WIN, t, t)
        a = _side(lon, t, t + WIN, t)
        if b is None or a is None or abs(b[1] - a[1]) < 1e-6:
            return None
        x = (a[0] - b[0]) / (b[1] - a[1])            # days from t
        # Intercept standard error at the window edge is ~2 rms / sqrt(n).
        s_int = math.hypot(2 * b[2] / math.sqrt(b[3]), 2 * a[2] / math.sqrt(a[3]))
        sigma_h = s_int / abs(b[1] - a[1]) * 24.0
        new_t = t + timedelta(days=x)
        if abs((new_t - split).total_seconds()) > CLAMP_H * 3600:
            return None
        t = new_t
    return t, sigma_h


def accuracy(trials: int = 400, seed: int = 7) -> dict:
    rng = random.Random(seed)
    th = events.Thresholds()
    rows = []
    for _ in range(trials):
        r_before = rng.choice([0.0, rng.uniform(-0.01, 0.01), rng.uniform(-0.3, 0.3)])
        delta = rng.choice([0.05, 0.1, 0.3, 1.0]) * rng.choice([-1, 1])
        t_true = T0 + timedelta(hours=rng.uniform(70, 110))
        ref = t_true + timedelta(hours=rng.uniform(30, 70))
        lon = events.unwrap(synth(rng, t_true, r_before, r_before + delta, ref)) \
            if hasattr(events, "unwrap") else synth(rng, t_true, r_before, r_before + delta, ref)
        split = events.ew_split(lon, ref, th)
        if split is None or split.edge:
            rows.append({"delta": abs(delta), "found": False})
            continue
        cross = crossing(lon, split.when)
        grid_err = (split.when - t_true).total_seconds() / 3600
        row = {"delta": abs(delta), "found": True, "grid_err_h": grid_err,
               "cross_err_h": None, "sigma_h": None}
        if cross is not None:
            row["cross_err_h"] = (cross[0] - t_true).total_seconds() / 3600
            row["sigma_h"] = cross[1]
        rows.append(row)
    return summarise(rows)


def _q(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


def summarise(rows) -> dict:
    found = [r for r in rows if r["found"]]
    crossed = [r for r in found if r["cross_err_h"] is not None]
    by = {}
    for d in sorted({r["delta"] for r in found}):
        g = [abs(r["grid_err_h"]) for r in found if r["delta"] == d]
        c = [abs(r["cross_err_h"]) for r in crossed if r["delta"] == d]
        inside = [r for r in crossed if r["delta"] == d
                  and abs(r["cross_err_h"]) <= 2 * r["sigma_h"]]
        by[str(d)] = {"n": len(g), "grid_median_h": statistics.median(g),
                      "grid_p95_h": _q(g, 0.95),
                      "cross_median_h": statistics.median(c) if c else None,
                      "cross_p95_h": _q(c, 0.95),
                      "crossed": len(c),
                      "within_2_sigma": len(inside)}
    return {"trials": len(rows), "found": len(found), "crossed": len(crossed),
            "by_delta": by}


def stability(trials: int = 60, seed: int = 11, hours: int = 9) -> dict:
    """Advance the reference hour by hour, with the data arriving as it would,
    and count how many distinct epochs each method stamps on the SAME burn."""
    rng = random.Random(seed)
    th = events.Thresholds()
    grid_counts, cross_spread = [], []
    grid_changed = cross_changed = compared = 0
    for _ in range(trials):
        t_true = T0 + timedelta(hours=rng.uniform(70, 100))
        full = synth(rng, t_true, 0.0, rng.choice([0.1, 0.3]) * rng.choice([-1, 1]),
                     t_true + timedelta(hours=60 + hours))
        grids, crosses = set(), []
        for k in range(hours + 1):
            ref = t_true + timedelta(hours=50 + k)
            lon = [p for p in full if p[0] <= ref]
            split = events.ew_split(lon, ref, th)
            if split is None or split.edge:
                continue
            grids.add(split.when)
            c = crossing(lon, split.when)
            if c is not None:
                crosses.append(c[0])
        if grids:
            grid_counts.append(len(grids))
        if grids and crosses:
            compared += 1
            grid_changed += len(grids) > 1
            # The event id, keyed on the stamp rounded to the hour.
            cross_changed += len({c.replace(minute=0, second=0, microsecond=0)
                                  + timedelta(hours=c.minute >= 30) for c in crosses}) > 1
        if len(crosses) > 1:
            cross_spread.append((max(crosses) - min(crosses)).total_seconds() / 60)
    return {"trials": trials, "hours_advanced": hours,
            "grid_distinct_epochs_median": statistics.median(grid_counts),
            "grid_distinct_epochs_max": max(grid_counts),
            "cross_spread_minutes_median": statistics.median(cross_spread),
            "cross_spread_minutes_p95": _q(cross_spread, 0.95),
            "burns_compared": compared,
            "grid_id_changed": grid_changed,
            "cross_hour_id_changed": cross_changed}


def example(seed: int = 3) -> dict:
    """One worked burn for the proposal chart: series, both fitted lines, the
    grid stamp, the crossing and the truth."""
    rng = random.Random(seed)
    t_true = T0 + timedelta(hours=88.6)
    ref = t_true + timedelta(hours=52)
    lon = synth(rng, t_true, 0.0, -0.3, ref)
    split = events.ew_split(lon, ref, events.Thresholds())
    cross = crossing(lon, split.when)
    t_c = cross[0]
    b = _side(lon, t_c - WIN, t_c, t_c)
    a = _side(lon, t_c, t_c + WIN, t_c)

    def h(w):
        return round((w - T0).total_seconds() / 3600, 3)
    return {"series": [[h(w), round(v, 4)] for w, v in lon],
            "true_h": h(t_true), "grid_h": h(split.when), "cross_h": h(t_c),
            "sigma_h": round(cross[1], 2),
            "before": {"at_cross": round(b[0], 4), "rate": round(b[1], 4)},
            "after": {"at_cross": round(a[0], 4), "rate": round(a[1], 4)},
            "win_h": events.WIN_H, "step_h": events.STEP_H}


if __name__ == "__main__":
    result = {"accuracy": accuracy(), "stability": stability(), "example": example()}
    OUT.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "example"}, indent=1))
    ex = result["example"]
    print("example: true %.2f h, grid %.2f h, crossing %.2f h (sigma %.2f h)"
          % (ex["true_h"], ex["grid_h"], ex["cross_h"], ex["sigma_h"]))
