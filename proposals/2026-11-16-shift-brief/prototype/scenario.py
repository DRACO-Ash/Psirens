"""PROTOTYPE ONLY. Inject five illustrative events into a copy of the guide
seed. Everything else in the store is left exactly as seeded."""
import json, sys
from datetime import datetime, timedelta
src, dst = sys.argv[1], sys.argv[2]
h = json.load(open(f"{src}/history.json"))
O = h["objects"]
end = max(datetime.fromisoformat(s["epoch"]) for o in O.values() for s in o["samples"])
def t(s): return datetime.fromisoformat(s["epoch"])
def days(a, b): return (a - b).total_seconds() / 86400
# 1. SHIYAN 12 02: E-W burn 30 h ago, now drifting 0.60 deg/day WEST
tb = end - timedelta(hours=30)
for s in O["50322"]["samples"]:
    if t(s) >= tb: s["sub_lon_deg"] = round(s["sub_lon_deg"] - 0.60 * days(t(s), tb), 4)
# 2. COSMOS 2589: N-S burn 58 h ago, inclination +0.030 deg
tb = end - timedelta(hours=58)
for s in O["64467"]["samples"]:
    if t(s) >= tb: s["inclination_deg"] = round(s["inclination_deg"] + 0.030, 4)
# 3. TJS-17: was drifting EAST 0.25 deg/day, stopped 40 h ago (arrived)
tb = end - timedelta(hours=40)
for s in O["63524"]["samples"]:
    if t(s) < tb: s["sub_lon_deg"] = round(s["sub_lon_deg"] - 0.25 * days(tb, t(s)), 4)
# 4. TIANLIAN-3 01: large burn 7 h ago, 1.0 deg/day EAST (early warning only)
tb = end - timedelta(hours=7)
for s in O["100172"]["samples"]:
    if t(s) >= tb: s["sub_lon_deg"] = round(s["sub_lon_deg"] + 1.0 * days(t(s), tb), 4)
# 5. TJS-24: no fix for the last 19 h
O["69235"]["samples"] = [s for s in O["69235"]["samples"] if t(s) < end - timedelta(hours=19)]
json.dump(h, open(f"{dst}/history.json", "w"))
print("store end", end.isoformat())
