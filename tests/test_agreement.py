"""1.7.5: one provider drives each object, and only when the providers agree.

Owner approval, 17 November 2026 ("Build it"): tracks and fits per provider;
a check that providers agree, with an outlier set aside; no drift rate when the
fit is poor; no projected approaches; old element sets aged out of selection.

Every fixture here goes through the real UDL ingest path (`UDLElsetSource` on a
mock transport, then `Store.merge_samples`), so the provenance tags, the
candidate keys and the stored shapes are the ones production writes.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from psirens.agreement import (AGREED, DISPUTED, SINGLE, UNCHECKED, Policy, Resolver,
                               elset_rate, judge, lon_at, resolve, resolve_data,
                               separation_deg)
from psirens.astro import sub_longitude_deg
from psirens.config import Config
from psirens.conjunction import conjunctions_for
from psirens.events import Thresholds, detect
from psirens.main import _drift_block, _tracks_payload, create_app
from psirens.models import DataMode
from psirens.sources import UDLElsetSource
from psirens.store import Store
from psirens.tle import ELSET_MAX_LAG_HOURS, candidates_of, select_elset

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
GEO_N = 1.00273791      # rev/day: zero longitude drift
SAT = "64467"


def _cfg(tmp_path, **over) -> Config:
    base = dict(port=8080, allowed_origin="", team_token="", retention_days=90,
                refresh_seconds=3600, lon_min=-180.0, lon_max=180.0, inc_min=0.0,
                inc_max=15.0, max_samples_per_object=2000, demo_mode=False,
                udl_enabled=True, udl_base_url="http://udl.invalid",
                udl_elset_path="/udl/elset", udl_user="u", udl_password="p",
                udl_target_field="tags", udl_epoch_param="epoch",
                udl_accept="application/json", data_dir=str(tmp_path),
                scheduler_enabled=False, udl_slice_pause_seconds=0.0)
    base.update(over)
    return Config(**base)


def _anomaly_for(lon: float, when: datetime, n: float) -> float:
    """The mean anomaly that puts a near-circular GEO orbit at `lon` at `when`.
    Sub-longitude is linear in mean anomaly with unit slope here, so two
    corrections land it to well under a millidegree."""
    m = 0.0
    for _ in range(3):
        got = sub_longitude_deg(inclination_deg=0.05, eccentricity=0.0002, raan_deg=80.0,
                                argp_deg=90.0, mean_anomaly_deg=m,
                                mean_motion_rev_per_day=n, bstar=1e-5, epoch=when)
        m = (m + separation_signed(lon, got)) % 360.0
    return m


def separation_signed(a: float, b: float | None) -> float:
    return ((a - (b or 0.0)) + 180.0) % 360.0 - 180.0


def rows(provider: str, lon0: float, *, rate: float = 0.0, hours: float = 96.0,
         end: datetime = NOW, step_h: float = 2.0, marking: str = "U//PR-TEST",
         ephem: int | None = 0, mode: str = "REAL", sat: str = SAT,
         scatter: float = 0.0) -> list[dict]:
    """UDL-shaped rows for one provider's view of one object."""
    n = GEO_N + rate / 360.0   # faster than synchronous drifts EAST
    out, t, k = [], end - timedelta(hours=hours), 0
    while t <= end:
        days = (t - (end - timedelta(hours=hours))).total_seconds() / 86400.0
        wobble = scatter * (1 if k % 2 else -1)
        row = {"satNo": sat, "epoch": t.isoformat().replace("+00:00", "Z"),
               "inclination": 0.05, "raan": 80.0, "eccentricity": 0.0002,
               "argOfPerigee": 90.0,
               "meanAnomaly": _anomaly_for(lon0 + rate * days + wobble, t, n),
               "meanMotion": n, "bStar": 1e-5, "source": provider, "dataMode": mode,
               "classificationMarking": marking}
        if ephem is not None:
            row["ephemType"] = ephem
        out.append(row)
        t += timedelta(hours=step_h)
        k += 1
    return out


