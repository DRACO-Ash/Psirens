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
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture
def client(tmp_path):
    app = create_app(_cfg(tmp_path),
                     sources=[DemoElsetSource(), ManualElsetSource(str(tmp_path))])
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
