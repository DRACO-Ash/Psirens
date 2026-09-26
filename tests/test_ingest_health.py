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


# -- a failed upstream request is an ERROR, not silence (1.6.9) -------------
#
# The defect this guards was found in production on 26 September 2026:
# /api/meta reported `last_ingest_added: 0` beside `last_ingest_errors: []`
# with a 403-hour-old picture. `UDLElsetSource._request` swallows a failure so
# one bad slice never kills a cycle, and nothing counted the swallowing. A
# rejected feed was therefore indistinguishable from a quiet one, which is the
# same class of blindness 1.6.6 fixed one layer higher up.
def _udl_source(status: int, rows=None):
    import httpx

    from psirens.sources import UDLElsetSource
    from test_sources import _cfg_udl

    cfg = _cfg_udl()
    body = httpx.Response(status, json=rows) if rows is not None \
        else httpx.Response(status, text="denied")
    client = httpx.Client(transport=httpx.MockTransport(lambda _req: body))
    return cfg, UDLElsetSource(cfg, client=client)


def _window():
    return _NOW - timedelta(hours=6), _NOW


def test_a_rejected_request_is_counted_not_swallowed():
    _cfg, src = _udl_source(401)
    start, end = _window()
    assert src.fetch(start, end) == {}          # still degrades to empty
    assert src.last_stats.requests == 1
    assert src.last_stats.failures == 1
    assert "HTTP 401" in src.last_stats.summary()


def test_a_successful_request_counts_its_rows():
    rows = [{"satNo": "43683", "epoch": "2026-09-24T09:00:00.000000Z",
             "inclination": 1.9, "eccentricity": 0.0002, "raan": 100.0,
             "argOfPerigee": 20.0, "meanAnomaly": 30.0, "meanMotion": 1.0027,
             "dataMode": "REAL", "classificationMarking": "U", "source": "18SDS"}]
    _cfg, src = _udl_source(200, rows)
    start, end = _window()
    src.fetch(start, end)
    assert src.last_stats.failures == 0
    assert src.last_stats.rows == 1
    assert src.last_stats.objects == 1


def test_the_refresher_reports_a_rejected_feed_as_an_error(tmp_path):
    """THE regression. Reverting `_absorb_stats` leaves this list empty, which
    is exactly what the live /api/meta showed."""
    cfg, src = _udl_source(403)
    store = _seeded_store(tmp_path, _NOW - timedelta(days=16))
    ref = Refresher(cfg, store, [src], SingleFlight())
    result = ref.run_once(now=_NOW)
    assert result["added"] == 0
    assert result["request_failures"] == 1
    assert ref.health(_NOW)["last_ingest_errors"], \
        "a rejected upstream must not report an empty error list"
    assert "HTTP 403" in ref.health(_NOW)["last_ingest_errors"][0]
    assert "upstream requests are failing" in ref.diagnosis()


def test_records_filtered_out_by_the_high_interest_list_are_visible(tmp_path):
    """The other reading of a zero: the upstream answered, and everything it
    sent was then dropped. Offered and kept have to be reported separately or
    the two are indistinguishable."""
    rows = [{"satNo": "99999", "epoch": "2026-09-24T09:00:00.000000Z",
             "inclination": 1.9, "eccentricity": 0.0002, "raan": 100.0,
             "argOfPerigee": 20.0, "meanAnomaly": 30.0, "meanMotion": 1.0027,
             "dataMode": "REAL", "classificationMarking": "U", "source": "18SDS"}]
    cfg, src = _udl_source(200, rows)

    class _Hrr:  # a list that does not contain the object on the wire
        def geo_set(self):
            return {"43683"}

    store = _seeded_store(tmp_path, _NOW - timedelta(days=16))
    ref = Refresher(cfg, store, [src], SingleFlight(), hrr=_Hrr())
    ref.run_once(now=_NOW)
    health = ref.health(_NOW)
    assert health["last_ingest_fetched"] == 1
    assert health["last_ingest_kept"] == 0
    assert health["last_ingest_added"] == 0
    assert "high-interest list" in health["ingest_diagnosis"]


def test_meta_carries_the_stage_counts(client):
    body = client.get("/api/meta").json()
    for key in ("last_ingest_requests", "last_ingest_request_failures",
                "last_ingest_rows", "last_ingest_fetched", "last_ingest_kept",
                "hrr_list_size", "ingest_diagnosis"):
        assert key in body, f"/api/meta must report {key}"
