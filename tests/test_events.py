"""Event detection (1.7.0): the Shift Brief's detectors, the drift fix and the
/api/events contract.

The synthetic objects carry what real GEO data carries and what fooled the
1.6.10 drift rate: eccentricity libration at the SIDEREAL period, a bias
between two providers, per-fix scatter, and ragged sampling with dropouts.
Every generator is seeded, so a failure reproduces.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from psirens.astro import SIDEREAL_DAY_DAYS
from psirens.astro import unwrap
from psirens.events import (EventEngine, Thresholds, _Head, detect, ew_kind,
                            ew_split, first_departure, latest_splits, ns_step,
                            projection, series)
from psirens.config import Config
from psirens.main import _drift_block, create_app
from psirens.models import SCHEMA_VERSION

END = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
TH = Thresholds()


def _cfg(tmp_path) -> Config:
    return Config(
        port=8080, allowed_origin="", team_token="",
        retention_days=90, refresh_seconds=3600,
        lon_min=-180.0, lon_max=180.0, inc_min=0.0, inc_max=15.0,
        max_samples_per_object=2000, demo_mode=True, udl_enabled=False,
        udl_base_url="", udl_elset_path="/udl/elset", udl_user="",
        udl_password="", udl_target_field="tags", udl_epoch_param="epoch",
        udl_accept="application/json", data_dir=str(tmp_path),
        scheduler_enabled=False,
    )


def _synth(rng: random.Random, *, days: float = 6.0, drift: float = 0.0,
           burn: datetime | None = None, dr: float = 0.0, di: float = 0.0,
           lon0: float | None = None, end: datetime = END) -> list[dict]:
    e = rng.uniform(0.0002, 0.003)
    amp, ph = math.degrees(2 * e), rng.uniform(0, 2 * math.pi)
    bias = {0: 0.0, 1: rng.uniform(-0.012, 0.012)}
    sp, si = rng.uniform(0.004, 0.02), rng.uniform(0.001, 0.004)
    inc0 = rng.uniform(0.02, 4.0)
    lon0 = rng.uniform(-170, 170) if lon0 is None else lon0
    start = end - timedelta(days=days)
    out, t = [], start
    while t <= end:
        if rng.random() > 0.3:
            d = (t - start).total_seconds() / 86400
            lon = lon0 + drift * d + amp * math.sin(2 * math.pi * d / SIDEREAL_DAY_DAYS + ph)
            inc = inc0
            if burn is not None and t >= burn:
                db = (t - burn).total_seconds() / 86400
                lon += dr * db
                inc += di
            prov = rng.randint(0, 1)
            out.append({"epoch": t.isoformat(),
                        "sub_lon_deg": (lon + bias[prov] + rng.gauss(0, sp) + 180) % 360 - 180,
                        "inclination_deg": inc + rng.gauss(0, si)})
        t += timedelta(minutes=rng.choice([45, 60, 60, 75, 90]))
    return out


def _lon(samples: list[dict]):
    return unwrap(series(samples, "sub_lon_deg"))


def _store(objects: dict[str, list[dict]]) -> dict:
    return {"schema": SCHEMA_VERSION,
            "objects": {oid: {"name": oid, "data_mode": "REAL", "samples": s}
                        for oid, s in objects.items()}}


def test_quiet_objects_raise_no_events():
    rng = random.Random(20261116)
    data = _store({f"Q{i}": _synth(rng) for i in range(40)})
    out = detect(data, {}, TH, now=END, stale_after_hours=24)
    assert out["events"] == [], [(e["object_id"], e["type"]) for e in out["events"]]


@pytest.mark.parametrize("seed", range(6))
def test_a_burn_is_confirmed_at_the_right_time_and_rate(seed):
    rng = random.Random(seed)
    burn = END - timedelta(hours=rng.uniform(40, 80))
    split = ew_split(_lon(_synth(rng, burn=burn, dr=-0.3)), END, TH)
    assert split is not None and not split.edge
    assert abs((split.when - burn).total_seconds()) / 3600 <= 6.0
    assert split.after.rate_deg_per_day == pytest.approx(-0.3, abs=0.03)
    assert ew_kind(split.before.rate_deg_per_day, split.after.rate_deg_per_day, 0.02) == "STARTED"


def test_the_edge_rule_never_confirms_a_burn_at_the_data_edge():
    """RED CASE from the prototype: a 1.0 deg/day burn 7 h before the end was
    CONFIRMED 17 h early at 0.31 deg/day. The split still peaks at the edge
    (the trap is in the data); it must come back as SUSPECTED, never CONFIRMED."""
    rng = random.Random(4)
    samples = _synth(rng, burn=END - timedelta(hours=7), dr=1.0)
    split = ew_split(_lon(samples), END, TH)
    assert split is not None and split.edge, "the data no longer contains the trap"
    out = detect(_store({"T": samples}), {}, TH, now=END, stale_after_hours=24)
    kinds = {(e["type"], e["tier"]) for e in out["events"]}
    assert not any(t in ("STARTED", "EW", "STOPPED") for t, _ in kinds), kinds
    assert ("LEAVING", "SUSPECTED") in kinds


def test_a_drift_that_stops_is_reported_as_stopped():
    rng = random.Random(11)
    burn = END - timedelta(hours=60)
    samples = _synth(rng, drift=0.25, burn=burn, dr=-0.25)
    out = detect(_store({"S": samples}), {}, TH, now=END, stale_after_hours=24)
    ev = [e for e in out["events"] if e["type"] == "STOPPED"]
    assert ev and ev[0]["rate_before"] == pytest.approx(0.25, abs=0.03)
    assert abs(ev[0]["rate_after"]) < 0.02
    assert ev[0]["evidence"]["before"]["rate_deg_per_day"] == pytest.approx(0.25, abs=0.03)
    known = datetime.fromisoformat(ev[0]["detectable_from"])
    assert known - datetime.fromisoformat(ev[0]["epoch"]) == timedelta(hours=24)


def test_an_inclination_step_is_an_ns_event():
    rng = random.Random(3)
    burn = END - timedelta(hours=58)
    inc = series(_synth(rng, burn=burn, di=0.03), "inclination_deg")
    found = ns_step(inc, END, TH)
    assert found is not None and found[1] == pytest.approx(0.03, abs=0.005)


def test_an_ns_event_carries_its_evidence_through_detect():
    rng = random.Random(3)
    samples = _synth(rng, burn=END - timedelta(hours=58), di=0.03)
    out = detect(_store({"N": samples}), {}, TH, now=END, stale_after_hours=24)
    ns = [e for e in out["events"] if e["type"] == "NS"]
    assert len(ns) == 1 and ns[0]["delta_inc_deg"] == pytest.approx(0.03, abs=0.005)
    ev = ns[0]["evidence"]
    assert ev["quantity"] == "inclination"
    assert ev["after"]["y0"] - ev["before"]["y0"] == pytest.approx(0.03, abs=0.005)
    known = datetime.fromisoformat(ns[0]["detectable_from"]) - datetime.fromisoformat(ns[0]["epoch"])
    assert known == timedelta(hours=12)


def test_ew_kind_names_every_case():
    assert ew_kind(0.0, -0.4, 0.02) == "STARTED"
    assert ew_kind(0.3, 0.001, 0.02) == "STOPPED"
    assert ew_kind(0.3, -0.3, 0.02) == "EW"


def test_early_warning_ignores_a_track_that_keeps_its_line():
    rng = random.Random(9)
    assert first_departure(_lon(_synth(rng, drift=0.4)), None, TH) is None


def test_a_silent_object_goes_quiet_only_while_the_belt_is_current():
    rng = random.Random(5)
    loud = _synth(rng)
    silent = [s for s in _synth(rng) if datetime.fromisoformat(s["epoch"]) < END - timedelta(hours=19)]
    data = _store({"A": loud, "B": silent})
    out = detect(data, {}, TH, now=END, stale_after_hours=24)
    quiet = [e for e in out["events"] if e["type"] == "QUIET"]
    assert [e["object_id"] for e in quiet] == ["B"] and quiet[0]["age_hours"] >= 19
    stale = detect(data, {}, TH, now=END + timedelta(days=3), stale_after_hours=24)
    assert stale["belt_current"] is False
    assert not [e for e in stale["events"] if e["type"] == "QUIET"]


def test_a_sparse_reporter_is_not_quiet_just_because_it_is_sparse():
    rng = random.Random(6)
    sparse = _synth(rng)[::20]   # roughly one fix a day: its normal cadence
    sparse = [s for s in sparse if datetime.fromisoformat(s["epoch"]) < END - timedelta(hours=14)]
    out = detect(_store({"A": _synth(rng), "B": sparse}), {}, TH, now=END, stale_after_hours=24)
    assert not [e for e in out["events"] if e["type"] == "QUIET"]


def test_projection_names_the_nearest_object_ahead_at_current_rates():
    heads = [_Head("me", 10.0, -0.5), _Head("ahead", 8.0, 0.0),
             _Head("further", 5.0, 0.0), _Head("behind", 12.0, 0.0)]
    p = projection("me", 10.0, -0.5, heads, 0.02)
    assert p is not None and p["object_id"] == "ahead" and p["days"] == pytest.approx(4.0)
    assert projection("me", 10.0, 0.01, heads, 0.02) is None           # below the floor
    assert projection("me", 10.0, -0.5, [_Head("x", 8.0, -0.6)], 0.02) is None  # pulling away


def test_projection_skips_an_object_at_the_same_longitude():
    """The float-equality guard was removed in 1.7.2 (python:S1244); a
    co-located object must still never be offered as the one being closed on."""
    assert projection("me", 10.0, -0.5, [_Head("same", 10.0, 0.0)], 0.02) is None
    near = projection("me", 10.0, -0.5, [_Head("same", 10.0, 0.0), _Head("t", 9.0, 0.0)], 0.02)
    assert near is not None and near["object_id"] == "t"


def test_projection_crosses_the_seam():
    p = projection("me", -179.0, -0.5, [_Head("t", 179.0, 0.0)], 0.02)
    assert p is not None and p["gap_deg"] == pytest.approx(2.0)


def test_latest_splits_takes_the_newest_confirmed_ew_event_only():
    evs = [{"object_id": "A", "type": "STARTED", "tier": "CONFIRMED", "epoch": "2026-11-14T00:00:00+00:00"},
           {"object_id": "A", "type": "STOPPED", "tier": "CONFIRMED", "epoch": "2026-11-15T00:00:00+00:00"},
           {"object_id": "A", "type": "LEAVING", "tier": "SUSPECTED", "epoch": "2026-11-16T00:00:00+00:00"},
           {"object_id": "B", "type": "NS", "tier": "CONFIRMED", "epoch": "2026-11-15T00:00:00+00:00"}]
    assert latest_splits(evs) == {"A": datetime(2026, 11, 15, tzinfo=timezone.utc)}


def test_detect_on_an_empty_store():
    out = detect({"objects": {}}, {}, TH, now=END, stale_after_hours=24)
    assert out["events"] == [] and out["reference_epoch"] is None


# -- the drift fix ------------------------------------------------------------
def _burnt(seed: int = 21) -> list[dict]:
    rng = random.Random(seed)
    return _synth(rng, burn=END - timedelta(hours=30), dr=-0.6)


def test_the_straddling_fit_really_does_understate_the_new_rate():
    """RED CASE: the trap must stay in the data, or the fix test below could
    start passing against harmless data."""
    pairs = [(datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in _burnt()]
    straddled = _drift_block(pairs)["drift_deg_per_day"]
    assert straddled is not None and abs(straddled) < 0.4


def test_the_drift_is_fitted_after_the_event_not_across_it():
    samples = _burnt()
    split = ew_split(_lon(samples), END, TH)
    assert split is not None and not split.edge
    pairs = [(datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in samples]
    block = _drift_block(pairs, split.when)
    assert block["drift_deg_per_day"] == pytest.approx(-0.6, abs=0.05)
    assert block["drift_since_event"] == split.when.isoformat()


# -- the engine and the API -----------------------------------------------------
def _write(tmp_path, objects: dict[str, list[dict]]) -> None:
    with open(os.path.join(tmp_path, "history.json"), "w", encoding="utf-8") as fh:
        json.dump(_store(objects), fh)


@pytest.fixture
def event_client(tmp_path):
    """Real HRR ids from the bundled snapshot, so names and ranks join:
    50322 SHIYAN 12 02 (rank 1) burns towards 49330 SJ-21 (rank 2);
    39216 INSAT 3D is rank 4 and burns too, to prove the rank filter."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    rng = random.Random(77)
    _write(tmp_path, {
        "50322": _synth(rng, burn=now - timedelta(hours=40), dr=-0.6, lon0=-47.0, end=now),
        "49330": _synth(rng, lon0=-50.0, end=now),
        "39216": _synth(rng, burn=now - timedelta(hours=50), dr=0.4, lon0=80.0, end=now),
    })
    app = create_app(_cfg(tmp_path), sources=[])
    with TestClient(app) as c:
        yield c


