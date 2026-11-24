"""Proof that the ledger holds exactly what the production store holds.

Drives the PRODUCTION `Store` and the prototype `LedgerStore` through the same
sequence of pulls and prunes, and after EVERY step compares (1) the two held
stores and (2) a fresh boot of the ledger from disk alone, which is what a
restart would see. Exercises every rule the merge carries: new samples, re-pulls
of held epochs, enrichment of a missing field, adoption of an untagged legacy
sample, age pruning, the per-object cap, object deletion by the high-interest
prune, and compaction part-way through.

    PYTHONPATH=src python3 proposals/2026-11-24-ledger-store/prototype/equivalence.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from ledger import LedgerStore  # noqa: E402

from psirens.store import SCHEMA_VERSION, Store  # noqa: E402

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
PROVIDERS = ("SAFRAN", "18SDS", "KBR")
RETENTION, CAP = 3, 120
PARTIAL: list = []   # samples first sent without RAAN, to be re-sent whole


def canon(data: dict) -> str:
    return json.dumps(data.get("objects", {}), sort_keys=True)


def legacy_seed(rng: random.Random, path: str) -> dict:
    """A store written before 1.7.4: untagged samples, some without RAAN."""
    objs = {}
    for i in range(30):
        samples = []
        for k in range(40):
            s = {"epoch": (T0 - timedelta(hours=2 * k)).isoformat(),
                 "sub_lon_deg": round(rng.uniform(-180, 180), 4),
                 "inclination_deg": round(rng.uniform(0, 5), 4)}
            if k % 3:
                s["raan_deg"] = round(rng.uniform(0, 360), 4)
            samples.append(s)
        samples.sort(key=lambda s: s["epoch"])
        objs[str(40000 + i)] = {"name": str(40000 + i), "data_mode": "REAL",
                                "samples": samples}
    with open(path, "w") as fh:
        json.dump({"schema": SCHEMA_VERSION, "objects": objs}, fh)
    return objs


def pull(rng: random.Random, step: int, legacy: dict) -> dict:
    now = T0 + timedelta(hours=6 * step)
    out = {}
    for i in range(30 + step % 4):           # objects come and go
        oid = str(40000 + i)
        samples = []
        for k in range(rng.randint(3, 12)):
            when = now - timedelta(hours=rng.randint(0, 24), minutes=rng.choice((0, 17, 43)))
            prov = rng.choice(PROVIDERS)
            row = {"epoch": when.isoformat(), "source": prov, "data_mode": "REAL",
                   "classification_marking": "U//PR-" + prov,
                   "sub_lon_deg": round(rng.uniform(-180, 180), 4),
                   "inclination_deg": round(rng.uniform(0, 5), 4),
                   "raan_deg": round(rng.uniform(0, 360), 4)}
            if rng.random() < 0.2:            # stored first without RAAN ...
                PARTIAL.append(dict(row))
                del row["raan_deg"]
            samples.append(row)
        while PARTIAL and rng.random() < 0.5:  # ... and completed by a later re-pull
            samples.append(PARTIAL.pop(0))
        old = legacy.get(oid, {}).get("samples") or []
        if old and step < 6:                 # re-pull reproduces a legacy record
            src = rng.choice(old)
            samples.append(dict(src, source="KBR", data_mode="REAL",
                                classification_marking="U//PR-KBR",
                                raan_deg=src.get("raan_deg", 12.5)))
        el = {"epoch": now.isoformat(), "source": rng.choice(PROVIDERS), "x": step}
        out[oid] = {"name": "SAT " + oid, "data_mode": "REAL", "samples": samples,
                    "elset": el, "elset_candidates": {el["source"]: el}}
    return out


def main() -> int:
    rng = random.Random(7)
    a_dir, b_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
    legacy = legacy_seed(rng, os.path.join(a_dir, "history.json"))
    with open(os.path.join(a_dir, "history.json")) as src, \
            open(os.path.join(b_dir, "history.json"), "w") as dst:
        dst.write(src.read())
    prod, led = Store(a_dir), LedgerStore(b_dir)
    checks = 0
    for step in range(40):
        now = T0 + timedelta(hours=6 * step)
        inc = pull(rng, step, legacy)
        got_a = prod.merge_samples(inc, retention_days=RETENTION, max_samples=CAP, now=now)
        got_b = led.merge_samples(json.loads(json.dumps(inc)), retention_days=RETENTION,
                                  max_samples=CAP, now=now)
        assert got_a == got_b, (step, "added", got_a, got_b)
        if step % 7 == 3:
            keep = {str(40000 + i) for i in range(30) if rng.random() > 0.1}
            assert prod.retain_only(keep) == led.retain_only(keep), (step, "retain")
        if step == 20:
            led.compact()
        held, rebooted = canon(prod.load()), canon(LedgerStore(b_dir).load())
        assert canon(led.load()) == held, (step, "held store differs")
        assert rebooted == held, (step, "a restart would see a different store")
        assert led.newest_epoch() == prod.newest_epoch(), (step, "newest")
        checks += 4
        if step == 5:   # the legacy path must really have been exercised
            adopted = sum(1 for o in prod.load()["objects"].values() for s in o["samples"]
                          if s.get("p") is not None and s["epoch"] <= T0.isoformat())
            assert adopted > 0, "no legacy sample was adopted: the proof is vacuous"
            filled = sum(1 for o in led.load()["objects"].values() for s in o["samples"]
                         if s.get("p") is not None and "raan_deg" in s)
            assert filled, "no sample was enriched: the proof is vacuous"
    objs = prod.load()["objects"]
    print("EQUIVALENT: %d checks over 40 pulls, %d objects held, %d samples, "
          "%d legacy samples adopted" % (checks, len(objs),
                                         sum(len(o["samples"]) for o in objs.values()), adopted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
