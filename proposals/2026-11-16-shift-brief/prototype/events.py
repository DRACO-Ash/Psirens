"""PROTOTYPE ONLY. Run both detectors over every object in a store and write
the events the mockup will display. Nothing here is hand-written output."""
import json, sys, statistics
from datetime import datetime, timedelta
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, "/home/user/Psirens/src")
from detect import ew_event, ns_event
from early import first_alarm
store, hrr, out = sys.argv[2], sys.argv[3], sys.argv[4]
O = json.load(open(store))["objects"]; meta = json.load(open(hrr))["objects"]
def tt(s): return datetime.fromisoformat(s["epoch"])
end = max(tt(s) for o in O.values() for s in o["samples"])
ev = []
for oid, o in O.items():
    S = sorted(o["samples"], key=tt)
    L = [(tt(s), s["sub_lon_deg"]) for s in S]; I = [(tt(s), s["inclination_deg"]) for s in S]
    m = meta.get(oid, {}); base = {"id": oid, "name": m.get("name", oid), "rank": m.get("rank"), "country": m.get("country")}
    e = ew_event(L)
    if e:
        when, rb, ra, z, edge = e
        if edge:
            e = None   # cannot confirm yet: fall through to the early-warning test
        else:
            kind = "STOPPED" if abs(ra) < 0.02 <= abs(rb) else ("STARTED" if abs(rb) < 0.02 else "EW")
            ev.append({**base, "type": kind, "state": "CONFIRMED", "when": when.isoformat(),
                       "before": round(rb, 3), "after": round(ra, 3), "z": round(z, 1)})
    n = ns_event(I)
    if n:
        when, d, z = n
        ev.append({**base, "type": "NS", "state": "CONFIRMED", "when": when.isoformat(), "dinc": round(d, 4), "z": round(z, 1)})
    if not e:
        a = first_alarm(L, end - timedelta(hours=24), end)
        if a: ev.append({**base, "type": "SUSPECTED", "state": "SUSPECTED", "when": a.isoformat()})
    age_h = (end - tt(S[-1])).total_seconds() / 3600
    if age_h >= 12:
        ev.append({**base, "type": "QUIET", "state": "CONFIRMED", "when": S[-1]["epoch"], "age_h": round(age_h, 1)})
ev.sort(key=lambda x: x["when"], reverse=True)
json.dump({"end": end.isoformat(), "objects": len(O), "events": ev}, open(out, "w"), indent=1)
print(f"{len(O)} objects scanned, {len(ev)} events")
for x in ev: print(" ", x["when"][:16], x["type"], x["state"], x["name"], {k: x[k] for k in ("before","after","dinc","z","age_h") if k in x})
