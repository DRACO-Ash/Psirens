#!/usr/bin/env python3
"""Seed a store holding the prototype's worked example as EXAMPLE-1: the same
synthetic burn (libration, provider scatter, irregular cadence, burn 1.6 h
off the 3-hour grid), shifted so its last fix is now. One tagged provider,
because the served view excludes untagged samples from events.
Usage: PYTHONPATH=src:<this dir> python3 seed_example.py <data-dir>"""
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

from crossing import T0, synth
from psirens.store import SCHEMA_VERSION

rng = random.Random(3)                      # the same seed as crossing.example()
t_true = T0 + timedelta(hours=88.6)
ref = t_true + timedelta(hours=52)
lon = synth(rng, t_true, 0.0, -0.3, ref)
shift = datetime.now(timezone.utc).replace(second=0, microsecond=0) - ref
samples = [{"epoch": (w + shift).isoformat(), "sub_lon_deg": round(v, 5),
            "inclination_deg": 0.05, "p": 0} for w, v in lon]
data = {"schema": SCHEMA_VERSION, "objects": {"EXAMPLE-1": {
    "name": "EXAMPLE-1", "data_mode": "REAL", "samples": samples,
    "provenance": [{"source": "SAFRAN", "data_mode": "REAL", "classification_marking": "U"}]}}}
with open(os.path.join(sys.argv[1], "history.json"), "w", encoding="utf-8") as fh:
    json.dump(data, fh)
if len(sys.argv) > 2:   # the truth, for the proposal caption: recorded, never derived
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump({"true_burn": (t_true + shift).isoformat(), "fixes": len(samples)}, fh)
print("seeded EXAMPLE-1: %d fixes, true burn %s" % (len(samples), (t_true + shift).isoformat()))
