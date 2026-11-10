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


# -- history trails (1.6.17) -----------------------------------------------
#
# The payload has carried a `points` list since 1.6.0, documented as "length 1
# until per-sample RAAN". Samples now retain RAAN, so these assert the trail is
# real: built from paired measurements, not from holding one value constant.
from datetime import datetime, timedelta, timezone  # noqa: E402

from psirens.coplanar import coplanar_for  # noqa: E402

_T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _trail_obj(lon, inc, raan, *, samples, name="OBJ"):
    """A store-shaped object. `samples` is a list of (hours_ago, raan or None)."""
    rows = []
    for hours, s_raan in samples:
        row = {"epoch": (_T0 - timedelta(hours=hours)).isoformat(),
               "sub_lon_deg": lon, "inclination_deg": inc}
        if s_raan is not None:
            row["raan_deg"] = s_raan
        rows.append(row)
    rows.sort(key=lambda r: r["epoch"])
    return {"name": name, "samples": rows,
            "elset": {"inclination_deg": inc, "raan_deg": raan}}


def _store(primary_samples, neighbour_samples):
    return {"objects": {
        "P": _trail_obj(0.0, 1.0, 10.0, samples=primary_samples, name="PRIMARY"),
        "N": _trail_obj(3.0, 1.0, 12.0, samples=neighbour_samples, name="NEIGHBOUR"),
    }}


def test_a_track_is_one_point_when_samples_carry_no_raan():
    """The pre-1.6.17 shape. Samples without RAAN cannot define a plane, so
    the chart falls back to the single elset-derived point."""
    data = _store([(6, None), (0, None)], [(6, None), (0, None)])
    track = coplanar_for(data, "P")["tracks"][0]
    assert len(track["points"]) == 1


def test_a_trail_appears_once_samples_carry_raan():
    data = _store([(12, 10.0), (6, 10.0), (0, 10.0)],
                  [(12, 14.0), (6, 13.0), (0, 12.0)])
    track = coplanar_for(data, "P")["tracks"][0]
    assert len(track["points"]) > 1, "paired samples should produce a trail"
    assert all(p["coplanar_deg"] >= 0 for p in track["points"])


def test_the_trail_runs_oldest_to_newest():
    """The SPA draws the polyline in array order and puts the marker on the
    last point, so the order is load-bearing rather than cosmetic."""
    data = _store([(12, 10.0), (6, 10.0), (0, 10.0)],
                  [(12, 14.0), (6, 13.0), (0, 12.0)])
    epochs = [p["epoch"] for p in coplanar_for(data, "P")["tracks"][0]["points"]]
    assert epochs == sorted(epochs)


def test_the_head_point_still_comes_from_the_elset():
    """The invariant from 1.6.0: the head uses the newest elset overall, the
    same source the plot head and the RA needle use, so changing the TLE
    selection policy cannot move the plot and this chart apart. A trail must
    not quietly replace it with a sample-derived head."""
    data = _store([(12, 10.0), (0, 10.0)], [(12, 99.0), (0, 99.0)])
    track = coplanar_for(data, "P")["tracks"][0]
    from psirens.coplanar import coplanar_angle_deg
    expected = round(coplanar_angle_deg(1.0, 10.0, 1.0, 12.0), 4)   # elset RAANs
    assert track["points"][-1]["coplanar_deg"] == expected


def test_a_sample_with_no_partner_in_time_makes_no_point():
    """Two objects are sampled by different providers at different epochs. A
    point pairing readings hours apart is not a measurement, so it is dropped
    rather than drawn."""
    far = _store([(0, 10.0)], [(48, 14.0), (0, 12.0)])
    track = coplanar_for(far, "P", pair_hours=6)["tracks"][0]
    assert len(track["points"]) == 1, "the 48h-old neighbour sample has no partner"


def test_the_trail_is_capped():
    many = [(h, 12.0 + h * 0.01) for h in range(40, 0, -1)]
    data = _store([(h, 10.0) for h in range(40, 0, -1)], many)
    track = coplanar_for(data, "P", max_points=5)["tracks"][0]
    assert len(track["points"]) <= 6   # cap, plus the elset head
