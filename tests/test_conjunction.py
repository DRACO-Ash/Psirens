"""Closest-approach and TLE export: validated against known GEO geometry and a
TLE round-trip, plus the neighbourhood filter and elset retention."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sgp4.api import Satrec

from psirens.conjunction import (build_satrec, closest_approach,
                                 conjunctions_for, tle_lines, _jday)
from psirens.sources import _elset_dict
from psirens.store import Store

EP = datetime(2026, 8, 6, tzinfo=timezone.utc)
GEO_R = 42164.0


def _el(oid, mean_anom, inc=0.05):
    return _elset_dict(
        sat_no=oid, epoch=EP, inclination_deg=inc, eccentricity=0.0002,
        raan_deg=80.0, argp_deg=90.0, mean_anomaly_deg=mean_anom,
        mean_motion_rev_per_day=1.0027379, bstar=1e-5,
        classification="U//DS-JCO-NOTIF")


def _obj(oid, lon, mean_anom):
    return {"name": f"OBJ-{oid}",
            "samples": [{"epoch": EP.isoformat(), "sub_lon_deg": lon,
                         "inclination_deg": 0.05}],
            "elset": _el(oid, mean_anom)}


def test_tle_export_roundtrips_to_metre():
    lines = tle_lines(_el("100172", 100.0))
    assert lines is not None
    l1, l2 = lines
    assert l1.startswith("1 ") and l2.startswith("2 ")
    a = build_satrec(_el("100172", 100.0))
    b = Satrec.twoline2rv(l1, l2)
    jd, fr = _jday(EP + timedelta(hours=6))
    _, ra, _ = a.sgp4(jd, fr)
    _, rb, _ = b.sgp4(jd, fr)
    assert math.dist(ra, rb) < 1e-3   # < 1 metre round-trip


def test_tle_alpha5_for_six_digit_satno():
    l1, _ = tle_lines(_el("100172", 100.0))
    assert l1.split()[1].startswith("A")   # 100172 -> Alpha-5 "A0172"


def test_tle_lines_bad_elset_returns_none():
    assert tle_lines({"epoch": "not-a-date"}) is None


def test_closest_approach_one_degree_chord():
    a = build_satrec(_el("A", 100.0))
    b = build_satrec(_el("B", 101.0))     # ~1 deg east
    min_km, tca, now_km = closest_approach(a, b, EP, window_hours=24)
    expected = 2 * GEO_R * math.sin(math.radians(1.0) / 2)  # ~735.9 km
    assert abs(now_km - expected) < 5
    assert abs(min_km - expected) < 5
    assert tca is not None


def test_conjunctions_for_filters_and_sorts():
    store = {"objects": {
        "100172": _obj("100172", 60.0, 100.0),
        "58204": _obj("58204", 61.0, 101.0),    # within +/-10
        "39216": _obj("39216", 200.0, 300.0),   # far -> excluded
    }}
    res = conjunctions_for(store, "100172", window_hours=24, now=EP)
    assert [n["id"] for n in res["neighbours"]] == ["58204"]
    assert res["target"]["tle"] is not None
    n = res["neighbours"][0]
    assert n["min_km"] and n["range_now_km"] and n["tle"] and n["tca"]


def test_conjunctions_for_target_without_elset():
    store = {"objects": {"x": {"name": "x", "samples": [
        {"epoch": EP.isoformat(), "sub_lon_deg": 0.0, "inclination_deg": 0.0}]}}}
    res = conjunctions_for(store, "x", now=EP)
    assert res["target"] is None
    assert "no element set" in res["detail"]


def test_conjunctions_for_unknown_target():
    res = conjunctions_for({"objects": {}}, "nope", now=EP)
    assert res["target"] is None and res["neighbours"] == []


def test_store_retains_newest_elset(tmp_path):
    st = Store(str(tmp_path))
    e_old = _el("1", 100.0); e_old["epoch"] = "2026-08-01T00:00:00+00:00"
    e_new = _el("1", 100.0); e_new["epoch"] = "2026-08-05T00:00:00+00:00"
    base = {"name": "a", "samples": [{"epoch": EP.isoformat(),
            "sub_lon_deg": 0.0, "inclination_deg": 0.0}]}
    st.merge_samples({"1": {**base, "elset": e_new}}, retention_days=90,
                     max_samples=100, now=EP)
    st.merge_samples({"1": {**base, "elset": e_old}}, retention_days=90,
                     max_samples=100, now=EP)   # older must not overwrite
    kept = st.load()["objects"]["1"]["elset"]
    assert kept["epoch"] == "2026-08-05T00:00:00+00:00"