def ingest(tmp_path, batch: list[dict]) -> Store:
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=batch)))
    src = UDLElsetSource(_cfg(tmp_path, udl_slice_hours=24 * 30), client=client)
    recs = src.fetch(NOW - timedelta(days=20), NOW + timedelta(hours=1))
    store = Store(str(tmp_path))
    store.merge_samples(recs, retention_days=90, max_samples=5000, now=NOW)
    return store


def track(tmp_path, store, view="real", modes=None):
    payload = _tracks_payload(_cfg(tmp_path), store, view, modes or {DataMode.REAL})
    return payload["tracks"][0] if payload["tracks"] else None


# -- the judgement itself ----------------------------------------------------
def test_two_that_agree_and_two_that_do_not():
    assert judge({"A": 10.0, "B": 10.5}, 1.0) == (AGREED, {"A", "B"})
    assert judge({"A": 10.0, "B": 12.0}, 1.0) == (DISPUTED, set())


def test_one_outlier_among_three_or_more_is_set_aside():
    status, agreed = judge({"A": 127.9, "B": 127.95, "C": 64.2}, 1.0)
    assert status == AGREED and agreed == {"A", "B"}


def test_two_equal_camps_is_a_dispute_not_a_majority():
    assert judge({"A": 10.0, "B": 10.1, "C": 40.0, "D": 40.1}, 1.0)[0] == DISPUTED


def test_agreement_is_judged_across_the_seam():
    assert separation_deg(179.8, -179.9) == pytest.approx(0.3)
    assert judge({"A": 179.8, "B": -179.9}, 1.0)[0] == AGREED


def test_nothing_to_compare():
    assert judge({}, 1.0) == (UNCHECKED, set())
    assert judge({"A": 1.0}, 1.0) == (SINGLE, {"A"})


# -- COSMOS 2589, the case that started this ---------------------------------
def _cosmos(tmp_path) -> Store:
    """Five providers agree at 127.9E, flat; NorthStar's element sets put it at
    64E, sliding west, from 36 h ago. The live shape of 17 November 2026."""
    # NorthStar's newest set is the newest of all, as it was live: commercial
    # first, newest within class, so the raw store would pick it. Without
    # that the providers tie, and a bypass of the agreement check picks the
    # same provider by dictionary order and proves nothing.
    batch = []
    for name, lon in (("SAFRAN", 127.90), ("18 SPCS", 127.88), ("KBR", 127.91),
                      ("EXO", 127.89)):
        batch += rows(name, lon, rate=-0.024, end=NOW - timedelta(hours=1),
                      marking=f"U//PR-{name.replace(' ', '')}")
    batch += rows("NorthStar", 67.7, rate=-2.28, hours=36,
                  marking="U//PR-NORTHSTAR-ELSET")
    return ingest(tmp_path, batch)


def test_cosmos_2589_northstar_is_set_aside_and_the_drift_is_real(tmp_path):
    t = track(tmp_path, _cosmos(tmp_path))
    assert t["agreement"]["status"] == AGREED
    assert set(t["agreement"]["set_aside"]) == {"NorthStar"}
    assert t["providers"]["NorthStar"]["status"] == "set aside"
    assert "deg from the 4 provider(s) that agree" in t["providers"]["NorthStar"]["note"]
    # Commercial first, newest within class, among the providers that agree.
    assert t["head_source"] in {"SAFRAN", "KBR", "EXO"}
    assert t["providers"][t["head_source"]]["status"] == "track"
    # The track is one provider's: nothing near 64E is plotted.
    assert all(abs(s["sub_lon_deg"] - 127.9) < 0.5 for s in t["samples"])
    # The pooled 1.7.4 track fitted -12.96 deg/day at 19 deg rms.
    assert_the_agreeing_rate(t)
    assert t["drift_residual_deg"] < 0.05
    assert t["drift_withheld_reason"] is None


