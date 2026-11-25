"""The three provenance rules of 1.7.4, each pinned through the real modules.

Owner rule, 17 November 2026: "The UDL is the primary source of information,
we need to make calculations based on that information. We are not in the
business of making this up." Built from it:

  1. every position carries the provider, data mode and marking of the one
     record it was calculated from;
  2. nothing is filled in: an incomplete or unmarked record is rejected and
     counted, never completed with a default;
  3. synthetic data never reaches a live store.

`audits/2026-11-17-tle-provenance/prove.py` reproduced the defects these
replace; each test below names the audit finding it closes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from psirens.config import Config
from psirens.main import _build_sources, _tracks_payload, create_app
from psirens.models import DataMode
from psirens.refresh import Refresher
from psirens.security import SingleFlight
from psirens.sources import (DEMO_ID_PREFIX, DemoElsetSource, ManualElsetSource,
                             UDLElsetSource, parse_row)
from psirens.store import Store, sample_provenance

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _cfg(tmp_path, **kw) -> Config:
    base = dict(
        port=8080, allowed_origin="", team_token="", retention_days=90,
        refresh_seconds=3600, lon_min=-180.0, lon_max=180.0, inc_min=0.0,
        inc_max=15.0, max_samples_per_object=2000, demo_mode=False,
        udl_enabled=True, udl_base_url="https://udl.example.test",
        udl_elset_path="/udl/elset", udl_user="u", udl_password="p",
        udl_target_field="tags", udl_epoch_param="epoch",
        udl_accept="application/json", data_dir=str(tmp_path),
        scheduler_enabled=False, udl_slice_pause_seconds=0)
    base.update(kw)
    return Config(**base)


def _row(oid="64467", *, source="SAFRAN", epoch=None, mode="REAL",
         marking="U//PR-SAFRAN", mean_anomaly=208.6412, **over) -> dict:
    row = {"satNo": oid, "epoch": (epoch or NOW).isoformat().replace("+00:00", "Z"),
           "inclination": 0.3274, "eccentricity": 0.0007366, "raan": 86.4242,
           "argOfPerigee": 37.1819, "meanAnomaly": mean_anomaly,
           "meanMotion": 1.00265344, "bStar": -6.3064e-5, "source": source,
           "dataMode": mode, "classificationMarking": marking}
    row.update(over)
    return {k: v for k, v in row.items() if v is not None}


def _fetch(tmp_path, rows):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=rows)))
    src = UDLElsetSource(_cfg(tmp_path), client=client)
    return src, src.fetch(NOW - timedelta(hours=1), NOW)


def _ingest(tmp_path, rows) -> Store:
    _, recs = _fetch(tmp_path, rows)
    store = Store(str(tmp_path))
    store.merge_samples(recs, retention_days=90, max_samples=2000, now=NOW)
    return store


# -- 1. every position is tagged ----------------------------------------
def test_every_sample_carries_its_records_provider_mode_and_marking(tmp_path):
    """F2: until 1.7.4 a stored sample said nothing about where it came from."""
    store = _ingest(tmp_path, [_row(source="SAFRAN"),
                               _row(source="NorthStar", marking="U//PR-NORTHSTAR-ELSET",
                                    epoch=NOW - timedelta(minutes=30))])
    obj = store.load()["objects"]["64467"]
    tags = {sample_provenance(obj, s)["source"]: sample_provenance(obj, s) for s in obj["samples"]}
    assert tags["SAFRAN"] == {"source": "SAFRAN", "data_mode": "REAL",
                              "classification_marking": "U//PR-SAFRAN"}
    assert tags["NorthStar"]["classification_marking"] == "U//PR-NORTHSTAR-ELSET"
    assert all("source" not in s for s in obj["samples"])   # a table, not 2000 copies


def test_two_providers_at_one_instant_stay_two_observations(tmp_path):
    """F1 at its smallest: KBR, NorthStar and EXO carried the SAME epoch to the
    millisecond in Ash's comparison. Keyed on epoch alone, the first won and
    the other was merged into it."""
    store = _ingest(tmp_path, [_row(source="KBR"),
                               _row(source="NorthStar", mean_anomaly=124.4911)])
    samples = store.load()["objects"]["64467"]["samples"]
    assert len(samples) == 2
    assert len({round(s["sub_lon_deg"]) for s in samples}) == 2   # nothing blended


def test_a_legacy_sample_is_tagged_only_when_a_re_pull_matches_it_exactly(tmp_path):
    """Provenance is attached on evidence. An untagged pre-1.7.4 sample takes
    the tags of a re-pulled record that reproduces it exactly, and of nothing
    else."""
    store = _ingest(tmp_path, [_row(source="SAFRAN")])
    obj = store.load()["objects"]["64467"]
    tagged = dict(obj["samples"][0])
    legacy = {k: v for k, v in tagged.items() if k != "p"}       # as 1.7.3 stored it
    other = {**legacy, "epoch": (NOW - timedelta(hours=1)).isoformat()}
    raw = {"objects": {"64467": {"name": "x", "data_mode": "REAL", "origin": "UDL",
                                  "samples": [legacy, other]}}}
    store._write_atomic(raw)
    _, recs = _fetch(tmp_path, [_row(source="SAFRAN"),
                                _row(source="NorthStar", mean_anomaly=124.4911,
                                     epoch=NOW - timedelta(hours=1))])
    added = store.merge_samples(recs, retention_days=90, max_samples=2000, now=NOW)
    obj = store.load()["objects"]["64467"]
    by_epoch = {}
    for s in obj["samples"]:
        by_epoch.setdefault(s["epoch"], []).append((sample_provenance(obj, s) or {}).get("source"))
    assert by_epoch[legacy["epoch"]] == ["SAFRAN"]                  # adopted, not duplicated
    # Different values at the same instant: a different record. The legacy
    # sample stays untagged and NorthStar's is stored beside it.
    assert sorted(map(str, by_epoch[other["epoch"]])) == ["None", "NorthStar"]
    assert added == 1                                               # only NorthStar is new


def test_the_real_view_serves_only_samples_tagged_real(tmp_path):
    """F10: an EXERCISE record under a real catalogue number used to join the
    REAL track and, arriving last, relabel the whole object."""
    store = _ingest(tmp_path, [_row(epoch=NOW - timedelta(hours=2)),
                               _row(source="EXERCISE-CELL", mode="EXERCISE", marking="U")])
    cfg = _cfg(tmp_path)
    real = _tracks_payload(cfg, store, "real", {DataMode.REAL})["tracks"][0]
    assert real["data_mode"] == "REAL"
    assert set(real["providers"]) == {"SAFRAN"}
    # 1.7.5: one stream per track. REAL before any other mode, so the combined
    # view no longer stacks an EXERCISE fix on a REAL object.
    combined = _tracks_payload(cfg, store, "combined", set(DataMode))["tracks"][0]
    assert combined["data_mode"] == "REAL" and combined["head_source"] == "SAFRAN"
    exercise = _tracks_payload(cfg, store, "combined", {DataMode.EXERCISE})["tracks"][0]
    assert exercise["data_mode"] == "EXERCISE"
    assert exercise["head_source"] == "EXERCISE-CELL"


def test_a_tracks_markings_are_read_from_its_samples_verbatim(tmp_path):
    # The same orbit from two providers an hour apart: the earlier mean
    # anomaly is wound back by one hour of mean motion, and both state SGP4,
    # so the agreement check can propagate and compare them.
    store = _ingest(tmp_path, [_row(epoch=NOW - timedelta(hours=1), ephemType=0,
                                    mean_anomaly=208.6412 - 360.0 * 1.00265344 / 24.0),
                               _row(source="NorthStar", marking="U//PR-NORTHSTAR-ELSET",
                                    ephemType=0)])
    t = _tracks_payload(_cfg(tmp_path), store, "real", {DataMode.REAL})["tracks"][0]
    # One provider drives (1.7.5), so the track carries that provider's
    # marking verbatim and nothing from the other.
    assert t["head_source"] == "NorthStar"
    assert t["classification_markings"] == ["U//PR-NORTHSTAR-ELSET"]
    assert t["classification_marking"] == "U//PR-NORTHSTAR-ELSET"
    assert t["providers"]["SAFRAN"]["status"] == "agrees"
    assert t["agreement"]["status"] == "agreed"


# -- 2. nothing is filled in --------------------------------------------
@pytest.mark.parametrize("drop", ["inclination", "eccentricity", "raan",
                                  "argOfPerigee", "meanAnomaly", "meanMotion"])
def test_a_row_missing_any_element_is_rejected_not_zero_filled(tmp_path, drop):
    """F9: a missing mean anomaly, perigee, RAAN or eccentricity used to become
    0.0 and the object was plotted somewhere it never was."""
    src, recs = _fetch(tmp_path, [_row(**{drop: None})])
    assert recs == {}
    assert src.last_stats.rejected == {"incomplete elements": 1}


@pytest.mark.parametrize("field,reason", [
    ("source", "no provider (source)"),
    ("classificationMarking", "no classification marking"),
    ("dataMode", "no recognised data mode"),
])
def test_a_row_without_provenance_is_rejected(tmp_path, field, reason):
    """F7 and F10 at the boundary: no marking used to become "U", no mode
    used to become REAL."""
    src, recs = _fetch(tmp_path, [_row(**{field: None})])
    assert recs == {}
    assert src.last_stats.rejected == {reason: 1}


def test_an_unknown_mode_and_a_blank_marking_are_rejected_too(tmp_path):
    src, _ = _fetch(tmp_path, [_row(dataMode="LIVE-ISH"), _row(classificationMarking="  ")])
    assert src.last_stats.rejected == {"no recognised data mode": 1,
                                       "no classification marking": 1}


def test_an_epoch_without_a_time_zone_is_rejected():
    assert parse_row({**_row(), "epoch": "2026-11-17T09:00:00"}) == \
        "no usable epoch with a time zone"


def test_an_absent_b_star_stays_absent(tmp_path):
    """B* plays no part in a position at the element set's own epoch, so the
    row is kept; the stored element set says None, and propagation away from
    the epoch is then refused rather than run on an invented 0.0."""
    _, recs = _fetch(tmp_path, [_row(bStar=None)])
    el = recs["64467"]["elset"]
    assert el["bstar"] is None


def test_rejections_reach_the_operator(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[_row(meanAnomaly=None), _row(source=None)])))
    # One request per cycle, so the counts are per response: this test is about
    # rejection, not slicing (the default slice is 1 h from 1.7.10).
    cfg = _cfg(tmp_path, udl_slice_hours=24)
    store = Store(str(tmp_path))
    ref = Refresher(cfg, store, [UDLElsetSource(cfg, client=client)], SingleFlight())
    ref.run_once(now=NOW)
    health = ref.health(now=NOW)
    assert health["last_ingest_rejected"] == 2
    assert health["last_ingest_rejected_reasons"] == {"incomplete elements": 1,
                                                      "no provider (source)": 1}
    assert health["ingest_diagnosis"].startswith("every record returned was rejected")


def test_a_manual_set_is_filed_under_the_operator_not_a_provider(tmp_path):
    """An operator typing "SAFRAN" must not make their numbers SAFRAN's."""
    from psirens.models import ManualElsetIn
    src = ManualElsetSource(str(tmp_path))
    src.add(ManualElsetIn(object_id="M9", name="t", epoch=NOW, inclination_deg=1.0,
                          eccentricity=0.0, raan_deg=0.0, argp_deg=0.0,
                          mean_anomaly_deg=0.0, mean_motion_rev_per_day=1.0027,
                          bstar=0.0, data_mode="SIMULATED",
                          classification_marking="U", source="SAFRAN"))
    rec = src.fetch(NOW - timedelta(hours=1), NOW)["M9"]
    assert rec["samples"][0]["source"] == "MANUAL"
    assert rec["elset"]["source"] == "MANUAL"
    assert rec["elset"]["stated_source"] == "SAFRAN"


