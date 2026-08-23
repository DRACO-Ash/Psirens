"""JCO High Risk Register (HRR) satellite list, pulled live from UDL.

The HRR GEO subset is the ONLY population PSIRENS plots. Non-HRR catalogue
objects are excluded so the belt is not skewed by inclined or graveyard objects.
The list is dynamic, so it is refreshed from UDL on a slow cadence (default 6h)
and on demand.

Source call (supplied by the tenant operator as the working form, used verbatim
including the relative `>now-N hours` operator):

    GET /udl/notification?createdAt=>now-<N> hours&dataMode=REAL
        &msgType=JCO-HRR-SATELLITES&source=JCO

Response shape is TBC against the tenant and therefore tolerant-parsed. Each
notification carries the HRR payload in `msgBody`; observed content is an array
of satellite objects with fields commonName, country, satNo, rank, orbitRegime.
`msgBody` may also arrive as a JSON string or wrapped in an envelope, so the
parser accepts a list, a JSON string, or a dict envelope and searches known
field-name variants. Anything it cannot parse leaves the last-known-good map
intact (anti-shrink); a cold start with no cache falls back to the bundled
static snapshot so the plot is never empty offline. Confirm the exact wire shape
with udl_hrr_probe.py before trusting a live pull.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone

import httpx

from .config import Config
from .store import resilient_write

_log = logging.getLogger("psirens.hrr")

# Field-name variants searched when parsing a satellite record (all TBC; the
# uploaded HRR snapshot uses the first of each group).
_SAT_LIST_KEYS = ("msgBody", "satellites", "sats", "data", "objects", "records", "body")
_SATNO_KEYS = ("satNo", "satno", "noradId", "noradCatId", "norad", "idOnOrbit", "scc")
_NAME_KEYS = ("commonName", "name", "objectName", "satName")
_COUNTRY_KEYS = ("country", "countryCode", "countryOfOrigin", "owner")
_RANK_KEYS = ("rank", "priority", "hrrRank", "tier")
_REGIME_KEYS = ("orbitRegime", "regime", "orbit", "orbitType")


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _as_sat_list(payload) -> list[dict]:
    """Coerce a notification msgBody into a list of satellite dicts, accepting a
    list, a JSON string, or a dict envelope."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in _SAT_LIST_KEYS:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        if _first(payload, _SATNO_KEYS) is not None:
            return [payload]  # a single-satellite dict
    return []


def _coerce_rank(raw) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _sat_entry(s: dict, regime_u: str) -> tuple[str, dict] | None:
    """Map one satellite row to (satNo, entry) when it is in the requested
    regime, else None."""
    sat_no = _first(s, _SATNO_KEYS)
    if sat_no is None:
        return None
    if str(_first(s, _REGIME_KEYS) or "").upper() != regime_u:
        return None
    return str(sat_no), {
        "name": str(_first(s, _NAME_KEYS) or sat_no),
        "country": str(_first(s, _COUNTRY_KEYS) or ""),
        "rank": _coerce_rank(_first(s, _RANK_KEYS)),
        "regime": regime_u,
    }


def _map_from_record(rec: dict, regime_u: str) -> dict[str, dict]:
    """Build {satNo: entry} for one notification record (empty if none match)."""
    mp: dict[str, dict] = {}
    for s in _as_sat_list(rec.get("msgBody", rec)):
        entry = _sat_entry(s, regime_u)
        if entry is not None:
            mp[entry[0]] = entry[1]
    return mp


def _created_key(r: dict) -> str:
    return str(r.get("createdAt") or r.get("createdTime") or r.get("createdDate") or "")


def parse_hrr(records, regime: str = "GEO") -> tuple[dict, str]:
    """Parse /udl/notification records into {satNo: {name, country, rank,
    regime}} filtered to the requested orbit regime, plus the classification
    marking. Picks the most recent record that yields satellites. Returns an
    empty map when nothing parses, so the caller can keep last-known-good."""
    regime_u = regime.upper()
    recs = records if isinstance(records, list) else [records]
    ordered = sorted((r for r in recs if isinstance(r, dict)),
                     key=_created_key, reverse=True)
    for rec in ordered:
        mp = _map_from_record(rec, regime_u)
        if mp:
            return mp, str(rec.get("classificationMarking") or "U")
    return {}, "U"


