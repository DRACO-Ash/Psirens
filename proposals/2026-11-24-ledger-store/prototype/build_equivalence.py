"""The built ledger (1.7.13) against the 1.7.12 store, byte for byte from git.

Loads `src/psirens/store.py` as it was at the 1.7.12 commit (not a
reconstruction: the 1.6.2 lesson) beside the shipped one, drives both through
the same pulls and prunes as `equivalence.py`, and after every step compares
the two held stores and a fresh boot of the new one from disk alone.

    PYTHONPATH=src python3 proposals/2026-11-24-ledger-store/prototype/build_equivalence.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
import equivalence as E  # noqa: E402  (the same pulls the prototype was proven on)

from psirens.store import Store  # noqa: E402

COMMIT_1_7_12 = "d3c8f28"


def old_store_class():
    src = subprocess.run(["git", "show", COMMIT_1_7_12 + ":src/psirens/store.py"],
                         capture_output=True, text=True, check=True).stdout
    spec = importlib.util.spec_from_loader("psirens.store_1_7_12", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "psirens"
    exec(compile(src, "store.py@" + COMMIT_1_7_12, "exec"), mod.__dict__)
    return mod.Store


def main() -> int:
    import random
    Old = old_store_class()
    rng = random.Random(7)
    a_dir, b_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
    legacy = E.legacy_seed(rng, os.path.join(a_dir, "history.json"))
    with open(os.path.join(a_dir, "history.json")) as src, \
            open(os.path.join(b_dir, "history.json"), "w") as dst:
        dst.write(src.read())
    old, new = Old(a_dir), Store(b_dir)
    checks = 0
    for step in range(40):
        now = E.T0 + timedelta(hours=6 * step)
        inc = E.pull(rng, step, legacy)
        got_a = old.merge_samples(inc, retention_days=E.RETENTION, max_samples=E.CAP, now=now)
        got_b = new.merge_samples(json.loads(json.dumps(inc)), retention_days=E.RETENTION,
                                  max_samples=E.CAP, now=now)
        assert got_a == got_b, (step, "added", got_a, got_b)
        if step % 7 == 3:
            keep = {str(40000 + i) for i in range(30) if rng.random() > 0.1}
            assert old.retain_only(keep) == new.retain_only(keep), (step, "retain")
        if step == 20:
            new.compact()
        held = E.canon(old.load())
        assert E.canon(new.load()) == held, (step, "held store differs from 1.7.12")
        assert E.canon(Store(b_dir).load()) == held, (step, "a restart would differ")
        assert new.newest_epoch() == old.newest_epoch(), (step, "newest")
        checks += 4
    print("EQUIVALENT TO 1.7.12 (%s): %d checks over 40 pulls" % (COMMIT_1_7_12, checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
