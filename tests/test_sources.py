from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from psirens.config import load_config
from psirens.models import DataMode, ManualElsetIn
from psirens.sources import DemoElsetSource, ManualElsetSource, UDLElsetSource


# -- models / boundary validation --------------------------------------
def test_manual_elset_accepts_all_four_modes():
    for m in ("REAL", "SIMULATED", "TEST", "EXERCISE"):
        e = ManualElsetIn(object_id="X", name="x",
                          epoch=datetime(2026, 7, 1, tzinfo=timezone.utc),
                          inclination_deg=3.0, eccentricity=0.0, raan_deg=0.0,
                          argp_deg=0.0, mean_anomaly_deg=0.0,
                          mean_motion_rev_per_day=1.0027, data_mode=m)
        assert e.data_mode == DataMode(m)


def test_manual_elset_rejects_bad_inclination():
    with pytest.raises(ValidationError):
        ManualElsetIn(object_id="X", name="x",
                      epoch=datetime(2026, 7, 1, tzinfo=timezone.utc),
                      inclination_deg=999.0, eccentricity=0.0, raan_deg=0.0,
                      argp_deg=0.0, mean_anomaly_deg=0.0, mean_motion_rev_per_day=1.0)


def test_manual_elset_rejects_bad_eccentricity():
    with pytest.raises(ValidationError):
        ManualElsetIn(object_id="X", name="x",
                      epoch=datetime(2026, 7, 1, tzinfo=timezone.utc),
                      inclination_deg=3.0, eccentricity=1.5, raan_deg=0.0,
                      argp_deg=0.0, mean_anomaly_deg=0.0, mean_motion_rev_per_day=1.0)


# -- demo source --------------------------------------------------------
def test_demo_source_is_deterministic_and_covers_all_modes():
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    a = DemoElsetSource().fetch(now - timedelta(days=90), now)
    b = DemoElsetSource().fetch(now - timedelta(days=90), now)
    assert a == b
    modes = {rec["data_mode"] for rec in a.values()}
    assert {"REAL", "SIMULATED", "TEST", "EXERCISE"} <= modes


def test_demo_source_has_target_link():
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    recs = DemoElsetSource().fetch(now - timedelta(days=20), now)
    assert recs["77777"]["target"] == "43683"


# -- manual source (temporary, TTL) ------------------------------------
def test_manual_add_list_and_expiry(tmp_path):
    src = ManualElsetSource(str(tmp_path))
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    e = ManualElsetIn(object_id="M1", name="temp", epoch=now,
                      inclination_deg=2.0, eccentricity=0.0, raan_deg=10.0,
                      argp_deg=0.0, mean_anomaly_deg=0.0,
                      mean_motion_rev_per_day=1.0027, ttl_hours=1.0)
    src.add(e, now=now)
    assert len(src.list_active(now=now)) == 1
    # after TTL the entry is gone (and pruned)
    assert src.list_active(now=now + timedelta(hours=2)) == []


def test_manual_source_fetch_computes_sample(tmp_path):
    src = ManualElsetSource(str(tmp_path))
    # Anchor to real now: fetch() prunes by the real clock, so a fixed past
    # epoch with a short TTL would expire and make this test time-dependent.
    now = datetime.now(timezone.utc)
    src.add(ManualElsetIn(object_id="M2", name="t", epoch=now,
            inclination_deg=1.0, eccentricity=0.0, raan_deg=0.0, argp_deg=0.0,
            mean_anomaly_deg=0.0, mean_motion_rev_per_day=1.0027,
            target="43683"))
    recs = src.fetch(now - timedelta(days=1), now)
    assert "M2" in recs and recs["M2"]["target"] == "43683"
    assert -180 < recs["M2"]["samples"][0]["sub_lon_deg"] <= 180


def test_manual_remove(tmp_path):
    src = ManualElsetSource(str(tmp_path))
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    src.add(ManualElsetIn(object_id="M3", name="t", epoch=now,
            inclination_deg=1.0, eccentricity=0.0, raan_deg=0.0, argp_deg=0.0,
            mean_anomaly_deg=0.0, mean_motion_rev_per_day=1.0027), now=now)
    assert src.remove("M3") is True
    assert src.remove("nope") is False


# -- UDL source (offline; fake transport) ------------------------------
def _cfg_udl():
    import os
    os.environ["UDL_BASE_URL"] = "https://udl.example.test"
    os.environ["UDL_USER"] = "u"
    os.environ["UDL_PASSWORD"] = "p%word"  # a percent must survive
    try:
        return load_config()
    finally:
        for k in ("UDL_BASE_URL", "UDL_USER", "UDL_PASSWORD"):
            os.environ.pop(k, None)


