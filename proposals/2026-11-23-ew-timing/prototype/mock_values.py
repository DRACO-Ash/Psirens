#!/usr/bin/env python3
"""Mockup values from the LIVE offline app, not hand-typed: fetch EXAMPLE-1's
E-W event and its served track, and time the burn with the prototype's own
`crossing`. Usage: PYTHONPATH=src:<this dir> python3 mock_values.py <base> <out.json>"""
import json
import sys
import urllib.request
from datetime import datetime

from crossing import crossing
from psirens import events

base, out = sys.argv[1:3]
get = lambda p: json.load(urllib.request.urlopen(base + p))  # noqa: E731
ev = next(e for e in get("/api/events?view=combined")["events"]
          if e["object_id"] == "EXAMPLE-1" and e["type"] in ("STARTED", "STOPPED", "EW"))
track = next(t for t in get("/api/tracks?view=combined")["tracks"] if t["object_id"] == "EXAMPLE-1")
lon = events.unwrap(sorted((datetime.fromisoformat(s["epoch"].replace("Z", "+00:00")),
                            s["sub_lon_deg"]) for s in track["samples"]))
split = datetime.fromisoformat(ev["evidence"]["split"])
cross = crossing(lon, split)
t_c, sigma = cross
json.dump({"event_id": ev["id"], "split": ev["evidence"]["split"], "cross": t_c.isoformat(),
           "sigma_h": round(sigma, 2), "series_last": max(p[0] for p in ev["evidence"]["series"]),
           "rate_before": ev["evidence"]["before"]["rate_deg_per_day"],
           "rate_after": ev["evidence"]["after"]["rate_deg_per_day"]}, open(out, "w"), indent=1)
print(open(out).read())
