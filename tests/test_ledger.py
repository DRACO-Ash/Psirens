"""The ledger store (1.7.13): the history held once, only changes written.

Proposal of 24 November 2026, approved by Ash the same day. The merge rules
are the ones the rest of the suite already pins; what these tests pin is what
the ledger adds: a restart sees exactly what was held, a change writes only
what changed, health reads no file, a view taken before a change is never
altered by it, torn files are ignored, a history.json written by someone else
(a rollback) is adopted, and a mount that refuses deletes still works.
"""

from __future__ import annotations

import errno
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from psirens import store as store_mod
from psirens.agreement import Policy, resolve_data
from psirens.models import SCHEMA_VERSION
from psirens.store import Store

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
PROVIDERS = ("SAFRAN", "18SDS", "KBR")


def canon(data: dict) -> str:
    return json.dumps(data.get("objects", {}), sort_keys=True)


def seed_legacy(path: str, rng: random.Random, per: int = 30) -> dict:
    """A history.json written before 1.7.4: untagged samples, some without
    RAAN, so adoption and enrichment both have work to do."""
    objs = {}
    for i in range(12):
        samples = []
        for k in range(per):
            s = {"epoch": (T0 - timedelta(hours=2 * k)).isoformat(),
                 "sub_lon_deg": round(rng.uniform(-180, 180), 4),
                 "inclination_deg": round(rng.uniform(0, 5), 4)}
            if k % 3:
                s["raan_deg"] = round(rng.uniform(0, 360), 4)
            samples.append(s)
        samples.sort(key=lambda s: s["epoch"])
        objs[str(40000 + i)] = {"name": str(40000 + i), "data_mode": "REAL", "samples": samples}
    with open(path, "w") as fh:
        json.dump({"schema": SCHEMA_VERSION, "objects": objs}, fh)
    return objs


def a_pull(rng: random.Random, step: int, legacy: dict, partial: list) -> dict:
    now = T0 + timedelta(hours=6 * step)
    out = {}
    for i in range(12 + step % 3):
        oid = str(40000 + i)
        samples = []
        for _ in range(rng.randint(2, 8)):
            prov = rng.choice(PROVIDERS)
            row = {"epoch": (now - timedelta(hours=rng.randint(0, 24))).isoformat(),
                   "source": prov, "data_mode": "REAL", "classification_marking": "U//PR-" + prov,
                   "sub_lon_deg": round(rng.uniform(-180, 180), 4),
                   "inclination_deg": round(rng.uniform(0, 5), 4),
                   "raan_deg": round(rng.uniform(0, 360), 4)}
            if rng.random() < 0.2:          # first sent without RAAN ...
                partial.append(dict(row))
                del row["raan_deg"]
            samples.append(row)
        while partial and rng.random() < 0.5:   # ... completed by a later pull
            samples.append(partial.pop(0))
        old = legacy.get(oid, {}).get("samples") or []
        if old and step < 5:                # a re-pull reproduces a legacy record
            src = rng.choice(old)
            samples.append(dict(src, source="KBR", data_mode="REAL",
                                classification_marking="U//PR-KBR",
                                raan_deg=src.get("raan_deg", 12.5)))
        el = {"epoch": now.isoformat(), "source": rng.choice(PROVIDERS), "step": step}
        out[oid] = {"name": "SAT " + oid, "data_mode": "REAL", "samples": samples,
                    "elset": el, "elset_candidates": {el["source"]: el}}
    return out


def merge(store: Store, inc: dict, step: int) -> int:
    return store.merge_samples(inc, retention_days=3, max_samples=60,
                               now=T0 + timedelta(hours=6 * step))


# -- the core promise --------------------------------------------------------------
def test_a_restart_sees_exactly_what_was_held(tmp_path):
    rng = random.Random(7)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store, partial, adopted = Store(str(tmp_path)), [], 0
    for step in range(30):
        merge(store, a_pull(rng, step, legacy, partial), step)
        if step % 7 == 3:
            store.retain_only({str(40000 + i) for i in range(12) if rng.random() > 0.1})
        if step == 15:
            store.compact()
        if step == 4:
            adopted = sum(1 for o in store.load()["objects"].values() for s in o["samples"]
                          if s.get("p") is not None and s["epoch"] <= T0.isoformat())
        assert canon(Store(str(tmp_path)).load()) == canon(store.load()), step
    assert adopted > 0, "no legacy sample was adopted: the test proves nothing"


def test_a_merge_writes_only_what_changed(tmp_path):
    rng = random.Random(3)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng, per=400)
    store = Store(str(tmp_path))
    store.load()
    history = store_mod._sig(store.path)
    store.merge_samples(a_pull(rng, 0, legacy, []), retention_days=90, max_samples=2000,
                        now=T0)
    whole = len(json.dumps(store.load(), separators=(",", ":")))
    assert 0 < store.last_write_bytes < whole / 20
    assert store_mod._sig(store.path) == history, "a merge rewrote history.json"


def test_health_reads_no_file(tmp_path, monkeypatch):
    rng = random.Random(5)
    seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    newest = store.newest_epoch()

    def refuse(*_a, **_k):
        raise AssertionError("health parsed a file")
    monkeypatch.setattr(store_mod, "_read_json", refuse)
    monkeypatch.setattr(store_mod.json, "load", refuse)
    for _ in range(5):
        assert store.newest_epoch() == newest
        assert store.version >= 1
        assert store.stats()["backend"] == "ledger"


def test_a_view_taken_before_a_change_is_never_altered(tmp_path):
    rng = random.Random(11)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    view = resolve_data(store.load(), Policy())
    raw = store.load()["objects"]["40000"]
    before_view, before_raw = json.dumps(view, sort_keys=True), json.dumps(raw, sort_keys=True)
    partial: list = []
    for step in range(4):
        merge(store, a_pull(rng, step, legacy, partial), step)
    assert json.dumps(view, sort_keys=True) == before_view
    assert json.dumps(raw, sort_keys=True) == before_raw
    assert store.load()["objects"]["40000"] is not raw