def test_the_tle_coplanar_and_screen_follow_the_track_provider(tmp_path):
    store = _cosmos(tmp_path)
    resolved = resolve_data(store.load(), Policy())
    obj = resolved["objects"][SAT]
    primary = obj["agreement"]["REAL"]["primary"]
    assert obj["elset"]["source"] == primary
    assert next(iter(obj["elset_candidates"].values()))["source"] == primary
    out = conjunctions_for(resolved, SAT)
    assert out["target"]["tle_provenance"]["source"] == primary


def test_cosmos_raises_no_manoeuvre_event(tmp_path):
    store = _cosmos(tmp_path)
    found = detect(resolve_data(store.load(), Policy()), Thresholds(),
                   now=NOW, stale_after_hours=24)
    assert not [e for e in found["events"] if e["type"] in ("STARTED", "STOPPED", "EW")]


# -- disputes -----------------------------------------------------------------
def test_two_providers_that_disagree_publish_no_drift_and_no_event(tmp_path):
    store = ingest(tmp_path, rows("SAFRAN", 127.9) + rows("NorthStar", 64.2, rate=-2.28))
    t = track(tmp_path, store)
    assert t["agreement"]["status"] == DISPUTED
    assert t["drift_deg_per_day"] is None
    assert "disagree" in t["drift_withheld_reason"]
    assert {r["status"] for r in t["providers"].values()} == {"track", "disputed"}
    found = detect(resolve_data(store.load(), Policy()), Thresholds(),
                   now=NOW, stale_after_hours=24)
    assert [e for e in found["events"] if e["type"] != "QUIET"] == []


def test_an_unverifiable_provider_is_listed_not_judged_and_not_preferred(tmp_path):
    """KBR states no ephemeris type, so its set cannot be propagated as issued.
    It is commercial and newest, and would have won 1.7.4's selection."""
    store = ingest(tmp_path, rows("18 SPCS", 127.9, end=NOW - timedelta(hours=1))
                   + rows("KBR", 127.9, ephem=None))
    t = track(tmp_path, store)
    assert t["agreement"]["status"] == SINGLE
    assert t["head_source"] == "18 SPCS"
    assert t["providers"]["KBR"]["status"] == "unverified"
    assert "ephemeris type not stated" in t["providers"]["KBR"]["note"]


# -- age-out --------------------------------------------------------------------
def test_an_element_set_far_older_than_the_newest_is_aged_out(tmp_path):
    """SAFRAN stopped three days ago. Commercial first used to keep it as the
    TLE for the whole retention window (audit F12)."""
    store = ingest(tmp_path, rows("SAFRAN", 127.9, end=NOW - timedelta(hours=72))
                   + rows("18 SPCS", 127.9))
    obj = store.load()["objects"][SAT]
    assert {el["source"] for el in candidates_of(obj)} == {"18 SPCS"}
    assert select_elset(obj)["source"] == "18 SPCS"
    t = track(tmp_path, store)
    assert t["head_source"] == "18 SPCS"
    assert t["providers"]["SAFRAN"]["status"] == "aged out"


def test_age_out_is_relative_to_the_newest_held_not_the_clock(tmp_path):
    """A feed outage must not blank every TLE: a store whose newest fix is a
    fortnight old still selects its newest provider."""
    old = NOW - timedelta(days=14)
    store = ingest(tmp_path, rows("SAFRAN", 127.9, end=old)
                   + rows("18 SPCS", 127.9, end=old - timedelta(hours=1)))
    obj = store.load()["objects"][SAT]
    assert select_elset(obj)["source"] == "SAFRAN"
    assert len(candidates_of(obj)) == 2
    assert ELSET_MAX_LAG_HOURS == 48.0


# -- the residual ceiling -------------------------------------------------------
def test_a_fit_that_misses_its_data_publishes_no_rate():
    t0 = NOW - timedelta(hours=72)
    pairs = [(t0 + timedelta(hours=h), 127.9 if h % 2 else 64.2) for h in range(72)]
    block = _drift_block(pairs)
    assert block["drift_deg_per_day"] is None
    assert block["drift_residual_deg"] > 1.0
    assert "ceiling" in block["drift_withheld_reason"]