def test_udl_normalises_rows_and_reads_target_field():
    cfg = _cfg_udl()
    rows = [{
        "satNo": "43683", "epoch": "2026-07-01T00:00:00.000000Z",
        "inclination": 1.9, "eccentricity": 0.0002, "raan": 100.0,
        "argOfPerigee": 20.0, "meanAnomaly": 30.0, "meanMotion": 1.0027,
        "dataMode": "REAL", "classificationMarking": "U", "source": "18SDS",
        "tags": ["TGT-99001"],
    }]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=rows))
    client = httpx.Client(transport=transport)
    src = UDLElsetSource(cfg, client=client)
    out = src.fetch(datetime(2026, 6, 1, tzinfo=timezone.utc),
                    datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert "43683" in out
    assert out["43683"]["target"] == "TGT-99001"
    assert out["43683"]["data_mode"] == "REAL"


def test_udl_fetch_failure_is_never_fatal():
    cfg = _cfg_udl()
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    client = httpx.Client(transport=transport)
    out = UDLElsetSource(cfg, client=client).fetch(
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 20, tzinfo=timezone.utc))
    assert out == {}  # degrades to empty, store keeps last-good


# -- native TLE capture at ingest ---------------------------------------
def _native_pair(oid="43683"):
    """A genuine checksum-correct line pair for `oid`, from the exporter."""
    from psirens.conjunction import tle_lines
    from psirens.sources import _elset_dict
    lines = tle_lines(_elset_dict(
        sat_no=oid, epoch=datetime(2026, 7, 1, tzinfo=timezone.utc),
        inclination_deg=1.9, eccentricity=0.0002, raan_deg=100.0,
        argp_deg=20.0, mean_anomaly_deg=30.0, mean_motion_rev_per_day=1.0027,
        bstar=0.0, classification="U"))
    assert lines is not None
    return lines


def _udl_row(oid="43683", **over):
    row = {
        "satNo": oid, "epoch": "2026-07-01T00:00:00.000000Z",
        "inclination": 1.9, "eccentricity": 0.0002, "raan": 100.0,
        "argOfPerigee": 20.0, "meanAnomaly": 30.0, "meanMotion": 1.0027,
        "dataMode": "REAL", "classificationMarking": "U", "source": "18SDS",
    }
    row.update(over)
    return row


def _pull(rows):
    cfg = _cfg_udl()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=rows))
    client = httpx.Client(transport=transport)
    return UDLElsetSource(cfg, client=client).fetch(
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 7, 20, tzinfo=timezone.utc))


def test_udl_captures_native_lines_and_the_extra_fields():
    l1, l2 = _native_pair()
    out = _pull([_udl_row(line1=l1, line2=l2, revNo=4211,
                          meanMotionDot=1.2e-6, meanMotionDDot=0.0,
                          ephemType=0)])
    el = out["43683"]["elset"]
    assert el["line1"] == l1 and el["line2"] == l2
    assert el["rev_no"] == 4211
    assert el["mean_motion_dot"] == 1.2e-6
    assert el["ephem_type"] == 0
    assert el["source"] == "18SDS"


def test_udl_drops_a_native_line_belonging_to_another_object():
    """A mispushed line is rejected at ingest, so nothing downstream has to
    decide whether a stored line can be trusted."""
    l1, l2 = _native_pair("28924")
    out = _pull([_udl_row("43683", line1=l1, line2=l2)])
    el = out["43683"]["elset"]
    assert el["line1"] == "" and el["line2"] == ""


def test_udl_tolerates_absent_native_lines_and_extras():
    out = _pull([_udl_row()])
    el = out["43683"]["elset"]
    assert el["line1"] == ""
    assert el["rev_no"] is None          # absent stays absent
    assert el["mean_motion_ddot"] is None


def test_udl_rejects_unparseable_extras_without_losing_the_record():
    out = _pull([_udl_row(revNo="not-a-number", ephemType="")])
    assert out["43683"]["elset"]["rev_no"] is None
    assert out["43683"]["elset"]["ephem_type"] is None


def test_udl_retains_the_newest_fix_per_provider():
    rows = [
        _udl_row(source="18SDS", epoch="2026-07-02T00:00:00.000000Z"),
        _udl_row(source="LeoLabs", epoch="2026-07-01T00:00:00.000000Z"),
        _udl_row(source="LeoLabs", epoch="2026-07-01T12:00:00.000000Z"),
    ]
    cands = _pull(rows)["43683"]["elset_candidates"]
    assert set(cands) == {"18SDS", "LeoLabs"}
    assert cands["LeoLabs"]["epoch"].startswith("2026-07-01T12:00")
    # `elset` stays the newest fix overall, so the plot head is unchanged.
    assert cands["18SDS"]["epoch"].startswith("2026-07-02")
