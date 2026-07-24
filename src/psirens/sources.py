"""Element-set sources and normalisation into store-ready records.

Three sources implement the same shape:
  * UDLElsetSource   - pulls /udl/elset (all wire details TBC, env-overridable);
  * ManualElsetSource- operator-supplied temporary elsets from the store;
  * DemoElsetSource  - deterministic synthetic belt for the offline/--demo path.

Every raw elset is normalised, its sub-satellite longitude computed via the
validated astro module, and emitted as a sample keyed by (object_id, epoch).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .astro import sub_longitude_deg
from .config import Config
from .models import DataMode, ManualElsetIn

_log = logging.getLogger("psirens.sources")


def _norm_mode(raw: str | None) -> DataMode:
    try:
        return DataMode((raw or "REAL").upper())
    except ValueError:
        return DataMode.REAL


def _sample_from_elements(
    *,
    inclination_deg: float,
    eccentricity: float,
    raan_deg: float,
    argp_deg: float,
    mean_anomaly_deg: float,
    mean_motion_rev_per_day: float,
    bstar: float,
    epoch: datetime,
) -> dict | None:
    lon = sub_longitude_deg(
        inclination_deg=inclination_deg,
        eccentricity=eccentricity,
        raan_deg=raan_deg,
        argp_deg=argp_deg,
        mean_anomaly_deg=mean_anomaly_deg,
        mean_motion_rev_per_day=mean_motion_rev_per_day,
        bstar=bstar,
        epoch=epoch,
    )
    if lon is None:
        return None
    return {
        "epoch": epoch.astimezone(timezone.utc).isoformat(),
        "sub_lon_deg": round(lon, 4),
        "inclination_deg": round(inclination_deg, 4),
    }


class ElsetSource(Protocol):
    def fetch(self, start: datetime, end: datetime) -> dict[str, dict]:
        """Return object_id -> record with meta and a `samples` list."""
        ...


# --------------------------------------------------------------------------
# UDL
# --------------------------------------------------------------------------
class UDLElsetSource:
    """Pulls element sets from UDL.

    WARNING (owner action): every wire detail below is TBC. The LEARNED
    register only verifies /udl/eoobservation behaviour; the Accept-header
    quirk and the 10,000 firstResult cap are NOT assumed to apply to /udl/elset.
    Confirm the endpoint path, the epoch range parameter, the Accept header,
    and which labelled field carries the intended target against the tenant.
    """

    def __init__(self, cfg: Config, client: httpx.Client | None = None):
        self.cfg = cfg
        self._client = client

    def _auth_header(self) -> dict[str, str]:
        raw = f"{self.cfg.udl_user}:{self.cfg.udl_password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def fetch(self, start: datetime, end: datetime) -> dict[str, dict]:
        client = self._client or httpx.Client(timeout=30.0)
        headers = {"Accept": self.cfg.udl_accept, **self._auth_header()}
        url = self.cfg.udl_base_url.rstrip("/") + self.cfg.udl_elset_path
        params = {
            self.cfg.udl_epoch_param: (
                f"{start.astimezone(timezone.utc).isoformat()}.."
                f"{end.astimezone(timezone.utc).isoformat()}"
            )
        }
        try:
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            _log.warning("UDL elset fetch failed (never fatal): %s", exc)
            return {}
        finally:
            if self._client is None:
                client.close()
        return self._normalise(rows if isinstance(rows, list) else [])

    def _target_of(self, row: dict) -> str | None:
        val = row.get(self.cfg.udl_target_field)
        if isinstance(val, list) and val:
            return str(val[0])
        return str(val) if val not in (None, "") else None

    def _normalise(self, rows: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in rows:
            oid = str(row.get("satNo") or row.get("idOnOrbit") or row.get("id") or "")
            if not oid:
                continue
            try:
                epoch = datetime.fromisoformat(str(row["epoch"]).replace("Z", "+00:00"))
                sample = _sample_from_elements(
                    inclination_deg=float(row["inclination"]),
                    eccentricity=float(row.get("eccentricity", 0.0)),
                    raan_deg=float(row.get("raan", 0.0)),
                    argp_deg=float(row.get("argOfPerigee", 0.0)),
                    mean_anomaly_deg=float(row.get("meanAnomaly", 0.0)),
                    mean_motion_rev_per_day=float(row["meanMotion"]),
                    bstar=float(row.get("bStar", 0.0)),
                    epoch=epoch,
                )
            except (KeyError, ValueError, TypeError):
                continue
            if sample is None:
                continue
            rec = out.setdefault(oid, {
                "name": str(row.get("origObjectId") or row.get("satNo") or oid),
                "data_mode": _norm_mode(row.get("dataMode")).value,
                "classification_marking": str(row.get("classificationMarking", "U")),
                "source": str(row.get("source", "UDL")),
                "origin": "UDL",
                "target": self._target_of(row),
                "samples": [],
            })
            rec["samples"].append(sample)
        return out


# --------------------------------------------------------------------------
# Manual (temporary, operator-supplied)
# --------------------------------------------------------------------------
class ManualElsetSource:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "manual.json")

    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, items: list[dict]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(items, fh)
        os.replace(tmp, self.path)

    def add(self, elset: ManualElsetIn, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        items = [i for i in self._load() if i["object_id"] != elset.object_id]
        payload = elset.model_dump(mode="json")
        payload["_expires_at"] = (now + timedelta(hours=elset.ttl_hours)).isoformat()
        items.append(payload)
        self._save(items)

    def remove(self, object_id: str) -> bool:
        items = self._load()
        kept = [i for i in items if i["object_id"] != object_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def list_active(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        active = [
            i for i in self._load()
            if datetime.fromisoformat(i["_expires_at"]) > now
        ]
        if len(active) != len(self._load()):
            self._save(active)  # prune expired
        return active

    def fetch(self, start: datetime, end: datetime) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in self.list_active():
            epoch = datetime.fromisoformat(i["epoch"])
            sample = _sample_from_elements(
                inclination_deg=i["inclination_deg"],
                eccentricity=i["eccentricity"],
                raan_deg=i["raan_deg"],
                argp_deg=i["argp_deg"],
                mean_anomaly_deg=i["mean_anomaly_deg"],
                mean_motion_rev_per_day=i["mean_motion_rev_per_day"],
                bstar=i.get("bstar", 0.0),
                epoch=epoch,
            )
            if sample is None:
                continue
            out[i["object_id"]] = {
                "name": i["name"],
                "data_mode": i["data_mode"],
                "classification_marking": i["classification_marking"],
                "source": i["source"],
                "origin": "MANUAL",
                "target": i.get("target"),
                "samples": [sample],
            }
        return out


# --------------------------------------------------------------------------
# Demo (deterministic, offline; the sandbox cannot reach UDL)
# --------------------------------------------------------------------------
class DemoElsetSource:
    """Synthetic GEO belt: station-kept anchors, longitude drifters, an
    inclined-orbit object, and an EXERCISE inspector that closes on a target.
    Deterministic given `now` so tests and CI are stable and offline-safe.
    """

    _GEO_MM = 1.0027379093  # sidereal rev/day

    def fetch(self, start: datetime, end: datetime) -> dict[str, dict]:
        now = end
        out: dict[str, dict] = {}

        # 1) Station-kept anchors (REAL), one point at `now`.
        anchors = [
            ("41836", "SES-10", -67.0, 0.02, "REAL"),
            ("28924", "EUTELSAT 174A", 174.0, 0.05, "REAL"),
            ("43683", "BEIDOU-3 G1", 140.0, 1.9, "REAL"),
            ("41748", "USA 270", 105.0, 3.6, "REAL"),
        ]
        for oid, name, lon, inc, mode in anchors:
            out[oid] = self._track(oid, name, mode, "DEMO", None,
                                   self._span(now, 1, lon, inc, 0.0))

        # 2) Longitude drifter (REAL), moving east ~0.9 deg/day over 40 days.
        out["90210"] = self._track(
            "90210", "DRIFTER-1", "REAL", "DEMO", None,
            self._span(now, 40, 120.0, 3.1, 0.9),
        )
        # 3) Relocating satellite (SIMULATED), west then station-keeping.
        out["99001"] = self._track(
            "99001", "RELOCATE-SIM", "SIMULATED", "DEMO", None,
            self._span(now, 30, 90.0, 0.4, -1.4),
        )
        # 4) Inclined-orbit object climbing in inclination (TEST).
        incl = self._span(now, 25, 62.0, 0.5, 0.0)
        for k, s in enumerate(incl):
            s["inclination_deg"] = round(0.5 + 0.11 * k, 4)
        out["55555"] = self._track("55555", "INCLINED-TEST", "TEST", "DEMO", None, incl)

        # 5) EXERCISE inspector closing on anchor 43683 (its labelled target).
        out["77777"] = self._track(
            "77777", "INSPECTOR-EX", "EXERCISE", "DEMO", "43683",
            self._span(now, 20, 133.0, 1.5, 0.35),
        )
        return out

    def _span(self, now: datetime, days: int, lon0: float, inc: float,
              rate: float) -> list[dict]:
        pts = []
        n = max(2, days)
        # Quantise to midnight UTC so repeated same-day refreshes produce
        # identical epoch strings and dedup cleanly (a stable one-per-day trail).
        base = now.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for k in range(n):
            t = base - timedelta(days=(n - 1 - k))
            lon = ((lon0 + rate * k) + 180.0) % 360.0 - 180.0
            pts.append({
                "epoch": t.astimezone(timezone.utc).isoformat(),
                "sub_lon_deg": round(lon, 4),
                "inclination_deg": round(inc, 4),
            })
        return pts

    @staticmethod
    def _track(oid, name, mode, origin, target, samples) -> dict:
        return {
            "name": name, "data_mode": mode, "classification_marking": "U",
            "source": "DEMO", "origin": origin, "target": target,
            "samples": samples,
        }
