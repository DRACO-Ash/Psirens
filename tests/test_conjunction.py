"""Closest-approach and TLE export: validated against known GEO geometry and a
TLE round-trip, plus the neighbourhood filter and elset retention."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fixture_tle import export_lines

from psirens.conjunction import build_satrec, closest_approach, conjunctions_for
from psirens.sources import _elset_dict
from psirens.tle import ElsetProvenance
from psirens.store import Store

EP = datetime(2026, 8, 6, tzinfo=timezone.utc)
GEO_R = 42164.0


def _el(oid, mean_anom, inc=0.05):
    """A provider-shaped element set WITH its own native lines, as UDL serves
    it: the app builds no line, so a fixture without one would show none."""
    el = _bare_el(oid, mean_anom, inc)
    lines = export_lines(el)
    if lines is not None and oid.isdigit():
        el["line1"], el["line2"] = lines
    return el


def _bare_el(oid, mean_anom, inc=0.05):
    return _elset_dict(
        sat_no=oid, epoch=EP, inclination_deg=inc, eccentricity=0.0002,
        raan_deg=80.0, argp_deg=90.0, mean_anomaly_deg=mean_anom,
        mean_motion_rev_per_day=1.0027379, bstar=1e-5,
        classification="U//DS-JCO-NOTIF",
        provenance=ElsetProvenance(source="18 SDS", ephem_type=0))


def _obj(oid, lon, mean_anom):
    return {"name": f"OBJ-{oid}",
            "samples": [{"epoch": EP.isoformat(), "sub_lon_deg": lon,
                         "inclination_deg": 0.05}],
            "elset": _el(oid, mean_anom)}


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
    assert "no marked element set" in res["detail"]


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


# -- a malformed target must degrade to JSON, never to an unhandled 500 ------
def _target_with(el):
    """A store holding one target whose element set is `el`."""
    obj = {"name": "KOREASAT 5",
           "samples": [{"epoch": EP.isoformat(), "sub_lon_deg": 113.07,
                        "inclination_deg": 1.96}]}
    if el is not None:
        obj["elset"] = el
    return {"objects": {"29349": obj}}


def test_malformed_target_elset_returns_a_reason_not_an_exception():
    """Regression: neighbours were guarded but the target was not, so one bad
    record raised out of the route as a non-JSON 500 and the SPA could only
    say "service unavailable"."""
    broken = {
        "no elset": None,
        "empty": {},
        "unparseable epoch": {**_el("29349", 100.0), "epoch": "not-a-date"},
        "null mean motion": {**_el("29349", 100.0),
                             "mean_motion_rev_per_day": None},
        "no B*": {**_el("29349", 100.0), "bstar": None},
        "XP theory": {**_el("29349", 100.0), "ephem_type": 4},
    }
    for label, el in broken.items():
        payload = conjunctions_for(_target_with(el), "29349", now=EP)
        assert payload["neighbours"] == [], label
        assert payload["detail"], f"{label}: no reason given"
    # A marked element set that cannot be propagated still shows the
    # provider's own line; only the screen is refused, and it says why.
    xp = conjunctions_for(_target_with({**_el("29349", 100.0), "ephem_type": 4}), "29349", now=EP)
    assert xp["target"]["tle"] is not None
    assert xp["detail"].startswith("not screened: ephemeris type 4")


def test_a_healthy_target_is_unaffected_by_the_guard():
    payload = conjunctions_for(_target_with(_el("29349", 100.0)), "29349", now=EP)
    assert payload["target"] is not None
    assert payload.get("detail") is None


def test_neighbour_and_target_names_come_from_the_hrr_list():
    """Same defect as the coplanar list: without the HRR join a neighbour row
    shows its catalogue number as its name."""
    store = {"objects": {
        "41836": _obj("41836", 0.0, 100.0),
        "28924": _obj("28924", 1.0, 101.0),
    }}
    store["objects"]["41836"]["name"] = "41836"
    store["objects"]["28924"]["name"] = "28924"
    named = conjunctions_for(store, "41836", now=EP,
                             names={"41836": "SES-10", "28924": "EUTELSAT 174A"})
    assert named["target"]["name"] == "SES-10"
    assert named["neighbours"][0]["name"] == "EUTELSAT 174A"
    bare = conjunctions_for(store, "41836", now=EP)
    assert bare["target"]["name"] == "41836"
    assert bare["neighbours"][0]["name"] == "28924"