def test_a_good_fit_is_published_with_no_reason():
    t0 = NOW - timedelta(hours=72)
    pairs = [(t0 + timedelta(hours=h), 10.0 - 0.3 * h / 24.0) for h in range(72)]
    block = _drift_block(pairs)
    assert block["drift_deg_per_day"] == pytest.approx(-0.3, abs=1e-6)
    assert block["drift_withheld_reason"] is None


def test_a_poor_fit_cannot_confirm_a_burn():
    """A real-looking burn buried in 1.5 deg of scatter. Without the ceiling
    the detector CONFIRMS it, from fits that miss their data by more than the
    burn moves the object in a day; with the ceiling it confirms nothing.
    The loose half is what makes the strict half mean something."""
    t0 = NOW - timedelta(hours=120)
    burn = t0 + timedelta(hours=72)
    samples = []
    for k in range(241):
        when = t0 + timedelta(minutes=30 * k)
        days_after = max(0.0, (when - burn).total_seconds() / 86400.0)
        lon = 127.9 - 4.0 * days_after + (1.5 if k % 2 else -1.5)
        samples.append({"epoch": when.isoformat(), "sub_lon_deg": lon,
                        "inclination_deg": 0.33})
    data = {"objects": {SAT: {"data_mode": "REAL", "samples": samples}}}
    kinds = ("STARTED", "STOPPED", "EW")
    loose = detect(data, Thresholds(max_residual=1e9), now=NOW, stale_after_hours=24)
    assert [e for e in loose["events"] if e["type"] in kinds]
    strict = detect(data, Thresholds(), now=NOW, stale_after_hours=24)
    assert not [e for e in strict["events"] if e["type"] in kinds]