# -- 3. synthetic data never reaches a live store -------------------------
def test_the_demo_belt_is_never_built_beside_live_udl(tmp_path):
    """F13: DEMO_MODE=true used to add the synthetic belt alongside UDL."""
    manual = ManualElsetSource(str(tmp_path))
    live = _build_sources(_cfg(tmp_path, demo_mode=True), None, manual)
    assert not any(isinstance(s, DemoElsetSource) for s in live)
    offline = _build_sources(_cfg(tmp_path, udl_enabled=False, udl_base_url=""), None, manual)
    assert any(isinstance(s, DemoElsetSource) for s in offline)


def test_no_demo_identifier_can_be_a_catalogue_number():
    """F13: four demo objects used REAL high-interest catalogue numbers."""
    recs = DemoElsetSource().fetch(NOW - timedelta(days=30), NOW)
    assert recs and all(oid.startswith(DEMO_ID_PREFIX) for oid in recs)
    assert all(not oid.isdigit() for oid in recs)
    # DEMO, or one of the 1.7.5 agreement demo's synthetic providers.
    assert all(s["source"] == "DEMO" or s["source"].startswith(DEMO_ID_PREFIX)
               for r in recs.values() for s in r["samples"])


def test_the_live_purge_removes_the_synthetic_providers_too(tmp_path):
    """The agreement demo's providers are DEMO-ALPHA and kin, not DEMO. Filed
    under a real number, the purge must still find every one of them."""
    from psirens.store import Store
    recs = DemoElsetSource().fetch(NOW - timedelta(days=30), NOW)
    real = {"64467": {**recs["DEMO-P1"], "origin": "UDL"}}
    store = Store(str(tmp_path))
    store.merge_samples(real, retention_days=90, max_samples=5000, now=NOW)
    assert store.load()["objects"]["64467"]["samples"]
    assert store.purge_synthetic() > 0
    assert "64467" not in store.load()["objects"]
    assert all(r["target"] is None or r["target"].startswith(DEMO_ID_PREFIX)
               for r in recs.values())


