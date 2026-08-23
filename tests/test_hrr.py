"""HRR list: tolerant notification parsing, store fallback and refresh, the
store prune, and the refresher's HRR-only ingest filter."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from psirens.config import Config
from psirens.hrr import HrrStore, parse_hrr
from psirens.refresh import Refresher
from psirens.security import SingleFlight
from psirens.store import Store


def _cfg(tmp_path, **kw) -> Config:
    base = dict(
        port=8080, allowed_origin="", team_token="",
        retention_days=90, refresh_seconds=3600,
        lon_min=-180.0, lon_max=180.0, inc_min=0.0, inc_max=15.0,
        max_samples_per_object=2000, demo_mode=False, udl_enabled=True,
        udl_base_url="https://udl.test", udl_elset_path="/udl/elset", udl_user="u",
        udl_password="p", udl_target_field="tags", udl_epoch_param="epoch",
        udl_accept="application/json", data_dir=str(tmp_path),
    )
    base.update(kw)
    return Config(**base)


# A satellite row in the real HRR field shape (from the uploaded snapshot).
def _sat(satno, name, country, rank, regime):
    return {"commonName": name, "country": country, "satNo": str(satno),
            "rank": rank, "orbitRegime": regime}


_SATS = [
    _sat(100172, "TIANLIAN-3 01", "CHN", 1, "GEO"),
    _sat(58204, "TJS-10", "CHN", 2, "GEO"),
    _sat(11111, "SOME-LEO", "USA", 3, "LEO"),   # filtered out (not GEO)
]


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._d


class _Client:
    """Minimal httpx.Client stand-in capturing the request URL."""
    def __init__(self, data):
        self._d = data
        self.urls: list[str] = []

    def get(self, url, headers=None):
        self.urls.append(url)
        return _Resp(self._d)


# --------------------------------------------------------------------------
# parse_hrr
# --------------------------------------------------------------------------
def test_parse_hrr_msgbody_as_list_filters_to_geo():
    records = [{"createdAt": "2026-08-05T05:00:00Z",
                "classificationMarking": "U//DS-JCO-NOTIF",
                "msgType": "JCO-HRR-SATELLITES", "msgBody": _SATS}]
    mp, marking = parse_hrr(records, regime="GEO")
    assert set(mp) == {"100172", "58204"}          # LEO excluded
    assert marking == "U//DS-JCO-NOTIF"
    assert mp["100172"] == {"name": "TIANLIAN-3 01", "country": "CHN",
                            "rank": 1, "regime": "GEO"}
    assert isinstance(next(iter(mp)), str)          # satNo carried as string


def test_parse_hrr_msgbody_as_json_string_envelope():
    # msgBody arrives as a STRING that decodes to an envelope wrapping the list.
    envelope = json.dumps({"classificationMarking": "U", "msgBody": _SATS})
    records = [{"createdAt": "2026-08-05T05:00:00Z", "msgBody": envelope}]
    mp, _ = parse_hrr(records, regime="GEO")
    assert set(mp) == {"100172", "58204"}


def test_parse_hrr_picks_newest_record():
    old = {"createdAt": "2026-08-01T00:00:00Z", "msgBody": [_sat(1, "OLD", "X", 5, "GEO")]}
    new = {"createdAt": "2026-08-05T00:00:00Z", "msgBody": [_sat(2, "NEW", "Y", 4, "GEO")]}
    mp, _ = parse_hrr([old, new], regime="GEO")
    assert set(mp) == {"2"}          # newest wins


def test_parse_hrr_garbage_returns_empty():
    assert parse_hrr(["nonsense", 42, {}], regime="GEO") == ({}, "U")


# --------------------------------------------------------------------------
# HrrStore
# --------------------------------------------------------------------------
def test_hrrstore_static_fallback(tmp_path):
    static = tmp_path / "hrr-geo.json"
    static.write_text(json.dumps({"marking": "U//X", "source": "JCO", "count": 1,
                                  "objects": {"39216": {"name": "INSAT 3D",
                                                        "country": "IND", "rank": 4}}}))
    store = HrrStore(_cfg(tmp_path, udl_enabled=False), static_fallback=str(static))
    assert store.geo_set() == {"39216"}
    assert store.meta()["source"] == "JCO"


def test_hrrstore_refresh_offline_is_noop(tmp_path):
    static = tmp_path / "hrr-geo.json"
    static.write_text(json.dumps({"objects": {"39216": {"name": "INSAT 3D",
                                                        "country": "IND", "rank": 4}}}))
    store = HrrStore(_cfg(tmp_path, udl_enabled=False), static_fallback=str(static))
    assert store.refresh() == 1          # keeps the static list, no network


def test_hrrstore_refresh_success_persists(tmp_path):
    rows = [{"createdAt": "2026-08-05T05:00:00Z",
             "classificationMarking": "U//DS-JCO-NOTIF", "msgBody": _SATS}]
    client = _Client(rows)
    store = HrrStore(_cfg(tmp_path), http_client=client)
    n = store.refresh(now=datetime(2026, 8, 5, 6, tzinfo=timezone.utc))
    assert n == 2
    assert store.geo_set() == {"100172", "58204"}
    # exact operator form is preserved in the request URL
    assert "createdAt=%3Enow-6%20hours" in client.urls[0]
    assert "msgType=JCO-HRR-SATELLITES" in client.urls[0]
    # persisted to disk as last-known-good
    on_disk = json.loads((tmp_path / "hrr.json").read_text())
    assert on_disk["count"] == 2 and on_disk["marking"] == "U//DS-JCO-NOTIF"


def test_hrrstore_refresh_empty_keeps_last_known_good(tmp_path):
    static = tmp_path / "hrr-geo.json"
    static.write_text(json.dumps({"objects": {"39216": {"name": "INSAT 3D",
                                                        "country": "IND", "rank": 4}}}))
    store = HrrStore(_cfg(tmp_path), http_client=_Client([]), static_fallback=str(static))
    assert store.refresh() == 1          # empty parse -> keep the seeded list
    assert store.geo_set() == {"39216"}


# --------------------------------------------------------------------------
# Store.retain_only
# --------------------------------------------------------------------------
def test_store_retain_only_drops_non_hrr(tmp_path):
    store = Store(str(tmp_path))
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    incoming = {
        "100172": {"origin": "UDL", "samples": [{"epoch": now.isoformat(),
                                                 "sub_lon_deg": 60.0, "inclination_deg": 2.0}]},
        "45011": {"origin": "UDL", "samples": [{"epoch": now.isoformat(),
                                                "sub_lon_deg": -20.0, "inclination_deg": 5.0}]},
        "99001": {"origin": "MANUAL", "samples": [{"epoch": now.isoformat(),
                                                   "sub_lon_deg": 10.0, "inclination_deg": 1.0}]},
    }
    store.merge_samples(incoming, retention_days=90, max_samples=2000, now=now)
    removed = store.retain_only({"100172"}, keep_origins=("MANUAL",))
    kept = set(store.load()["objects"])
    assert removed == 1                  # only the non-HRR UDL object dropped
    assert kept == {"100172", "99001"}   # HRR kept, manual kept


# --------------------------------------------------------------------------
# Refresher HRR-only ingest filter + prune
# --------------------------------------------------------------------------
class _StubHrr:
    def __init__(self, ids):
        self._ids = set(ids)
        self.refreshed = 0

    def geo_set(self):
        return set(self._ids)

    def refresh(self, now=None):
        self.refreshed += 1
        return len(self._ids)


class _StubSource:
    """Returns one HRR object and one non-HRR object."""
    def fetch(self, start, end):
        ep = end.isoformat()
        return {
            "100172": {"name": "TIANLIAN-3 01", "data_mode": "REAL",
                       "classification_marking": "U", "source": "S", "origin": "UDL",
                       "target": None,
                       "samples": [{"epoch": ep, "sub_lon_deg": 60.0, "inclination_deg": 2.0}]},
            "45011": {"name": "CAT", "data_mode": "REAL",
                      "classification_marking": "U", "source": "S", "origin": "UDL",
                      "target": None,
                      "samples": [{"epoch": ep, "sub_lon_deg": -20.0, "inclination_deg": 5.0}]},
        }


def test_refresher_ingests_hrr_only(tmp_path):
    cfg = _cfg(tmp_path)                 # udl_enabled=True
    store = Store(str(tmp_path))
    hrr = _StubHrr({"100172"})
    r = Refresher(cfg, store, [_StubSource()], SingleFlight(), hrr=hrr)
    out = r.run_once(now=datetime(2026, 8, 5, 12, tzinfo=timezone.utc))
    assert out["status"] == "ok"
    assert hrr.refreshed == 1            # HRR pulled on first run
    assert set(store.load()["objects"]) == {"100172"}   # non-HRR never ingested


def test_refresher_offline_keeps_all(tmp_path):
    cfg = _cfg(tmp_path, udl_enabled=False)   # offline: no HRR filter
    store = Store(str(tmp_path))
    hrr = _StubHrr({"100172"})
    r = Refresher(cfg, store, [_StubSource()], SingleFlight(), hrr=hrr)
    r.run_once(now=datetime(2026, 8, 5, 12, tzinfo=timezone.utc))
    assert set(store.load()["objects"]) == {"100172", "45011"}   # unfiltered
    assert hrr.refreshed == 0            # no pull when UDL disabled