# -- untagged history -------------------------------------------------------------
def _untagged(tmp_path, *, with_tagged: bool) -> Store:
    store = ingest(tmp_path, rows("SAFRAN", 127.9, hours=6) if with_tagged else [])
    data = store.load() if with_tagged else {"schema": 1, "objects": {}}
    obj = data["objects"].setdefault(SAT, {"samples": [], "data_mode": "REAL",
                                           "classification_marking": "U"})
    t0 = NOW - timedelta(days=5)
    obj["samples"] = [{"epoch": (t0 + timedelta(hours=h)).isoformat(),
                       "sub_lon_deg": 64.0 + 0.01 * h, "inclination_deg": 0.33}
                      for h in range(90)] + obj["samples"]
    with open(store.path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return store


def test_untagged_history_is_never_mixed_with_tagged_samples(tmp_path):
    t = track(tmp_path, _untagged(tmp_path, with_tagged=True))
    assert t["head_source"] == "SAFRAN"
    assert all(abs(s["sub_lon_deg"] - 127.9) < 0.5 for s in t["samples"])
    assert t["providers"]["untagged"]["status"] == "excluded"
    assert t["providers"]["untagged"]["count"] == 90


def test_an_object_with_only_untagged_history_is_shown_without_a_rate(tmp_path):
    store = _untagged(tmp_path, with_tagged=False)
    t = track(tmp_path, store)
    assert t["agreement"]["samples_untagged"] is True
    assert len(t["samples"]) == 90
    assert t["drift_deg_per_day"] is None and "provider is unknown" in t["drift_withheld_reason"]
    found = detect(resolve_data(store.load(), Policy()), Thresholds(),
                   now=NOW, stale_after_hours=24)
    assert [e for e in found["events"] if e["type"] != "QUIET"] == []


# -- silence is judged on every provider -------------------------------------------
def test_quiet_is_judged_on_any_provider_not_only_the_track_provider(tmp_path):
    """SAFRAN, the track provider, stopped 20 h ago; 18 SPCS still reports.
    The object is not silent, so no QUIET event."""
    # A second object keeps the rest of the belt current, so the belt's
    # reference time is NOW and a 20 h silence is measurable at all.
    store = ingest(tmp_path, rows("SAFRAN", 127.9, end=NOW - timedelta(hours=20))
                   + rows("18 SPCS", 127.9) + rows("SAFRAN", 40.0, sat="11111"))
    resolved = resolve_data(store.load(), Policy())
    assert resolved["objects"][SAT]["agreement"]["REAL"]["primary"] == "SAFRAN"
    found = detect(resolved, Thresholds(), now=NOW, stale_after_hours=24)
    assert found["belt_current"] is True
    assert not [e for e in found["events"] if e["type"] == "QUIET" and e["object_id"] == SAT]


# -- the resolved view is cached and served -------------------------------------------
def test_the_resolver_computes_once_per_store_change(tmp_path):
    store = _cosmos(tmp_path)
    resolver = Resolver(store, Policy())
    resolver.load()
    resolver.load()
    assert resolver.computations == 1
    assert store.remove_object(SAT)   # any change to the held store (1.7.13)
    resolver.load()
    assert resolver.computations == 2


def test_the_api_serves_the_agreement_and_never_a_projection(tmp_path):
    _cosmos(tmp_path)
    app = create_app(_cfg(tmp_path), sources=[])
    with TestClient(app) as c:
        body = c.get("/api/tracks?view=real").json()
        t = next(x for x in body["tracks"] if x["object_id"] == SAT)
        assert t["agreement"]["set_aside"].keys() == {"NorthStar"}
        assert "providers" not in t["agreement"]
        events = c.get("/api/events?view=real").json()["events"]
        assert all("projection" not in e for e in events)
        conj = c.get(f"/api/conjunctions?target={SAT}").json()
        assert t["head_source"] != "NorthStar"
        assert conj["target"]["tle_provenance"]["source"] == t["head_source"]


def test_lon_at_reads_the_provider_own_elements():
    el = {"epoch": NOW.isoformat(), "inclination_deg": 0.05, "eccentricity": 0.0002,
          "raan_deg": 80.0, "argp_deg": 90.0,
          "mean_anomaly_deg": _anomaly_for(127.9, NOW, GEO_N),
          "mean_motion_rev_per_day": GEO_N, "bstar": 1e-5, "ephem_type": 0}
    assert lon_at(el, NOW) == pytest.approx(127.9, abs=1e-3)
    assert lon_at({**el, "ephem_type": 4}, NOW) is None
    assert resolve({"samples": []}, Policy())["samples"] == []


# -- 1.7.8: a consensus drift rate, from the providers' own element sets ---------
def _line_el(epoch_day: float, inc, raan, ecc, argp, ma, n, bstar) -> dict:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=epoch_day - 1.0)
    return {"epoch": when.isoformat(), "inclination_deg": inc, "raan_deg": raan,
            "eccentricity": ecc, "argp_deg": argp, "mean_anomaly_deg": ma,
            "mean_motion_rev_per_day": n, "bstar": bstar, "ephem_type": 0}


def test_the_element_set_rate_reproduces_the_cosmos_2589_lines():
    """Ash's two lines of 17 November 2026, verbatim elements. The vendor
    comparison put COSMOS 2589 at -0.022 to -0.026 deg/day."""
    udl = _line_el(321.41066406, 0.3274, 86.4242, 0.0007366, 37.1819, 208.6412,
                   1.00265344, -6.3064e-5)
    northstar = _line_el(321.35849668, 0.3270, 86.3698, 0.0006840, 38.8569, 124.4911,
                         0.99642971, 0.0)
    assert elset_rate(udl) == pytest.approx(-0.0257, abs=0.002)
    assert elset_rate(northstar) == pytest.approx(-2.27, abs=0.02)


def test_one_full_orbit_cancels_the_eccentricity_libration():
    """An e = 0.0022 orbit swings 0.25 deg east-west once per orbit; sampled
    over less than a whole orbit that swing would read as drift."""
    el = _line_el(321.0, 0.05, 80.0, 0.0002, 90.0, 10.0, GEO_N, 1e-5)
    eccentric = {**el, "eccentricity": 0.0022}
    assert elset_rate(eccentric) == pytest.approx(elset_rate(el), abs=1e-3)
    faster = {**el, "mean_motion_rev_per_day": GEO_N + 0.5 / 360.0}
    assert elset_rate(faster) - elset_rate(el) == pytest.approx(0.5, abs=0.01)  # east