def test_a_live_start_purges_anything_synthetic(tmp_path):
    """A volume that once ran offline must not carry its demo belt into the
    live picture, whatever ids that belt used at the time."""
    store = Store(str(tmp_path))
    store.merge_samples(DemoElsetSource().fetch(NOW - timedelta(days=30), NOW),
                        retention_days=90, max_samples=2000, now=NOW)
    _, recs = _fetch(tmp_path, [_row()])
    store.merge_samples(recs, retention_days=90, max_samples=2000, now=NOW)
    data = store.load()
    data["objects"]["41748"] = {"name": "USA 270", "origin": "DEMO", "data_mode": "REAL",
                                "samples": [{"epoch": NOW.isoformat(), "sub_lon_deg": 105.0,
                                             "inclination_deg": 3.6}]}
    real = data["objects"]["64467"]
    real["provenance"].append({"source": "DEMO", "data_mode": "REAL",
                               "classification_marking": "U"})
    real["samples"].append({"epoch": (NOW - timedelta(hours=3)).isoformat(),
                            "sub_lon_deg": 140.0, "inclination_deg": 1.9,
                            "p": len(real["provenance"]) - 1})
    store._write_atomic(data)
    # From 1.7.13 the purge runs where the store is first used, in the worker.
    left = create_app(_cfg(tmp_path), sources=[]).state.store.load()["objects"]
    assert set(left) == {"64467"}
    assert [sample_provenance(left["64467"], s)["source"] for s in left["64467"]["samples"]] == ["SAFRAN"]


def test_an_offline_start_keeps_the_demo_belt(tmp_path):
    store = Store(str(tmp_path))
    store.merge_samples(DemoElsetSource().fetch(NOW - timedelta(days=30), NOW),
                        retention_days=90, max_samples=2000, now=NOW)
    before = len(store.load()["objects"])
    create_app(_cfg(tmp_path, udl_enabled=False, udl_base_url=""), sources=[])
    assert len(store.load()["objects"]) == before
