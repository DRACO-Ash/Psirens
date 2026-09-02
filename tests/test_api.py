from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from psirens.config import Config
from psirens.main import create_app
from psirens.sources import DemoElsetSource, ManualElsetSource


def _cfg(tmp_path, token="", origin="", **kw):
    base = dict(
        port=8080, allowed_origin=origin, team_token=token,
        retention_days=90, refresh_seconds=3600,
        lon_min=-180.0, lon_max=180.0, inc_min=0.0, inc_max=15.0,
        max_samples_per_object=2000, demo_mode=True, udl_enabled=False,
        udl_base_url="", udl_elset_path="/udl/elset", udl_user="",
        udl_password="", udl_target_field="tags", udl_epoch_param="epoch",
        udl_accept="application/json", data_dir=str(tmp_path),
        scheduler_enabled=False,  # deterministic: tests seed via run_once()
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture
def client(tmp_path):
    app = create_app(_cfg(tmp_path),
                     sources=[DemoElsetSource(), ManualElsetSource(str(tmp_path))])
    app.state.refresher.run_once()  # deterministic seed (prod seeds in background)
    with TestClient(app) as c:
        yield c


def test_root_returns_200_html_not_redirect(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "PSIRENS" in r.text
    assert "Content-Security-Policy" in r.headers


def test_healthz_200_unauthenticated(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readyz_200(client):
    assert client.get("/readyz").status_code == 200


def test_tracks_shape_and_etag_304(client):
    r = client.get("/api/tracks?view=combined")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1 and "classification_banner" in body
    etag = r.headers["ETag"]
    r2 = client.get("/api/tracks?view=combined", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_real_view_excludes_sim_modes(client):
    real = client.get("/api/tracks?view=real").json()
    modes = {t["data_mode"] for t in real["tracks"]}
    assert modes <= {"REAL"}
    combined = client.get("/api/tracks?view=combined").json()
    cmodes = {t["data_mode"] for t in combined["tracks"]}
    assert {"SIMULATED", "EXERCISE"} & cmodes


def test_tracks_rejects_bad_view_and_mode(client):
    assert client.get("/api/tracks?view=nope").status_code == 400
    assert client.get("/api/tracks?view=combined&modes=BOGUS").status_code == 400


def test_tracks_drift_and_target_present(client):
    body = client.get("/api/tracks?view=combined").json()
    by = {t["object_id"]: t for t in body["tracks"]}
    assert by["90210"]["drift_deg_per_day"] > 0        # eastward drifter
    assert by["77777"]["target"] == "43683"            # exercise inspector target


def test_manual_elset_roundtrip_no_token(client):
    payload = {
        "object_id": "MZ1", "name": "temp inject",
        "epoch": datetime(2026, 7, 20, tzinfo=timezone.utc).isoformat(),
        "inclination_deg": 2.0, "eccentricity": 0.0, "raan_deg": 10.0,
        "argp_deg": 0.0, "mean_anomaly_deg": 0.0,
        "mean_motion_rev_per_day": 1.0027, "data_mode": "SIMULATED",
        "target": "43683", "ttl_hours": 2.0,
    }
    assert client.post("/api/manual-elset", json=payload).status_code == 200
    combined = client.get("/api/tracks?view=combined").json()
    assert any(t["object_id"] == "MZ1" for t in combined["tracks"])
    assert client.delete("/api/manual-elset/MZ1").status_code == 200
    assert client.delete("/api/manual-elset/MZ1").status_code == 404


def test_manual_elset_rejects_invalid_body(client):
    bad = {"object_id": "B", "name": "b",
           "epoch": "2026-07-20T00:00:00+00:00", "inclination_deg": 500.0,
           "eccentricity": 0.0, "raan_deg": 0.0, "argp_deg": 0.0,
           "mean_anomaly_deg": 0.0, "mean_motion_rev_per_day": 1.0}
    assert client.post("/api/manual-elset", json=bad).status_code == 422


def test_state_change_requires_token_when_set(tmp_path):
    app = create_app(_cfg(tmp_path, token="s3cret", origin="https://x.test"),
                     sources=[DemoElsetSource()])
    with TestClient(app) as c:
        r = c.post("/api/refresh")
        assert r.status_code == 401
        r2 = c.post("/api/refresh", headers={"Authorization": "Bearer s3cret"})
        assert r2.status_code == 200


def test_refresh_rate_limited(tmp_path):
    app = create_app(_cfg(tmp_path), sources=[DemoElsetSource()])
    with TestClient(app) as c:
        codes = [c.post("/api/refresh").status_code for _ in range(10)]
        assert 429 in codes  # strict tier trips


def test_wildcard_origin_with_token_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(_cfg(tmp_path, token="t", origin="*"), sources=[DemoElsetSource()])


def test_meta_lists_views(client):
    m = client.get("/api/meta").json()
    assert m["views"]["real"] == ["REAL"]
    assert set(m["views"]["combined"]) == {"REAL", "SIMULATED", "TEST", "EXERCISE"}


def test_favicon_and_icon_assets_served(client):
    assert client.get("/favicon.ico").status_code == 200
    r = client.get("/static/icon-512.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    m = client.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and "PSIRENS" in m.text


def test_static_asset_rejects_unknown_name(client):
    # path-traversal / unknown files are refused (whitelist only)
    assert client.get("/static/etc-passwd").status_code == 404


def test_hrr_endpoint_returns_map(client):
    r = client.get("/api/hrr")
    assert r.status_code == 200
    j = r.json()
    assert "objects" in j and "count" in j and "marking" in j
    assert j["count"] == len(j["objects"])


def test_conjunctions_requires_target(client):
    r = client.get("/api/conjunctions")
    assert r.status_code == 400
    assert "target" in r.json()["detail"]


def test_conjunctions_endpoint_returns_target_tle(client):
    # demo anchors carry a synthesised elset, so the target exports a TLE even
    # though these anchors are too far apart to have neighbours within +/-10deg.
    r = client.get("/api/conjunctions?target=41836")
    assert r.status_code == 200
    j = r.json()
    assert j["target"] is not None
    assert j["target"]["tle"] is not None and len(j["target"]["tle"]) == 2
    assert "neighbours" in j and "window_hours" in j


def test_conjunctions_payload_carries_tle_provenance(client):
    """Every served line states where it came from and whether it is the
    provider's own fix or a reconstruction."""
    j = client.get("/api/conjunctions?target=41836").json()
    prov = j["target"]["tle_provenance"]
    assert prov is not None
    # The demo belt synthesises elements, so these are reconstructions and
    # must be labelled as such rather than presented as a provider fix.
    assert prov["native"] is False
    assert prov["source"] == "DEMO"
    assert prov["source_class"] == "unknown"
    assert prov["epoch"]
    assert set(prov) >= {"native", "source", "source_class", "epoch",
                         "classification_marking", "copyable",
                         "copy_denied_reason"}


def test_conjunctions_copy_gate_follows_the_marking(client):
    """Demo elsets are plain unclassified, so copy-out is permitted; the gate
    itself is exercised exhaustively in test_tle.py."""
    j = client.get("/api/conjunctions?target=41836").json()
    prov = j["target"]["tle_provenance"]
    assert prov["classification_marking"] == "U"
    assert prov["copyable"] is True
    assert prov["copy_denied_reason"] is None


def test_conjunctions_respect_the_configured_source_policy(tmp_path):
    """The policy reaches the route from config, not just the library."""
    from psirens.conjunction import tle_for
    from psirens.tle import parse_priority
    app = create_app(_cfg(tmp_path, tle_source_priority="government,commercial"),
                     sources=[DemoElsetSource(), ManualElsetSource(str(tmp_path))])
    app.state.refresher.run_once()
    with TestClient(app) as c:
        assert c.get("/api/conjunctions?target=41836").status_code == 200
    obj = {"elset_candidates": {
        "18SDS": {"sat_no": "1", "epoch": "2026-08-01T00:00:00+00:00",
                  "source": "18SDS", "classification": "U",
                  "inclination_deg": 0.05, "eccentricity": 0.0002,
                  "raan_deg": 80.0, "argp_deg": 90.0, "mean_anomaly_deg": 10.0,
                  "mean_motion_rev_per_day": 1.0027379, "bstar": 0.0},
        "LeoLabs": {"sat_no": "1", "epoch": "2026-08-09T00:00:00+00:00",
                    "source": "LeoLabs", "classification": "U",
                    "inclination_deg": 0.05, "eccentricity": 0.0002,
                    "raan_deg": 80.0, "argp_deg": 90.0, "mean_anomaly_deg": 10.0,
                    "mean_motion_rev_per_day": 1.0027379, "bstar": 0.0}}}
    el, _, _ = tle_for(obj, parse_priority("government,commercial"))
    assert el["source"] == "18SDS"   # government first, despite the older epoch


# -- SIMULATION view, timescale pull, and non-destructive prune ---------------
def test_tracks_sim_view_is_simulated_only(client):
    r = client.get("/api/tracks?view=sim")
    assert r.status_code == 200
    modes = {t["data_mode"] for t in r.json()["tracks"]}
    assert modes <= {"SIMULATED"}          # a clean picture: zero REAL
    assert "SIMULATED" in modes            # the demo belt carries one


def test_tracks_real_view_is_real_only(client):
    r = client.get("/api/tracks?view=real")
    assert r.status_code == 200
    assert all(t["data_mode"] == "REAL" for t in r.json()["tracks"])


def test_pull_sim_returns_ok(client):
    r = client.post("/api/pull?mode=sim&hours=48")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["mode"] == "sim"
    assert "start" in j and "end" in j


def test_pull_rejects_unknown_mode(client):
    assert client.post("/api/pull?mode=nonsense&hours=24").status_code == 400


def test_pull_rejects_non_integer_hours(client):
    assert client.post("/api/pull?mode=real&hours=lots").status_code == 400


def test_retain_only_keeps_simulated_drops_stale_real(tmp_path):
    from datetime import datetime, timezone
    from psirens.store import Store
    st = Store(str(tmp_path))
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    sample = [{"epoch": now.isoformat(), "sub_lon_deg": 0.0, "inclination_deg": 0.0}]
    st.merge_samples({
        "R1": {"name": "real-hrr", "data_mode": "REAL", "origin": "UDL", "samples": sample},
        "R2": {"name": "real-stale", "data_mode": "REAL", "origin": "UDL", "samples": sample},
        "S1": {"name": "sim", "data_mode": "SIMULATED", "origin": "UDL", "samples": sample},
    }, retention_days=90, max_samples=100, now=now)
    removed = st.retain_only({"R1"})       # only R1 is on the HRR list
    kept = set(st.load()["objects"])
    assert removed == 1                     # R2 (stale REAL) dropped
    assert kept == {"R1", "S1"}             # simulated survives the HRR prune


def test_pull_real_brings_real_data(client):
    r = client.post("/api/pull?mode=real&hours=24")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert client.get("/api/tracks?view=real").json()["count"] >= 1


def test_pull_combined_pulls_both(client):
    r = client.post("/api/pull?mode=combined&hours=72")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["mode"] == "combined"


# -- absolute scenario start/stop window --------------------------------------
def test_pull_absolute_range_ok(client):
    r = client.post("/api/pull?mode=sim"
                    "&start=2026-07-01T00:00:00Z&end=2026-07-08T00:00:00Z")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["mode"] == "sim"
    assert j["start"].startswith("2026-07-01") and j["end"].startswith("2026-07-08")


def test_pull_absolute_naive_treated_as_utc(client):
    r = client.post("/api/pull?mode=real"
                    "&start=2026-07-01T00:00&end=2026-07-02T00:00")
    assert r.status_code == 200
    assert "+00:00" in r.json()["start"]  # naive input anchored to UTC


def test_pull_range_requires_both_ends(client):
    assert client.post("/api/pull?mode=sim&start=2026-07-01T00:00:00Z").status_code == 400


def test_pull_range_rejects_start_after_end(client):
    r = client.post("/api/pull?mode=sim"
                    "&start=2026-07-08T00:00:00Z&end=2026-07-01T00:00:00Z")
    assert r.status_code == 400


def test_pull_range_rejects_bad_datetime(client):
    r = client.post("/api/pull?mode=sim&start=not-a-date&end=2026-07-01T00:00:00Z")
    assert r.status_code == 400


def test_pull_range_rejects_oversized_span(client):
    r = client.post("/api/pull?mode=sim"
                    "&start=2024-01-01T00:00:00Z&end=2026-01-01T00:00:00Z")
    assert r.status_code == 400


def test_tracks_carry_ra_deg(client):
    r = client.get("/api/tracks?view=real")
    assert r.status_code == 200
    tracks = r.json()["tracks"]
    assert tracks and all("ra_deg" in t for t in tracks)  # RAAN for the bearing needle


def test_scheduler_seeds_on_first_tick(tmp_path):
    """The background loop runs run_once on its first tick and seeds the store.
    Deterministic: one tick, then stop. Covers refresh.Refresher.scheduler."""
    import asyncio

    from psirens.refresh import Refresher
    from psirens.security import SingleFlight
    from psirens.store import Store as _Store

    refresher = Refresher(_cfg(tmp_path), _Store(str(tmp_path)),
                          [DemoElsetSource()], SingleFlight(), hrr=None)

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(refresher.scheduler(stop))
        await asyncio.sleep(0.15)   # let the first tick complete run_once
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
    assert refresher.store.load().get("objects")  # first tick seeded the belt


def test_coplanar_requires_target(client):
    assert client.get("/api/coplanar").status_code == 400


def test_coplanar_endpoint_returns_primary_and_tracks(client):
    r = client.get("/api/coplanar?target=41836")  # a demo anchor with an elset
    assert r.status_code == 200
    j = r.json()
    assert "primary" in j and "tracks" in j and "half_width_deg" in j