def test_a_rate_is_published_with_hours_of_history_not_days(tmp_path):
    """The owner's report of 17 November 2026: every object read "no usable
    baseline" because each provider held under 24 h of tagged fixes. The
    agreeing providers' element sets carry a rate from the first fix."""
    store = ingest(tmp_path, rows("SAFRAN", 92.2, rate=-0.3, hours=6)
                   + rows("KBR", 92.21, rate=-0.3, hours=6)
                   + rows("EXO", 92.19, rate=-0.3, hours=6))
    t = track(tmp_path, store)
    assert t["drift_observed_deg_per_day"] is None
    assert "no usable baseline" in t["drift_observed_withheld_reason"]
    assert t["drift_basis"] == "element_sets"
    assert t["drift_withheld_reason"] is None
    assert t["drift_deg_per_day"] == pytest.approx(-0.3, abs=0.02)
    assert t["drift_consensus"]["count"] == 3


def assert_the_agreeing_rate(t: dict) -> None:
    """The published rate is the agreeing providers' own, not NorthStar's
    -2.28 and not the pooled -12.96.

    ROTTED ASSERTION, found 24 November 2026: this used to compare against
    the -0.024 deg/day the rows were generated with, +/-0.01. An element
    set's one-orbit rate is NOT that number: SGP4 adds about +0.007 deg/day
    (J2) and, at GEO, SDP4's lunisolar terms move it with the Moon. Measured
    for this very orbit through November 2026: -0.0130 to -0.0193 deg/day on
    a roughly two-week cycle, so the old assertion failed for a few days in
    every fortnight (it failed on the 1.7.12 source on 24 November at 23:00Z)
    and passed the rest. Now the expectation comes from the element sets
    themselves, with a physical bound that holds all month.
    """
    agreeing = [t["providers"][p]["elset_rate_deg_per_day"]
                for p in ("SAFRAN", "18 SPCS", "KBR", "EXO")]
    assert t["drift_deg_per_day"] == pytest.approx(statistics.median(agreeing), abs=1e-4)
    assert -0.04 < t["drift_deg_per_day"] < -0.005


def test_the_consensus_never_counts_a_provider_set_aside(tmp_path):
    t = track(tmp_path, _cosmos(tmp_path))
    cons = t["drift_consensus"]
    assert "NorthStar" not in cons["providers"] and cons["count"] == 4
    assert cons["min_deg_per_day"] <= t["drift_deg_per_day"] <= cons["max_deg_per_day"]
    assert t["providers"]["NorthStar"]["elset_rate_deg_per_day"] == pytest.approx(-2.28, abs=0.05)
    assert_the_agreeing_rate(t)


def test_one_checked_provider_is_published_as_one_source(tmp_path):
    t = track(tmp_path, ingest(tmp_path, rows("SAFRAN", 40.0, rate=0.2, hours=6)))
    assert t["agreement"]["status"] == SINGLE
    assert t["drift_basis"] == "element_sets" and t["drift_consensus"]["count"] == 1


def test_the_observed_track_is_kept_beside_the_consensus_as_a_cross_check(tmp_path):
    """Samples drift west at 0.5 deg/day for four days while every element set
    states synchronous motion: the consensus is published, the observed fit is
    shown beside it, and the disagreement is flagged rather than hidden."""
    batch = []
    for name, off in (("SAFRAN", 0.0), ("KBR", 0.01)):
        batch += [{**r, "meanMotion": GEO_N} for r in rows(name, 60.0 + off, rate=-0.5)]
    t = track(tmp_path, ingest(tmp_path, batch))
    assert t["drift_basis"] == "element_sets"
    assert t["drift_observed_deg_per_day"] == pytest.approx(-0.5, abs=0.05)
    assert t["drift_crosscheck"]["agrees"] is False
    assert t["drift_crosscheck"]["difference_deg_per_day"] < -0.4

