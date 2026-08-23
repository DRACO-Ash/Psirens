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
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, TypeVar

import httpx

from sgp4.api import jday

from .astro import gmst_rad, sub_longitude_deg
from .config import Config
from .models import DataMode, ManualElsetIn
from .store import resilient_write
from .tle import validate_native

_log = logging.getLogger("psirens.sources")


def _norm_mode(raw: str | None) -> DataMode:
    try:
        return DataMode((raw or "REAL").upper())
    except ValueError:
        return DataMode.REAL


def _elset_dict(*, sat_no: str, epoch: datetime, inclination_deg: float,
                eccentricity: float, raan_deg: float, argp_deg: float,
                mean_anomaly_deg: float, mean_motion_rev_per_day: float,
                bstar: float, classification: str, intl_desig: str = "",
                source: str = "", line1: str = "", line2: str = "",
                rev_no: int | None = None, mean_motion_dot: float | None = None,
                mean_motion_ddot: float | None = None,
                ephem_type: int | None = None) -> dict:
    """Latest mean elements retained per object so conjunctions and TLE export
    can be computed later. This is the only place the full element set survives
    into the store; the plot itself needs only sub-longitude and inclination.

    `line1`/`line2` carry the provider's own native TLE where UDL supplied one
    and it passed validation. They are stored verbatim: the whole point of
    serving native lines is that nothing between the provider and the operator
    reformats them. `source` is retained because it decides selection order
    when several providers hold a fix for the same object.
    """
    return {
        "sat_no": str(sat_no),
        "epoch": epoch.astimezone(timezone.utc).isoformat(),
        "inclination_deg": inclination_deg, "eccentricity": eccentricity,
        "raan_deg": raan_deg, "argp_deg": argp_deg,
        "mean_anomaly_deg": mean_anomaly_deg,
        "mean_motion_rev_per_day": mean_motion_rev_per_day, "bstar": bstar,
        "classification": classification, "intl_desig": intl_desig,
        "source": source, "line1": line1, "line2": line2,
        "rev_no": rev_no, "mean_motion_dot": mean_motion_dot,
        "mean_motion_ddot": mean_motion_ddot, "ephem_type": ephem_type,
    }


_N = TypeVar("_N", int, float)


def _opt_num(row: dict, key: str, cast: Callable[[object], _N]) -> _N | None:
    """Read an optional numeric wire field, or None when absent or unusable.

    Absent stays absent: a missing rev number is not a rev number of zero, and
    writing one in would make a reconstructed line look like a provider fix.
    """
    val = row.get(key)
    if val is None or val == "":
        return None
    try:
        return cast(val)
    except (TypeError, ValueError):
        return None


def _record_elset(rec: dict, el: dict) -> None:
    """File one element set against a record, per source and overall.

    `elset` stays the newest fix regardless of provider: it drives the plot
    head, the drift rate and the RA needle, and none of those should change
    because a selection policy changed. `elset_candidates` keeps the newest
    fix per provider, which is what the TLE selection policy chooses between.
    """
    prev = rec.get("elset")
    if prev is None or el["epoch"] >= prev["epoch"]:
        rec["elset"] = el
    key = el.get("source") or "UNKNOWN"
    bucket = rec.setdefault("elset_candidates", {})
    held = bucket.get(key)
    if held is None or el["epoch"] >= held["epoch"]:
        bucket[key] = el


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
    def fetch(self, start: datetime, end: datetime, /) -> dict[str, dict]:
        """Return object_id -> record with meta and a `samples` list."""
        ...


