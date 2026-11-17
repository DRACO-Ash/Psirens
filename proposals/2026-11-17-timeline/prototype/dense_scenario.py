"""PROTOTYPE ONLY. A dense, clustered event scenario like the live belt Ash
captured on 17 November 2026 (about 40 events in four days, bunched), built on
the guide seed's synthetic geometry. Events are PLANTED in the samples; the
list the timeline shows is whatever src/psirens/events.py then DETECTS.

Usage: python dense_scenario.py <repo-root> <out-dir>
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone

root, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{root}/tools")
sys.path.insert(0, f"{root}/src")
import guide_seed  # noqa: E402
from psirens.events import Thresholds, detect  # noqa: E402

guide_seed._SPAN_DAYS = 7          # enough history for a burn on day one to be confirmable
rng = random.Random(20261117)
end = datetime.now(timezone.utc).replace(microsecond=0)
head = end - timedelta(hours=0.5)
snap = f"{root}/src/psirens/static/hrr-geo.json"
store = guide_seed.build(snap, end, 0.5, events=False)
hrr = json.load(open(snap))["objects"]
O = store["objects"]
quiet = [s for s in O if (hrr.get(s) or {}).get("rank") in (1, 2, 3)
         and abs(guide_seed._profile(s)["drift"]) < 0.03]
rng.shuffle(quiet)


def t(s):
    return datetime.fromisoformat(s["epoch"])


def ew(sat, hours_ago, rate):
    burn = head - timedelta(hours=hours_ago)
    for s in O[sat]["samples"]:
        if t(s) >= burn:
            s["sub_lon_deg"] = round(s["sub_lon_deg"] + rate * (t(s) - burn).total_seconds() / 86400, 4)


def ns(sat, hours_ago, step):
    burn = head - timedelta(hours=hours_ago)
    for s in O[sat]["samples"]:
        if t(s) >= burn:
            s["inclination_deg"] = round(s["inclination_deg"] + step, 4)


def silent(sat, hours):
    O[sat]["samples"] = [s for s in O[sat]["samples"] if t(s) < head - timedelta(hours=hours)]


plan = []
# A tight knot on day two, like the left of the screenshot, and another late on.
plan += [("ns", 70 + rng.uniform(0, 3)) for _ in range(5)] + [("ew", 71 + rng.uniform(0, 2)) for _ in range(3)]
plan += [("ew", 30 + rng.uniform(0, 3)) for _ in range(4)] + [("ns", 26 + rng.uniform(0, 4)) for _ in range(3)]
# Spread across the four days.
plan += [("ns", rng.uniform(20, 90)) for _ in range(9)] + [("ew", rng.uniform(26, 90)) for _ in range(7)]
# Early warnings and silences at the newest end, where the screenshot piles up.
plan += [("leave", rng.uniform(4, 9)) for _ in range(3)] + [("quiet", rng.uniform(13, 22)) for _ in range(4)]
for (kind, h), sat in zip(plan, quiet):
    if kind == "ns":
        ns(sat, h, rng.choice([-1, 1]) * rng.uniform(0.012, 0.05))
    elif kind == "ew":
        ew(sat, h, rng.choice([-1, 1]) * rng.uniform(0.12, 0.6))
    elif kind == "leave":
        ew(sat, h, rng.choice([-1, 1]) * 1.0)
    else:
        silent(sat, h)

with open(f"{out}/history.json", "w") as fh:
    json.dump(store, fh, separators=(",", ":"))
with open(f"{out}/hrr.json", "w") as fh:
    json.dump(json.load(open(snap)), fh)
found = detect(store, hrr, Thresholds(), now=end, stale_after_hours=24)["events"]
kinds = {}
for e in found:
    kinds[e["type"]] = kinds.get(e["type"], 0) + 1
print(f"planted {len(plan)}, detected {len(found)}: {kinds}")