class HrrStore:
    """Holds the current HRR map, refreshes it from UDL, and persists a
    last-known-good copy. Reads are cheap; refresh is the only network call."""

    def __init__(self, cfg: Config, http_client: httpx.Client | None = None,
                 static_fallback: str | None = None):
        self.cfg = cfg
        self._client = http_client
        self._path = os.path.join(cfg.storage_dir(), "hrr.json")
        self._static_fallback = static_fallback
        self._map: dict[str, dict] = {}
        self._marking = "U"
        self._generated: str | None = None
        self._loaded = False

    # -- persistence ------------------------------------------------------
    def _load_cache(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                d = json.load(fh)
            self._map = d.get("objects", {}) or {}
            self._marking = d.get("marking", "U")
            self._generated = d.get("generated")
            if self._map:
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if self._static_fallback:  # cold-start seed from the bundled snapshot
            try:
                with open(self._static_fallback, encoding="utf-8") as fh:
                    d = json.load(fh)
                self._map = d.get("objects", {}) or {}
                self._marking = d.get("marking", "U")
                self._generated = d.get("generated")
                _log.info("HRR seeded from bundled snapshot: %d objects", len(self._map))
            except (FileNotFoundError, json.JSONDecodeError):
                self._map = {}

    def _persist(self) -> None:
        payload = {
            "marking": self._marking, "source": self.cfg.hrr_source,
            "regime": self.cfg.hrr_regime, "generated": self._generated,
            "count": len(self._map), "objects": self._map,
        }
        resilient_write(self.cfg.storage_dir(), self._path,
                        json.dumps(payload, separators=(",", ":")))

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_cache()
            self._loaded = True

    # -- accessors --------------------------------------------------------
    def map(self) -> dict[str, dict]:
        self.ensure_loaded()
        return self._map

    def geo_set(self) -> set[str]:
        return set(self.map().keys())

    def meta(self) -> dict:
        self.ensure_loaded()
        return {
            "marking": self._marking, "source": self.cfg.hrr_source,
            "regime": self.cfg.hrr_regime, "generated": self._generated,
            "count": len(self._map),
        }

    def as_payload(self) -> dict:
        m = self.meta()
        m["objects"] = self.map()
        return m

    # -- refresh ----------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        raw = f"{self.cfg.udl_user}:{self.cfg.udl_password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def _request_url(self) -> str:
        """Build the exact URL form the operator supplied, encoding the relative
        createdAt operator as `%3Enow-N%20hours` (verbatim working form)."""
        base = self.cfg.udl_base_url.rstrip("/") + self.cfg.udl_notification_path
        params = {
            "createdAt": f">now-{self.cfg.hrr_lookback_hours} hours",
            "dataMode": "REAL",
            "msgType": self.cfg.hrr_msg_type,
            "source": self.cfg.hrr_source,
        }
        return base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    def refresh(self, now: datetime | None = None) -> int:
        """Pull the HRR list from UDL. On any failure or empty parse, keep the
        last-known-good map (anti-shrink). Returns the object count in effect."""
        self.ensure_loaded()
        if not self.cfg.udl_enabled:
            return len(self._map)  # offline: static/last-known-good only
        client = self._client or httpx.Client(timeout=30.0)
        try:
            resp = client.get(self._request_url(),
                              headers={"Accept": "application/json", **self._auth_header()})
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            _log.warning("HRR fetch failed (keeping last-known-good, %d): %s",
                         len(self._map), exc)
            return len(self._map)
        finally:
            if self._client is None:
                client.close()
        new_map, marking = parse_hrr(rows, regime=self.cfg.hrr_regime)
        if not new_map:
            _log.warning("HRR pull parsed 0 %s objects; keeping last-known-good (%d). "
                         "Verify the notification shape with udl_hrr_probe.py.",
                         self.cfg.hrr_regime, len(self._map))
            return len(self._map)
        self._map = new_map
        self._marking = marking
        self._generated = (now or datetime.now(timezone.utc)).isoformat()
        self._persist()
        _log.info("HRR refreshed: %d %s objects (marking %s)",
                  len(self._map), self.cfg.hrr_regime, self._marking)
        return len(self._map)