def test_events_endpoint_shape_names_and_projection(event_client):
    r = event_client.get("/api/events?view=real")
    assert r.status_code == 200
    body = r.json()
    by = {e["object_id"]: e for e in body["events"]}
    assert set(by) == {"50322", "39216"}, [(e["object_id"], e["type"]) for e in body["events"]]
    ev = by["50322"]
    assert ev["type"] == "STARTED" and ev["tier"] == "CONFIRMED"
    assert ev["name"] == "SHIYAN 12 02 (SY-12 02)" and ev["rank"] == 1 and ev["country"] == "CHN"
    assert ev["projection"]["name"] == "SJ-21"
    assert {"series", "split", "before", "after"} <= set(ev["evidence"])
    assert body["thresholds"]["ew_floor_deg_per_day"] == 0.02 and body["belt_current"] is True


def test_events_follow_the_rank_filter_and_cache_with_etag(event_client):
    narrow = event_client.get("/api/events?view=real&ranks=1,2,3")
    assert [e["object_id"] for e in narrow.json()["events"]] == ["50322"]
    again = event_client.get("/api/events?view=real&ranks=1,2,3",
                             headers={"If-None-Match": narrow.headers["ETag"]})
    assert again.status_code == 304
    assert event_client.get("/api/events?view=real&ranks=1").headers["ETag"] != narrow.headers["ETag"]


