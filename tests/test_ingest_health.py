"""Ingest health and the staleness alarm (1.6.6).

The defect these guard is not a crash, it is SILENCE. A feed returning
truncated pages left the plot a constant fortnight behind while the refresh
timestamp advanced hourly, the object count held steady and no log line fired
after boot. The owner found it by opening one object and reading its epoch.

So every test here is about whether the system SAYS something.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from psirens.main import create_app
from psirens.refresh import Refresher
from psirens.security import SingleFlight
from psirens.sources import DemoElsetSource, ManualElsetSource
from psirens.store import Store

from test_api import _cfg

_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _seeded_store(tmp_path, epoch: datetime) -> Store:
    """A store whose newest fix is exactly `epoch`."""
    store = Store(str(tmp_path))
    store.merge_samples({"43683": {
        "name": "BEIDOU-3 G1", "data_mode": "REAL", "classification_marking": "U",
        "source": "T", "origin": "UDL", "target": None,
        "samples": [{"epoch": epoch.isoformat(), "sub_lon_deg": 140.0,
                     "inclination_deg": 1.9}],
    }}, retention_days=3650, max_samples=100, now=epoch)
    return store


def _refresher(tmp_path, store, **cfg_kw):
    cfg = _cfg(tmp_path, **cfg_kw)
    return Refresher(cfg, store, [DemoElsetSource(),
                                  ManualElsetSource(str(tmp_path))], SingleFlight())


# -- the store can report its own freshness --------------------------------
def test_newest_epoch_is_none_for_an_empty_store(tmp_path):
    assert Store(str(tmp_path)).newest_epoch() is None


def test_newest_epoch_is_the_latest_across_all_objects(tmp_path):
    store = _seeded_store(tmp_path, _NOW - timedelta(days=14))
    store.merge_samples({"41836": {
        "name": "SES-10", "data_mode": "REAL", "classification_marking": "U",
        "source": "T", "origin": "UDL", "target": None,
        "samples": [{"epoch": (_NOW - timedelta(hours=2)).isoformat(),
                     "sub_lon_deg": -67.0, "inclination_deg": 0.02}],
    }}, retention_days=3650, max_samples=100, now=_NOW)
    newest = store.newest_epoch()
    assert newest == (_NOW - timedelta(hours=2)).isoformat()


# -- the alarm ------------------------------------------------------------
def test_fresh_data_is_not_stale(tmp_path):
    r = _refresher(tmp_path, _seeded_store(tmp_path, _NOW - timedelta(hours=2)))
    assert r.is_stale(_NOW) is False
    assert r.staleness_hours(_NOW) == pytest.approx(2.0, abs=0.01)


def test_the_reported_defect_trips_the_alarm(tmp_path):
    """The committed case that makes the guard go red: the exact condition the
    owner reported, a newest fix 14.1 days old."""
    r = _refresher(tmp_path, _seeded_store(tmp_path, _NOW - timedelta(days=14.1)))
    assert r.is_stale(_NOW) is True
    assert r.staleness_hours(_NOW) == pytest.approx(338.4, abs=1)


def test_an_empty_store_is_not_reported_as_stale(tmp_path):
    """Nothing ingested yet is a different condition from ingest having
    stopped, and conflating them would cry wolf on every cold start."""
    r = _refresher(tmp_path, Store(str(tmp_path)))
    assert r.staleness_hours(_NOW) is None
    assert r.is_stale(_NOW) is False


def test_the_threshold_is_configurable(tmp_path):
    store = _seeded_store(tmp_path, _NOW - timedelta(hours=30))
    assert _refresher(tmp_path, store, stale_after_hours=24.0).is_stale(_NOW)
    assert not _refresher(tmp_path, store, stale_after_hours=72.0).is_stale(_NOW)


# -- it must actually SAY so ----------------------------------------------
def test_a_stale_refresh_logs_a_warning(tmp_path, caplog):
    """The heart of it. Before 1.6.6 the scheduler discarded run_once's result
    and nothing was logged after the first tick."""
    r = _refresher(tmp_path, _seeded_store(tmp_path, _NOW - timedelta(days=14)))
    with caplog.at_level(logging.WARNING, logger="psirens.refresh"):
        r.report({"status": "ok", "added": 0, "errors": []}, now=_NOW)
    assert any("STALE DATA" in m for m in caplog.messages)


def test_a_healthy_refresh_does_not_warn(tmp_path, caplog):
    r = _refresher(tmp_path, _seeded_store(tmp_path, _NOW - timedelta(hours=1)))
    with caplog.at_level(logging.WARNING, logger="psirens.refresh"):
        r.report({"status": "ok", "added": 12, "errors": []}, now=_NOW)
    assert not any("STALE DATA" in m for m in caplog.messages)


def test_source_errors_are_logged_every_run(tmp_path, caplog):
    r = _refresher(tmp_path, _seeded_store(tmp_path, _NOW - timedelta(hours=1)))
    with caplog.at_level(logging.WARNING, logger="psirens.refresh"):
        r.report({"status": "ok", "added": 0,
                  "errors": ["UDLElsetSource: connect timeout"]}, now=_NOW)
    assert any("connect timeout" in m for m in caplog.messages)


def test_run_once_records_what_it_did(tmp_path):
    r = _refresher(tmp_path, Store(str(tmp_path)))
    out = r.run_once(now=_NOW)
    assert out["status"] == "ok"
    assert r.last_added == out["added"]
    assert r.last_errors == out["errors"]
    assert r.last_ok is not None, "a run that added samples must record success"


# -- and expose it -------------------------------------------------------
@pytest.fixture()
def client(tmp_path):
    app = create_app(_cfg(tmp_path),
                     sources=[DemoElsetSource(), ManualElsetSource(str(tmp_path))])
    app.state.refresher.run_once()
    with TestClient(app) as c:
        yield c


def test_meta_exposes_ingest_health(client):
    body = client.get("/api/meta").json()
    for key in ("last_refresh", "last_ingest_added", "last_ingest_errors",
                "newest_sample_epoch", "data_age_hours", "stale"):
        assert key in body, f"/api/meta must report {key}"


def test_readyz_reports_staleness_without_failing(client, tmp_path):
    """Stale data is not unreadiness. A 503 here would have the platform
    restart a container that is serving a correct last-known-good picture,
    fixing no feed and losing the store."""
    # A store of its own: seeding into the app's directory would merge with the
    # fresh demo population and the newest epoch would be today.
    stale_dir = tmp_path / "stale-store"
    stale_dir.mkdir()
    client.app.state.refresher.store = _seeded_store(
        stale_dir, _NOW - timedelta(days=14))
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["data_stale"] is True


def test_tracks_carries_the_threshold_for_the_interface(client):
    """The banner is drawn client-side from data already on screen, so the
    threshold has to travel with it."""
    assert client.get("/api/tracks?view=combined").json()["stale_after_hours"] > 0