# -- files that are torn, refused or written by someone else ------------------------
def test_a_torn_segment_is_ignored_not_half_applied(tmp_path):
    rng = random.Random(2)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    merge(store, a_pull(rng, 0, legacy, []), 0)
    held = canon(store.load())
    merge(store, a_pull(rng, 1, legacy, []), 1)
    seg = store._files(store_mod._SEG)[-1][1]
    with open(seg, "r+", encoding="utf-8") as fh:
        text = fh.read()
        fh.seek(0)
        fh.truncate()
        fh.write(text[: len(text) // 2])
    assert canon(Store(str(tmp_path)).load()) == held


def test_a_torn_snapshot_falls_back_to_the_one_before(tmp_path, monkeypatch):
    rng = random.Random(4)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    merge(store, a_pull(rng, 0, legacy, []), 0)
    monkeypatch.setattr(store_mod.os, "remove", _refuse_delete)
    store.compact()                      # a mount without delete keeps the old files
    held = canon(store.load())
    newest = store._files(store_mod._SNAP)[-1][1]
    with open(newest, "wb") as fh:
        fh.write(b"\x1f\x8b not a whole gzip stream")
    monkeypatch.undo()
    assert canon(Store(str(tmp_path)).load()) == held


def _refuse_delete(path: str) -> None:
    raise PermissionError(errno.EPERM, "delete not allowed on this mount", path)


def test_a_mount_that_refuses_deletes_still_works(tmp_path, monkeypatch):
    rng = random.Random(6)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    monkeypatch.setattr(store_mod.os, "remove", _refuse_delete)
    store = Store(str(tmp_path))
    for step in range(3):
        merge(store, a_pull(rng, step, legacy, []), step)
        store.compact()
    assert canon(Store(str(tmp_path)).load()) == canon(store.load())


def test_a_mount_that_refuses_rename_still_gets_history_json(tmp_path, monkeypatch):
    rng = random.Random(8)
    seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    store.load()

    def no_rename(*_a, **_k):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")
    monkeypatch.setattr(store_mod.os, "replace", no_rename)
    store.compact()
    with open(store.path, encoding="utf-8") as fh:
        assert canon(json.load(fh)) == canon(store.load())


def test_a_history_file_written_after_the_ledger_is_adopted(tmp_path):
    """A rollback to 1.7.12 writes history.json; coming back must not discard
    what that build stored."""
    rng = random.Random(9)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    merge(store, a_pull(rng, 0, legacy, []), 0)
    rolled = {"schema": SCHEMA_VERSION, "objects": {"99999": {"name": "ROLLBACK", "samples": [
        {"epoch": T0.isoformat(), "sub_lon_deg": 1.0, "inclination_deg": 0.1}]}}}
    with open(store.path, "w", encoding="utf-8") as fh:
        json.dump(rolled, fh)
    future = time.time() + 60
    os.utime(store.path, (future, future))
    assert set(Store(str(tmp_path)).load()["objects"]) == {"99999"}
    assert set(store.load()["objects"]) == {"99999"}, "the running store missed it"


def test_the_first_start_adopts_the_legacy_file_once(tmp_path):
    rng = random.Random(10)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path))
    assert store._data is None, "nothing is read before first use (gunicorn --preload)"
    assert set(store.load()["objects"]) == set(legacy)
    assert store._files(store_mod._SNAP), "the adoption left no snapshot"
    again = Store(str(tmp_path))
    assert canon(again.load()) == canon(store.load())


def test_compaction_writes_history_and_a_snapshot_and_retires_the_rest(tmp_path):
    rng = random.Random(12)
    legacy = seed_legacy(os.path.join(tmp_path, "history.json"), rng)
    store = Store(str(tmp_path), compact_hours=24)
    for step in range(3):
        merge(store, a_pull(rng, step, legacy, []), step)
    assert store.stats()["segments_since_snapshot"] == 3
    assert not store.compaction_due()
    store.compact()
    with open(store.path, encoding="utf-8") as fh:
        assert canon(json.load(fh)) == canon(store.load())
    assert not store._files(store_mod._SEG)
    assert len(store._files(store_mod._SNAP)) == 1
    assert store.stats()["segments_since_snapshot"] == 0
    assert Store(str(tmp_path), compact_hours=0).compaction_due() is False  # not booted yet
    due = Store(str(tmp_path), compact_hours=0)
    due.load()
    assert due.compaction_due()


def test_meta_reports_the_ledger(tmp_path):
    from fastapi.testclient import TestClient

    from psirens.config import load_config
    from psirens.main import create_app
    cfg = load_config()
    object.__setattr__(cfg, "data_dir", str(tmp_path))
    object.__setattr__(cfg, "scheduler_enabled", False)
    with TestClient(create_app(cfg, sources=[])) as c:
        block = c.get("/api/meta").json()["store"]
    assert block["backend"] == "ledger"
    assert {"version", "snapshot_age_hours", "segments_since_snapshot",
            "last_write_bytes", "boot_seconds"} <= set(block)


@pytest.mark.parametrize("due", [True, False])
def test_the_scheduler_compacts_only_when_due(due):
    import asyncio

    from psirens.refresh import Refresher

    class Fake:
        compacted = 0

        def compaction_due(self) -> bool:
            return due

        def compact(self) -> int:
            Fake.compacted += 1
            return 1

    ref = Refresher.__new__(Refresher)
    ref.store = Fake()
    asyncio.run(ref._compact_if_due())
    assert Fake.compacted == (1 if due else 0)
