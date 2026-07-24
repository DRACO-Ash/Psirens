from datetime import datetime, timedelta, timezone

from psirens.store import Store


def _rec(oid, epochs, mode="REAL", target=None):
    return {oid: {
        "name": oid, "data_mode": mode, "classification_marking": "U",
        "source": "T", "origin": "DEMO", "target": target,
        "samples": [{"epoch": e.isoformat(), "sub_lon_deg": 0.0,
                     "inclination_deg": 0.0} for e in epochs],
    }}


def test_load_empty_when_absent(tmp_path):
    s = Store(str(tmp_path))
    data = s.load()
    assert data["objects"] == {}


def test_merge_adds_and_dedups(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    e1, e2 = now - timedelta(days=2), now - timedelta(days=1)
    assert s.merge_samples(_rec("A", [e1]), retention_days=90, max_samples=100, now=now) == 1
    # second merge includes e1 again plus e2: only e2 is new
    added = s.merge_samples(_rec("A", [e1, e2]), retention_days=90, max_samples=100, now=now)
    assert added == 1
    assert len(s.load()["objects"]["A"]["samples"]) == 2


def test_merge_is_anti_shrink(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    s.merge_samples(_rec("A", [now - timedelta(days=1)]), retention_days=90, max_samples=100, now=now)
    s.merge_samples(_rec("B", [now - timedelta(days=1)]), retention_days=90, max_samples=100, now=now)
    # a pull that only contains B must not delete A
    s.merge_samples(_rec("B", [now]), retention_days=90, max_samples=100, now=now)
    objs = s.load()["objects"]
    assert "A" in objs and "B" in objs


def test_merge_prunes_old_samples_and_empty_objects(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=200)
    s.merge_samples(_rec("A", [old]), retention_days=90, max_samples=100, now=now)
    # only an out-of-window sample -> object pruned entirely
    assert "A" not in s.load()["objects"]


def test_merge_caps_samples_keeping_newest(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    epochs = [now - timedelta(days=d) for d in range(10, 0, -1)]
    s.merge_samples(_rec("A", epochs), retention_days=90, max_samples=3, now=now)
    kept = s.load()["objects"]["A"]["samples"]
    assert len(kept) == 3
    assert kept[-1]["epoch"] == epochs[-1].isoformat()  # newest retained


def test_meta_never_blanked_by_partial_pull(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    s.merge_samples(_rec("A", [now], target="43683"), retention_days=90, max_samples=100, now=now)
    partial = {"A": {"samples": [{"epoch": (now + timedelta(minutes=1)).isoformat(),
               "sub_lon_deg": 1.0, "inclination_deg": 0.0}]}}
    s.merge_samples(partial, retention_days=90, max_samples=100, now=now)
    assert s.load()["objects"]["A"]["target"] == "43683"


def test_remove_object(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    s.merge_samples(_rec("A", [now]), retention_days=90, max_samples=100, now=now)
    assert s.remove_object("A") is True
    assert "A" not in s.load()["objects"]
    assert s.remove_object("A") is False


def test_health_probe_writes(tmp_path):
    ok, detail = Store(str(tmp_path)).probe_write()
    assert ok and str(tmp_path) in detail


def test_atomic_write_leaves_no_tmp(tmp_path):
    s = Store(str(tmp_path))
    now = datetime.now(timezone.utc)
    s.merge_samples(_rec("A", [now]), retention_days=90, max_samples=100, now=now)
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