# --------------------------------------------------------------------------
# UDL
# --------------------------------------------------------------------------
def _udl_ts(dt: datetime) -> str:
    """Format an instant the way the UDL epoch range filter expects: microsecond
    precision with a trailing Z. Python's isoformat() emits a '+00:00' offset,
    which the tenant accepts (200) but matches against nothing, yielding an empty
    result. Verified against tenant data: the Z form returns records.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class UDLElsetSource:
    """Pulls element sets from UDL.

    Verified against tenant /udl/elset responses: field names (satNo, epoch,
    inclination, eccentricity, raan, argOfPerigee, meanAnomaly, meanMotion,
    bStar, dataMode, classificationMarking, origObjectId, source) map directly,
    and the epoch range filter requires the trailing-Z form (see _udl_ts).
    Still TBC: which labelled field carries the intended target (UDL_TARGET_FIELD
    default 'tags'); this tenant's records carry no such field, so target links
    stay empty until confirmed.
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
            self.cfg.udl_epoch_param: f"{_udl_ts(start)}..{_udl_ts(end)}"
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
                els = {
                    "inclination_deg": float(row["inclination"]),
                    "eccentricity": float(row.get("eccentricity", 0.0)),
                    "raan_deg": float(row.get("raan", 0.0)),
                    "argp_deg": float(row.get("argOfPerigee", 0.0)),
                    "mean_anomaly_deg": float(row.get("meanAnomaly", 0.0)),
                    "mean_motion_rev_per_day": float(row["meanMotion"]),
                    "bstar": float(row.get("bStar", 0.0)),
                }
                sample = _sample_from_elements(epoch=epoch, **els)
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
            _record_elset(rec, self._elset_from_row(oid, row, epoch, els))
        return out

    @staticmethod
    def _elset_from_row(oid: str, row: dict, epoch: datetime, els: dict) -> dict:
        """Build the retained element set for one UDL row, native lines included.

        A native line is captured only if it validates against this record's
        own catalogue number (see `tle.validate_native`); a line that fails is
        dropped here rather than stored, so nothing downstream has to decide
        whether a stored line can be trusted.
        """
        native = validate_native(row.get("line1"), row.get("line2"), oid)
        line1, line2 = native if native else ("", "")
        return _elset_dict(
            sat_no=oid, epoch=epoch,
            classification=str(row.get("classificationMarking", "U")),
            intl_desig=str(row.get("origObjectId", "") or ""),
            source=str(row.get("source", "") or ""),
            line1=line1, line2=line2,
            rev_no=_opt_num(row, "revNo", int),
            mean_motion_dot=_opt_num(row, "meanMotionDot", float),
            mean_motion_ddot=_opt_num(row, "meanMotionDDot", float),
            ephem_type=_opt_num(row, "ephemType", int),
            **els)


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
        resilient_write(self.data_dir, self.path, json.dumps(items))

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

    def fetch(self, _start: datetime, _end: datetime) -> dict[str, dict]:
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
                "elset": _elset_dict(
                    sat_no=i["object_id"], epoch=epoch,
                    inclination_deg=i["inclination_deg"], eccentricity=i["eccentricity"],
                    raan_deg=i["raan_deg"], argp_deg=i["argp_deg"],
                    mean_anomaly_deg=i["mean_anomaly_deg"],
                    mean_motion_rev_per_day=i["mean_motion_rev_per_day"],
                    bstar=i.get("bstar", 0.0),
                    classification=i["classification_marking"],
                    source=str(i.get("source", "MANUAL") or "MANUAL"),
                    intl_desig=str(i["object_id"])),
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

    def fetch(self, _start: datetime, end: datetime) -> dict[str, dict]:
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
        for oid, rec in out.items():  # synthesise a GEO elset so offline works
            head = rec["samples"][-1]
            rec["elset"] = self._demo_elset(
                oid, head["sub_lon_deg"], head["inclination_deg"],
                datetime.fromisoformat(head["epoch"]))
        return out

    @staticmethod
    def _demo_elset(oid: str, lon: float, inc: float, epoch: datetime) -> dict:
        """Approximate mean elements placing a circular GEO object near `lon`:
        with raan=argp=0 and e~0, sub-longitude ~= mean anomaly minus GMST."""
        jd, fr = jday(epoch.year, epoch.month, epoch.day, epoch.hour,
                      epoch.minute, epoch.second + epoch.microsecond / 1e6)
        gmst_deg = math.degrees(gmst_rad(jd + fr))
        mean_anom = (lon + gmst_deg) % 360.0
        return _elset_dict(
            sat_no=oid, epoch=epoch, inclination_deg=inc, eccentricity=0.0002,
            raan_deg=0.0, argp_deg=0.0, mean_anomaly_deg=mean_anom,
            mean_motion_rev_per_day=DemoElsetSource._GEO_MM, bstar=0.0,
            classification="U", intl_desig="", source="DEMO")

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
