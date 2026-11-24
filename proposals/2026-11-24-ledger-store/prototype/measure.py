"""Measure the production store against the ledger at full size.

Every scenario runs in its OWN process, so each peak-memory figure is that
scenario's alone. The starting store is 597 objects x 2000 samples (the
retention cap), in the production sample shape (epoch, sub_lon_deg,
inclination_deg, raan_deg, p) with four providers per object: the size the live
store is heading for, not the size it is today (TBC, owner Ash, from
`/api/meta`).

    PYTHONPATH=src python3 proposals/.../prototype/measure.py <full-size history.json>

Writes results.json beside this file.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

from psirens.sources import candidate_key

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results.json")
NOW = datetime(2026, 11, 24, 1, tzinfo=timezone.utc)   # an hour after the newest fix
HOURLY_NEW, HOURLY_REPEAT = 2, 46   # live: +1,177 a cycle over ~600 objects; 24 h overlap
DAY_NEW = 34                        # live backfill: ~20,000 new a day over ~600 objects


def peak_mb() -> float:
    """This process's own high-water mark. Not ru_maxrss: on Linux that is
    inherited across fork and exec, so a child would report its parent's peak."""
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rss_mb() -> float:
    """What the process holds now, after the work: its resting size."""
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def make_pull(data: dict, new: int, repeat: int, offset_h: float) -> dict:
    """A pull shaped like a live one: `repeat` samples already held (the
    24-hour overlap), `new` samples not held, plus the element sets."""
    out = {}
    for oid, obj in data["objects"].items():
        prov = obj["provenance"]
        rows = []
        for s in obj["samples"][-repeat:] if repeat else []:
            tags = prov[s["p"]]
            rows.append({**{k: v for k, v in s.items() if k != "p"}, **tags})
        for k in range(new):
            when = NOW - timedelta(hours=offset_h, minutes=7 * k + 3)
            rows.append({"epoch": when.isoformat(), "sub_lon_deg": 12.3456,
                         "inclination_deg": 0.0567, "raan_deg": 81.25,
                         **prov[k % len(prov)]})
        # Two providers publish a fresh element set this cycle, as live.
        fresh = [dict(obj["elset"], epoch=(NOW - timedelta(hours=offset_h, minutes=20 + k)
                                           ).isoformat(), source=prov[k]["source"])
                 for k in range(2)]
        out[oid] = {"name": obj.get("name"), "data_mode": "REAL", "samples": rows,
                    "elset": fresh[0],
                    "elset_candidates": {candidate_key(el): el for el in fresh}}
    return out


PROBE_PERIOD_S = 10.0   # Kubernetes default; the platform's own value is TBC


def cycle(st, inc: dict) -> dict:
    """One hourly refresh as the server runs it, with the readiness probe
    firing on its own thread throughout: merge the pull, rebuild the resolved
    view every route reads, answer the probe."""
    import threading
    from psirens.agreement import Resolver, policy_from
    from psirens.config import load_config
    from psirens.refresh import Refresher
    cfg = load_config()
    ref = Refresher(cfg, st, [], None)
    probes: list = []
    stop = threading.Event()

    def probe() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            ref.health(NOW)
            probes.append(time.perf_counter() - t0)
            stop.wait(PROBE_PERIOD_S)

    th = threading.Thread(target=probe)
    t = time.perf_counter()
    th.start()
    st.merge_samples(inc, retention_days=90, max_samples=2000, now=NOW)
    res = Resolver(st, policy_from(cfg))
    res.load()
    seconds = time.perf_counter() - t
    stop.set()
    th.join()
    st._keep = res   # what the server keeps between refreshes: the resolved view
    return {"seconds": round(seconds, 2), "probes": len(probes),
            "probe_max_s": round(max(probes), 3)}


def scenario(kind: str, src: str) -> dict:
    sys.path.insert(0, HERE)
    from ledger import LedgerStore
    from psirens.config import load_config
    from psirens.refresh import Refresher
    from psirens.store import Store

    work = tempfile.mkdtemp()
    if kind.startswith("led"):
        os.makedirs(os.path.join(work, "ledger"))
        for name in os.listdir(os.path.join(src, "ledger")):
            shutil.copy(os.path.join(src, "ledger", name), os.path.join(work, "ledger"))
    else:
        shutil.copy(os.path.join(src, "history.json"), work)
    base = peak_mb()
    t = time.perf_counter()
    st = LedgerStore(work) if kind.startswith("led") else Store(work)
    boot_s = time.perf_counter() - t
    row = {"kind": kind, "boot_s": round(boot_s, 2), "boot_peak_mb": round(peak_mb())}
    action = kind.split("_", 1)[1]
    if action in ("hourly", "day"):
        data = st.load()
        inc = (make_pull(data, HOURLY_NEW, HOURLY_REPEAT, 0) if action == "hourly"
               else make_pull(data, DAY_NEW, 0, 30 * 24))
        row["incoming_samples"] = sum(len(r["samples"]) for r in inc.values())
        del data
        t = time.perf_counter()
        row["added"] = st.merge_samples(inc, retention_days=90, max_samples=2000, now=NOW)
        row["seconds"] = round(time.perf_counter() - t, 3)
        row["bytes_written"] = (st.bytes_written if kind.startswith("led")
                                else os.path.getsize(st.path))
    elif action == "cycle":
        row.update(cycle(st, make_pull(st.load(), HOURLY_NEW, HOURLY_REPEAT, 0)))
    elif action == "readyz":
        ref = Refresher(load_config(), st, [], None)
        t = time.perf_counter()
        ref.health(NOW)
        row["seconds"] = round(time.perf_counter() - t, 4)
    elif action == "compact":
        t = time.perf_counter()
        row["bytes_written"] = st.compact()
        row["seconds"] = round(time.perf_counter() - t, 2)
    import gc
    gc.collect()
    row["peak_mb"] = round(peak_mb())
    row["resting_mb"] = round(rss_mb())
    row["base_mb"] = round(base)
    shutil.rmtree(work, ignore_errors=True)
    return row


def main() -> int:
    if sys.argv[1] == "--one":
        print(json.dumps(scenario(sys.argv[2], sys.argv[3])))
        return 0
    legacy = os.path.dirname(os.path.abspath(sys.argv[1]))
    prep = tempfile.mkdtemp()
    shutil.copy(os.path.join(legacy, "history.json"), prep)
    sys.path.insert(0, HERE)
    from ledger import LedgerStore
    t = time.perf_counter()
    led = LedgerStore(prep)              # first start adopts the legacy file once
    migrate = {"seconds": round(time.perf_counter() - t, 1),
               "snapshot_bytes": led.bytes_written,
               "legacy_bytes": os.path.getsize(os.path.join(prep, "history.json"))}
    del led
    rows = []
    for kind in ("cur_hourly", "led_hourly", "cur_day", "led_day",
                 "cur_readyz", "led_readyz", "cur_cycle", "led_cycle", "led_compact"):
        src = prep if kind.startswith("led") else legacy
        res = subprocess.run([sys.executable, __file__, "--one", kind, src],
                             capture_output=True, text=True, check=True,
                             env=dict(os.environ))
        rows.append(json.loads(res.stdout.strip().splitlines()[-1]))
        print(rows[-1], flush=True)
    with open(OUT, "w") as fh:
        json.dump({"migrate": migrate, "scenarios": rows,
                   "store": "597 objects x 2000 samples, 4 providers"}, fh, indent=1)
    print("migrate", migrate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