def test_events_reject_bad_input(event_client):
    assert event_client.get("/api/events?view=nope").status_code == 400
    assert event_client.get("/api/events?ranks=9").status_code == 400
    assert event_client.get("/api/events?modes=BOGUS").status_code == 400


def test_sim_view_sees_no_real_events(event_client):
    assert event_client.get("/api/events?view=sim").json()["events"] == []


def test_tracks_carry_the_post_event_drift(event_client):
    by = {t["object_id"]: t for t in event_client.get("/api/tracks?view=real").json()["tracks"]}
    assert by["50322"]["drift_since_event"] is not None
    assert by["50322"]["drift_deg_per_day"] == pytest.approx(-0.6, abs=0.06)
    assert by["49330"]["drift_since_event"] is None


def test_events_are_computed_once_not_per_request(event_client):
    engine = event_client.app.state.events
    event_client.get("/api/events")
    event_client.get("/api/tracks")
    event_client.get("/api/events?ranks=1")
    assert engine.computations == 1


def test_the_engine_recomputes_after_the_store_changes(tmp_path):
    rng = random.Random(1)
    _write(tmp_path, {"A": _synth(rng)})
    engine = EventEngine(TH, 24)

    class _S:
        path = os.path.join(tmp_path, "history.json")

        @staticmethod
        def load():
            with open(_S.path, encoding="utf-8") as fh:
                return json.load(fh)

    engine.current(_S, {}, now=END)
    engine.current(_S, {}, now=END)
    assert engine.computations == 1
    _write(tmp_path, {"A": _synth(rng), "B": _synth(rng)})
    os.utime(_S.path, ns=(1, 1))
    engine.current(_S, {}, now=END)
    assert engine.computations == 2


def test_the_refresh_cycle_warms_the_engine_and_survives_a_failing_hook(tmp_path):
    from psirens.sources import DemoElsetSource
    app = create_app(_cfg(tmp_path), sources=[DemoElsetSource()])
    app.state.refresher.run_once()
    assert app.state.events.computations == 1

    def _boom():
        raise RuntimeError("hook failure")

    app.state.refresher.on_cycle = _boom
    assert app.state.refresher.run_once()["status"] == "ok"
