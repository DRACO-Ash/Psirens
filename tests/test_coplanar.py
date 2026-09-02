"""Coplanar angle validated against the vector-normal method and known cases,
plus the screening filter/sort."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from psirens.coplanar import (OrbitPlane, coplanar_angle_deg, coplanar_for,
                              longitude_difference_deg)
from psirens.sources import _elset_dict

EP = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _normal(inc, raan):
    i, o = math.radians(inc), math.radians(raan)
    return (math.sin(i) * math.sin(o), -math.sin(i) * math.cos(o), math.cos(i))


def _vector_angle(i1, o1, i2, o2):
    a, b = _normal(i1, o1), _normal(i2, o2)
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))))


def test_matches_vector_method():
    for i1, o1, i2, o2 in [(5, 100, 5, 100), (5, 0, 5, 180), (0, 0, 5, 0),
                           (3, 0, 3, 90), (10, 0, 10, 90), (3.8, 12, 0.05, 300)]:
        assert abs(coplanar_angle_deg(i1, o1, i2, o2) - _vector_angle(i1, o1, i2, o2)) < 1e-9


def test_known_cases():
    assert coplanar_angle_deg(5, 100, 5, 100) == 0.0            # identical planes
    assert abs(coplanar_angle_deg(5, 0, 5, 180) - 10.0) < 1e-9  # opposite nodes -> 2i
    assert abs(coplanar_angle_deg(0, 0, 5, 0) - 5.0) < 1e-9     # equatorial vs i


def test_symmetry_and_bounds():
    a = coplanar_angle_deg(4, 30, 2, 210)
    b = coplanar_angle_deg(2, 210, 4, 30)
    assert abs(a - b) < 1e-12 and 0.0 <= a <= 180.0


def test_orbitplane_wrapper_agrees():
    p, q = OrbitPlane(3.0, 45.0), OrbitPlane(1.0, 200.0)
    assert abs(p.angle_to(q) - coplanar_angle_deg(3, 45, 1, 200)) < 1e-12


def test_longitude_difference_wraps():
    assert longitude_difference_deg(179, -179) == 2.0
    assert longitude_difference_deg(10, 350) == 20.0
    assert longitude_difference_deg(60, 60) == 0.0


def _obj(name, lon, inc, raan):
    el = _elset_dict(sat_no=name, epoch=EP, inclination_deg=inc, eccentricity=0.0002,
                     raan_deg=raan, argp_deg=90.0, mean_anomaly_deg=10.0,
                     mean_motion_rev_per_day=1.0027379, bstar=0.0, classification="U")
    return {"name": name, "samples": [{"epoch": EP.isoformat(),
            "sub_lon_deg": lon, "inclination_deg": inc}], "elset": el}


def test_coplanar_for_filters_sorts_and_shapes():
    store = {"objects": {
        "P": _obj("PRIMARY", 60.0, 3.0, 0.0),
        "A": _obj("NEAR-PLANE", 61.0, 3.0, 5.0),     # small coplanar, close lon
        "B": _obj("OFF-PLANE", 62.0, 3.0, 180.0),    # large coplanar (~6deg)
        "C": _obj("FAR-LON", 100.0, 3.0, 1.0),       # outside +/-20deg -> excluded
    }}
    res = coplanar_for(store, "P", half_width_deg=20.0, now=EP)
    ids = [t["id"] for t in res["tracks"]]
    assert ids == ["A", "B"]                          # C excluded, A before B (smaller angle)
    assert res["tracks"][0]["points"][0]["coplanar_deg"] < res["tracks"][1]["points"][0]["coplanar_deg"]
    assert res["primary"]["id"] == "P"
    assert res["tracks"][0]["points"][0]["lon_diff_deg"] == 1.0


def test_coplanar_for_primary_without_elset():
    store = {"objects": {"x": {"name": "x", "samples": [
        {"epoch": EP.isoformat(), "sub_lon_deg": 0.0, "inclination_deg": 0.0}]}}}
    res = coplanar_for(store, "x", now=EP)
    assert res["primary"] is None and "element set" in res["detail"]


def test_coplanar_for_unknown_primary():
    res = coplanar_for({"objects": {}}, "nope", now=EP)
    assert res["primary"] is None and res["tracks"] == []


# -- display names: never show the catalogue number twice -------------------
def _named_store():
    """Two objects whose stored name is their own catalogue number, which is
    what a UDL record looks like on a tenant that leaves origObjectId empty."""
    return {"objects": {
        "63157": {"name": "63157", "samples": [
            {"epoch": EP.isoformat(), "sub_lon_deg": 89.5, "inclination_deg": 0.4}],
            "elset": {"inclination_deg": 0.4, "raan_deg": 85.0}},
        "39017": {"name": "39017", "samples": [
            {"epoch": EP.isoformat(), "sub_lon_deg": 90.5, "inclination_deg": 1.2}],
            "elset": {"inclination_deg": 1.2, "raan_deg": 88.0}},
    }}


def test_hrr_names_replace_the_catalogue_number():
    names = {"63157": "TJS-15", "39017": "ZHONGXING-12"}
    out = coplanar_for(_named_store(), "63157", names=names)
    assert out["primary"]["name"] == "TJS-15"
    assert out["tracks"][0]["name"] == "ZHONGXING-12"


def test_without_hrr_the_id_shows_once_not_twice():
    """The defect: a row read "63157 63157". With no name available the id
    stands alone, so the interface never repeats it."""
    out = coplanar_for(_named_store(), "63157")
    assert out["primary"]["name"] == "63157"
    assert out["tracks"][0]["name"] == "39017"


def test_a_genuine_stored_name_survives_when_hrr_has_none():
    store = _named_store()
    store["objects"]["39017"]["name"] = "ZHONGXING-12"
    out = coplanar_for(store, "63157", names={})
    assert out["tracks"][0]["name"] == "ZHONGXING-12"
