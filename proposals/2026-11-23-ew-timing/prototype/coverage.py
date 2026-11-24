#!/usr/bin/env python3
"""Measure CROSS_COVERAGE_K for the SHIPPED crossing (events.ew_crossing): the
factor on the stated standard error that covers 95% of true burn times.
Fitted on one seed, checked on an independent one so the factor is not judged
on the data that chose it. Run from the repo root:
    PYTHONPATH=src:proposals/2026-11-23-ew-timing/prototype python3 .../coverage.py"""
import json
import math
import random
import sys
from datetime import timedelta

from crossing import T0, synth
from psirens import events


def ratios(seed: int, trials: int = 400) -> list[float]:
    rng = random.Random(seed)
    th = events.Thresholds()
    out = []
    for _ in range(trials):
        r_before = rng.choice([0.0, rng.uniform(-0.01, 0.01), rng.uniform(-0.3, 0.3)])
        delta = rng.choice([0.05, 0.1, 0.3, 1.0]) * rng.choice([-1, 1])
        t_true = T0 + timedelta(hours=rng.uniform(70, 110))
        ref = t_true + timedelta(hours=rng.uniform(30, 70))
        lon = events.unwrap(synth(rng, t_true, r_before, r_before + delta, ref))
        split = events.ew_split(lon, ref, th)
        if split is None or split.edge:
            continue
        c = events.ew_crossing(lon, split.when)
        if c is None or c.sigma_hours <= 0:
            continue
        out.append(abs((c.when - t_true).total_seconds() / 3600) / c.sigma_hours)
    return sorted(out)


fit, check = ratios(7), ratios(8)
k95 = fit[min(len(fit) - 1, math.ceil(0.95 * len(fit)) - 1)]
k = math.ceil(k95 * 10) / 10                      # rounded UP to a tenth
cover = lambda rs, kk: sum(r <= kk for r in rs) / len(rs)  # noqa: E731
result = {"fit_seed": 7, "check_seed": 8, "fit_n": len(fit), "check_n": len(check),
          "k95_fit": round(k95, 3), "k_chosen": k,
          "coverage_2sigma_fit": round(cover(fit, 2.0), 3),
          "coverage_k_fit": round(cover(fit, k), 3),
          "coverage_k_check": round(cover(check, k), 3)}
json.dump(result, open(sys.argv[1] if len(sys.argv) > 1 else "/dev/stdout", "w"), indent=1)
print(json.dumps(result, indent=1))
