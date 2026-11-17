"""Element-set sources and normalisation into store-ready records.

Three sources implement the same shape:
  * UDLElsetSource   - pulls /udl/elset (all wire details TBC, env-overridable);
  * ManualElsetSource- operator-supplied temporary elsets from the store;
  * DemoElsetSource  - deterministic synthetic belt for the offline/--demo path.

Every raw elset is normalised, its sub-satellite longitude computed via the
validated astro module, and emitted as a sample keyed by (object_id, provider,
epoch).

PROVENANCE AND NOTHING FILLED IN (1.7.4). Owner rule, 17 November 2026: "The
UDL is the primary source of information ... We are not in the business of
making this up." Every sample now carries the provider, data mode and
classification marking of the ONE record it was calculated from. A row that
lacks any element a position needs, its provider, its marking or a recognised
data mode is REJECTED and counted by reason (`FetchStats.rejected`), never
completed with a default. Until 1.7.4 a missing mean anomaly, perigee, RAAN or
eccentricity became 0.0, a missing mode became REAL and a missing marking
became "U"; `audits/2026-11-17-tle-provenance/` reproduces each.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, TypeVar

import httpx

from sgp4.api import jday

from .astro import SIDEREAL_DAY_DAYS, gmst_rad, sub_longitude_deg
from .config import Config
from .models import DataMode, ManualElsetIn
from .store import resilient_write
from .tle import ElsetProvenance, validate_native

_log = logging.getLogger("psirens.sources")


def _elset_dict(*, sat_no: str, epoch: datetime, inclination_deg: float,
                eccentricity: float, raan_deg: float, argp_deg: float,
                mean_anomaly_deg: float, mean_motion_rev_per_day: float,
                bstar: float | None, classification: str, intl_desig: str = "",
                provenance: ElsetProvenance = ElsetProvenance()) -> dict:
    """Latest mean elements retained per object so conjunctions and TLE export
    can be computed later. This is the only place the full element set survives
    into the store; the plot itself needs only sub-longitude and inclination.

    `provenance` carries the provider's own native `line1`/`line2` where UDL
    supplied a pair and it passed validation. Those are stored verbatim: the
    whole point of serving native lines is that nothing between the provider
    and the operator reformats them. The source is retained with them because
    it decides selection order when several providers hold a fix for the same
    object. The fields are grouped rather than passed loose so this signature
    stays inside the SonarQube parameter cap (S107).
    """
    return {
        "sat_no": str(sat_no),
        "epoch": epoch.astimezone(timezone.utc).isoformat(),
        "inclination_deg": inclination_deg, "eccentricity": eccentricity,
        "raan_deg": raan_deg, "argp_deg": argp_deg,
        "mean_anomaly_deg": mean_anomaly_deg,
        "mean_motion_rev_per_day": mean_motion_rev_per_day, "bstar": bstar,
        "classification": classification, "intl_desig": intl_desig,
        "source": provenance.source,
        "line1": provenance.line1, "line2": provenance.line2,
        "rev_no": provenance.rev_no,
        "mean_motion_dot": provenance.mean_motion_dot,
        "mean_motion_ddot": provenance.mean_motion_ddot,
        "ephem_type": provenance.ephem_type,
        "data_mode": provenance.data_mode or None,
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


def candidate_key(el: dict) -> str:
    """Candidates are kept per provider AND mode, so a provider's newer
    EXERCISE set can never displace its REAL one under the same number."""
    source = el.get("source") or "UNKNOWN"
    mode = el.get("data_mode")
    return source if mode in (None, "", "REAL") else f"{source}|{mode}"


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
    key = candidate_key(el)
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
        # RAAN is retained PER SAMPLE from 1.6.17. Inclination alone cannot
        # define a plane, so without this the co-planar chart can only ever
        # draw one point per object: the newest elset. `coplanar.py` has
        # carried a points list since 1.6.0 waiting for exactly this field.
        "raan_deg": round(raan_deg, 4),
    }


# The elements a position needs, wire name -> stored name. Every one is
# required: a row missing any of them is rejected, never zero-filled.
_ROW_ELEMENTS = (
    ("inclination", "inclination_deg"), ("eccentricity", "eccentricity"),
    ("raan", "raan_deg"), ("argOfPerigee", "argp_deg"),
    ("meanAnomaly", "mean_anomaly_deg"), ("meanMotion", "mean_motion_rev_per_day"),
)
TAG_KEYS = ("source", "data_mode", "classification_marking")


def _row_elements(row: dict) -> dict | None:
    out: dict = {}
    for wire, key in _ROW_ELEMENTS:
        val = row.get(wire)
        if val is None or val == "":
            return None
        try:
            out[key] = float(val)
        except (TypeError, ValueError):
            return None
    return out


def _row_tags(row: dict) -> dict | str:
    """The provenance a sample carries, or the reason the row is rejected."""
    source = str(row.get("source") or "").strip()
    if not source:
        return "no provider (source)"
    marking = str(row.get("classificationMarking") or "").strip()
    if not marking:
        return "no classification marking"
    try:
        mode = DataMode(str(row.get("dataMode") or "").strip().upper())
    except ValueError:
        return "no recognised data mode"
    return {"source": source, "data_mode": mode.value, "classification_marking": marking}


def _row_epoch(row: dict) -> datetime | None:
    try:
        epoch = datetime.fromisoformat(str(row["epoch"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return epoch if epoch.tzinfo is not None else None


def parse_row(row: dict) -> tuple[str, datetime, dict, dict, dict] | str:
    """(object id, epoch, elements, tags, sample) for one UDL row, or the
    reason it is rejected. Nothing is defaulted."""
    oid = str(row.get("satNo") or row.get("idOnOrbit") or row.get("id") or "")
    if not oid:
        return "no catalogue number"
    epoch = _row_epoch(row)
    if epoch is None:
        return "no usable epoch with a time zone"
    els = _row_elements(row)
    if els is None:
        return "incomplete elements"
    tags = _row_tags(row)
    if isinstance(tags, str):
        return tags
    # B* is optional on the wire and stored as absent when absent. It plays no
    # part in a position AT the element set's own epoch (SGP4 drag terms scale
    # with time since epoch, which is zero), so 0.0 here changes nothing; the
    # stored element set keeps None, and propagation away from the epoch is
    # refused without it (`tle.propagation_refusal`).
    sample = _sample_from_elements(epoch=epoch, bstar=0.0, **els)
    if sample is None:
        return "SGP4 failed at the element set's own epoch"
    sample.update(tags)
    return oid, epoch, els, tags, sample


def sample_key(s: dict) -> tuple[str, str]:
    """Samples are distinct per PROVIDER and epoch: two providers' fixes at
    the same instant are two observations, not one to be merged."""
    return (str(s.get("source") or ""), str(s["epoch"]))


_MIN_SLICE = timedelta(minutes=30)   # below this, subdividing stops helping
_MAX_SPLIT_DEPTH = 6                 # 24h down to ~22 minutes


def _slices(start: datetime, end: datetime,
            slice_hours: int) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into windows of at most `slice_hours`.

    Slices are half-open going forward but the endpoints overlap by nothing:
    duplicate records across a boundary are harmless because both the sample
    merge and the element-set retention key on epoch.
    """
    if end <= start:
        return []
    span = timedelta(hours=max(1, slice_hours))
    out, cursor = [], start
    while cursor < end:
        nxt = min(cursor + span, end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


def _merge_records(acc: dict[str, dict], incoming: dict[str, dict]) -> None:
    """Fold one slice's records into the accumulator, deduplicating samples by
    (provider, epoch) and keeping the newest element set per provider."""
    for oid, rec in incoming.items():
        held = acc.get(oid)
        if held is None:
            acc[oid] = rec
            continue
        seen = {sample_key(s) for s in held["samples"]}
        held["samples"].extend(s for s in rec["samples"] if sample_key(s) not in seen)
        held["samples"].sort(key=lambda s: s["epoch"])
        for el in (rec.get("elset_candidates") or {}).values():
            _record_elset(held, el)


@dataclass
class FetchStats:
    """What the WIRE did, as opposed to what the merge did.

    1.6.6 gave the app an alarm for stale data but built it on the number of
    samples the merge ADDED. That number is zero both when the upstream
    returns nothing and when every upstream request fails, and `_request`
    deliberately swallows a failure so one bad slice never kills a cycle. The
    consequence, seen live on 26 September 2026: `last_ingest_added: 0` beside
    `last_ingest_errors: []`, which reads as "the feed is healthy and simply
    had nothing new" when it may equally mean "every request was rejected".
    A source now records what it actually did so the two are distinguishable.
    """

    requests: int = 0
    failures: int = 0
    rows: int = 0          # raw records returned, before normalisation
    objects: int = 0       # distinct objects after normalisation
    reasons: list[str] = field(default_factory=list)
    # Rows refused at the boundary, by reason (1.7.4): incomplete elements, no
    # provider, no marking, no recognised data mode. Counted, never filled in.
    rejected: dict[str, int] = field(default_factory=dict)

    def note_failure(self, reason: str) -> None:
        self.failures += 1
        if reason not in self.reasons:
            self.reasons.append(reason)

    def note_rejected(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def summary(self) -> str:
        detail = "; ".join(self.reasons[:3]) or "no reason recorded"
        return (f"{self.failures}/{self.requests} upstream request(s) failed: "
                f"{detail}")


def _failure_reason(exc: Exception) -> str:
    """A short, loggable cause. The status code is the single most useful
    fact when a feed goes quiet: 401 is credentials, 403 is entitlement, 5xx
    is the tenant, a timeout is the network."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, json.JSONDecodeError):
        return "non-JSON response body"
    return type(exc).__name__


class ElsetSource(Protocol):
    last_stats: FetchStats

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
        self.last_stats = FetchStats()

    def _auth_header(self) -> dict[str, str]:
        raw = f"{self.cfg.udl_user}:{self.cfg.udl_password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def fetch(self, start: datetime, end: datetime) -> dict[str, dict]:
        """Pull the window in slices and merge the results.

        LEARNED (CONTEXT-001): the tenant hard-caps results server-side and the
        documented remedy is to SLICE BY TIME WINDOW, never to offset-paginate.
        Until 1.6.6 this asked for the whole window in one request, which for
        the scheduled refresh meant 90 days of every object in the catalogue.
        The response came back truncated, the newest fixes were the ones
        missing, and the plot sat a constant fortnight behind while every other
        signal looked healthy.
        """
        client = self._client or httpx.Client(timeout=30.0)
        merged: dict[str, dict] = {}
        self.last_stats = FetchStats()  # per fetch, never cumulative
        try:
            windows = _slices(start, end, self.cfg.udl_slice_hours)
            for index, (w_start, w_end) in enumerate(windows):
                if index > 0 and self.cfg.udl_slice_pause_seconds > 0:
                    time.sleep(self.cfg.udl_slice_pause_seconds)
                _merge_records(merged, self._fetch_slice(client, w_start, w_end))
        finally:
            if self._client is None:
                client.close()
        self.last_stats.objects = len(merged)
        if self.last_stats.failures:
            _log.warning("UDL fetch for %s..%s: %s", _udl_ts(start),
                         _udl_ts(end), self.last_stats.summary())
        return merged

    def _request(self, client: httpx.Client, start: datetime,
                 end: datetime) -> list[dict] | None:
        """One request. None means the request failed, which is never fatal;
        an empty list means it succeeded and matched nothing."""
        headers = {"Accept": self.cfg.udl_accept, **self._auth_header()}
        url = self.cfg.udl_base_url.rstrip("/") + self.cfg.udl_elset_path
        params = {self.cfg.udl_epoch_param: f"{_udl_ts(start)}..{_udl_ts(end)}"}
        if self.cfg.udl_max_results > 0:
            params["maxResults"] = str(self.cfg.udl_max_results)
        self.last_stats.requests += 1
        try:
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            # Never fatal to the cycle, but never silent either: a swallowed
            # failure that reported no error is what made a dead feed look
            # like a quiet one.
            self.last_stats.note_failure(_failure_reason(exc))
            _log.warning("UDL elset fetch failed for %s..%s (never fatal): %s",
                         _udl_ts(start), _udl_ts(end), exc)
            return None
        out = rows if isinstance(rows, list) else []
        self.last_stats.rows += len(out)
        return out

    def _fetch_slice(self, client: httpx.Client, start: datetime,
                     end: datetime, depth: int = 0) -> dict[str, dict]:
        """One slice, subdivided if the response looks truncated.

        A truncated page and a complete one are indistinguishable from the
        outside, so treat a suspiciously full response as truncated and halve
        the window. This is the check that would have caught the original
        defect on the day it started.
        """
        rows = self._request(client, start, end)
        if rows is None:
            return {}
        if len(rows) >= self.cfg.udl_truncation_threshold and depth < _MAX_SPLIT_DEPTH:
            span = (end - start) / 2
            if span >= _MIN_SLICE:
                _log.warning(
                    "UDL returned %d rows for %s..%s, at or above the "
                    "truncation threshold of %d; halving the window. Newest "
                    "records are the ones a truncated page drops.",
                    len(rows), _udl_ts(start), _udl_ts(end),
                    self.cfg.udl_truncation_threshold)
                out = self._fetch_slice(client, start, start + span, depth + 1)
                _merge_records(out, self._fetch_slice(client, start + span, end,
                                                      depth + 1))
                return out
            _log.warning(
                "UDL returned %d rows for a %s window that cannot be split "
                "further; the result is probably TRUNCATED and newer fixes "
                "may be missing.", len(rows), end - start)
        return self._normalise(rows)

    def _target_of(self, row: dict) -> str | None:
        val = row.get(self.cfg.udl_target_field)
        if isinstance(val, list) and val:
            return str(val[0])
        return str(val) if val not in (None, "") else None

    def _normalise(self, rows: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in rows:
            parsed = parse_row(row)
            if isinstance(parsed, str):
                self.last_stats.note_rejected(parsed)
                continue
            oid, epoch, els, tags, sample = parsed
            # Record-level fields are what the FIRST row of this response
            # said. They are kept for older readers only; every consumer of
            # provenance reads the sample's own tags.
            rec = out.setdefault(oid, {
                "name": str(row.get("origObjectId") or row.get("satNo") or oid),
                **tags,
                "origin": "UDL",
                "target": self._target_of(row),
                "samples": [],
            })
            rec["samples"].append(sample)
            _record_elset(rec, self._elset_from_row(oid, row, epoch, els, tags))
        return out

    @staticmethod
    def _elset_from_row(oid: str, row: dict, epoch: datetime, els: dict,
                        tags: dict) -> dict:
        """Build the retained element set for one UDL row, native lines included.

        A native line is captured only if it validates against this record's
        own catalogue number (see `tle.validate_native`); a line that fails is
        dropped here rather than stored, so nothing downstream has to decide
        whether a stored line can be trusted.
        """
        native = validate_native(row.get("line1"), row.get("line2"), oid)
        line1, line2 = native if native else ("", "")
        return _elset_dict(
            sat_no=oid, epoch=epoch, bstar=_opt_num(row, "bStar", float),
            classification=tags["classification_marking"],
            intl_desig=str(row.get("origObjectId", "") or ""),
            provenance=ElsetProvenance(
                source=tags["source"],
                line1=line1, line2=line2,
                rev_no=_opt_num(row, "revNo", int),
                mean_motion_dot=_opt_num(row, "meanMotionDot", float),
                mean_motion_ddot=_opt_num(row, "meanMotionDDot", float),
                ephem_type=_opt_num(row, "ephemType", int),
                data_mode=tags["data_mode"]),
            **els)


# --------------------------------------------------------------------------
# Manual (temporary, operator-supplied)
# --------------------------------------------------------------------------
MANUAL_PROVIDER = "MANUAL"
DEMO_PROVIDER = "DEMO"
# Demo objects are named so that no identifier can ever be a catalogue number.
# Until 1.7.4 four of them reused REAL high-interest catalogue numbers
# (41836, 28924, 43683, 41748) with synthetic positions labelled REAL.
DEMO_ID_PREFIX = "DEMO-"


class ManualElsetSource:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "manual.json")
        # A local source makes no requests, so failures stay at zero; the
        # object count still matters, because it separates "nothing was
        # offered" from "something was offered and then filtered away".
        self.last_stats = FetchStats()

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
            rec = self._record(i)
            if rec is not None:
                out[i["object_id"]] = rec
        self.last_stats = FetchStats(objects=len(out))
        return out

    @staticmethod
    def _record(i: dict) -> dict | None:
        """One operator-supplied element set as a store record.

        The PROVIDER is always MANUAL, whatever the operator typed as the
        source: an operator is not SAFRAN or 18 SDS, and a manual set filed
        under a provider's name would have competed in TLE selection as that
        provider. The operator's own words are kept as `stated_source`. An
        entry missing a marking, a data mode or B* (possible only in a
        manual.json written before 1.7.4, when all three had defaults) is
        skipped rather than completed.
        """
        if not all(i.get(k) not in (None, "") for k in
                   ("classification_marking", "data_mode", "bstar")):
            return None
        epoch = datetime.fromisoformat(i["epoch"])
        els = {k: i[k] for k in ("inclination_deg", "eccentricity", "raan_deg",
                                 "argp_deg", "mean_anomaly_deg",
                                 "mean_motion_rev_per_day")}
        sample = _sample_from_elements(epoch=epoch, bstar=float(i["bstar"]), **els)
        if sample is None:
            return None
        tags = {"source": MANUAL_PROVIDER, "data_mode": i["data_mode"],
                "classification_marking": i["classification_marking"]}
        sample.update(tags)
        el = _elset_dict(sat_no=i["object_id"], epoch=epoch, bstar=float(i["bstar"]),
                         classification=i["classification_marking"],
                         provenance=ElsetProvenance(source=MANUAL_PROVIDER,
                                                    data_mode=i["data_mode"]),
                         intl_desig=str(i["object_id"]), **els)
        el["stated_source"] = str(i.get("source") or "")
        return {"name": i["name"], **tags, "origin": "MANUAL",
                "target": i.get("target"), "samples": [sample], "elset": el}


# --------------------------------------------------------------------------
# Demo (deterministic, offline; the sandbox cannot reach UDL)
# --------------------------------------------------------------------------
class DemoElsetSource:
    """Synthetic GEO belt: station-kept anchors, longitude drifters, an
    inclined-orbit object, and an EXERCISE inspector that closes on a target.
    Deterministic given `now` so tests and CI are stable and offline-safe.

    SYNTHETIC, AND KEPT AWAY FROM A LIVE STORE (1.7.4). It is never built when
    UDL is enabled, whatever DEMO_MODE says (`main._build_sources`); every id
    starts with DEMO- so none can be a catalogue number; every sample is tagged
    provider DEMO; and a live start purges anything DEMO-tagged from the store
    (`Store.purge_synthetic`).
    """

    _GEO_MM = 1.0027379093  # sidereal rev/day

    def __init__(self) -> None:
        self.last_stats = FetchStats()

    def fetch(self, _start: datetime, end: datetime) -> dict[str, dict]:
        now = end
        out: dict[str, dict] = {}

        # 1) Station-kept anchors (REAL). Each carries a 30-day FLAT trail and
        # its own RAAN from 1.6.17. A station-kept object still has a history,
        # and the co-planar trail is built by pairing a neighbour's samples
        # against the PRIMARY's, so an anchor holding a single point would cap
        # every neighbour's trail at one point offline and leave the new path
        # unexercised by the probe.
        anchors = [
            ("DEMO-A1", "ANCHOR-W67", -67.0, 0.02, "REAL", 15.0),
            ("DEMO-A2", "ANCHOR-E174", 174.0, 0.05, "REAL", 95.0),
            ("DEMO-A3", "ANCHOR-E140", 140.0, 1.9, "REAL", 210.0),
            ("DEMO-A4", "ANCHOR-E105", 105.0, 3.6, "REAL", 300.0),
        ]
        for oid, name, lon, inc, mode, raan in anchors:
            out[oid] = self._track(oid, name, mode, "DEMO", None,
                                   self._span(now, 30, lon, inc, 0.0, raan))

        # 2) Longitude drifter (REAL), moving east ~0.9 deg/day over 40 days.
        out["DEMO-D1"] = self._track(
            "DEMO-D1", "DRIFTER-1", "REAL", "DEMO", None,
            self._span(now, 40, 120.0, 3.1, 0.9, 128.0),
        )
        # 3) Relocating satellite (SIMULATED), west then station-keeping.
        out["DEMO-S1"] = self._track(
            "DEMO-S1", "RELOCATE-SIM", "SIMULATED", "DEMO", None,
            self._span(now, 30, 90.0, 0.4, -1.4, 44.0),
        )
        # 4) Inclined-orbit object climbing in inclination (TEST).
        incl = self._span(now, 25, 62.0, 0.5, 0.0, 262.0)
        for k, s in enumerate(incl):
            s["inclination_deg"] = round(0.5 + 0.11 * k, 4)
        out["DEMO-T1"] = self._track("DEMO-T1", "INCLINED-TEST", "TEST", "DEMO", None, incl)

        # 5) EXERCISE inspector closing on anchor DEMO-A3 (its labelled target).
        out["DEMO-X1"] = self._track(
            "DEMO-X1", "INSPECTOR-EX", "EXERCISE", "DEMO", "DEMO-A3",
            self._span(now, 20, 133.0, 1.5, 0.35, 208.0),
        )
        # 6) A co-located cluster around anchor DEMO-A3 at 140E. Real neighbourhoods
        # in the belt are crowded, and the offline belt was not: with two
        # neighbours the co-planar chart could never exercise label collision,
        # so the guard for it would have passed on a chart that had nothing to
        # collide. Same reasoning as the 30-day anchor trails above.
        cluster = [
            ("DEMO-C1", "CLUSTER-ALPHA", 138.5, 0.3, 203.0, 0.05),
            ("DEMO-C2", "CLUSTER-BRAVO", 141.2, 2.1, 212.0, -0.08),
            ("DEMO-C3", "CLUSTER-CHARLIE", 142.6, 1.2, 188.0, 0.12),
            ("DEMO-C4", "CLUSTER-DELTA", 136.9, 3.0, 249.0, -0.03),
        ]
        for oid, name, lon, inc, raan, rate in cluster:
            out[oid] = self._track(oid, name, "REAL", "DEMO", None,
                                   self._span(now, 15, lon, inc, rate, raan))

        # 7) Two DENSELY sampled objects for the Shift Brief (1.7.0). Every
        # other demo object has one fix a day, which no event detector can
        # read, so without these the offline belt has no events and the probe
        # guarding the brief, the timeline and focus mode would pass on a page
        # that had nothing to show. One burns westward towards DEMO-A3;
        # the other stops reporting while the rest of the belt carries on.
        out["DEMO-M1"] = self._track("DEMO-M1", "MANOEUVRE-DEMO", "REAL", "DEMO", None,
                                   self._dense(now, lon0=150.4, inc=0.8, raan=230.0,
                                               burn_h=40.0, rate=-0.5))
        out["DEMO-Q1"] = self._track("DEMO-Q1", "SILENT-DEMO", "REAL", "DEMO", None,
                                   self._dense(now, lon0=-20.0, inc=0.3, raan=80.0,
                                               stop_h=16.0))

        for oid, rec in out.items():  # synthesise a GEO elset so offline works
            head = rec["samples"][-1]
            rec["elset"] = self._demo_elset(
                oid, head["sub_lon_deg"], head["inclination_deg"],
                datetime.fromisoformat(head["epoch"]), head["raan_deg"])
            rec["elset"]["data_mode"] = rec["data_mode"]
        self.last_stats = FetchStats(objects=len(out))
        return out

    @staticmethod
    def _demo_elset(oid: str, lon: float, inc: float, epoch: datetime,
                    raan: float = 0.0) -> dict:
        """Approximate mean elements placing a circular GEO object near `lon`:
        with argp=0 and e~0, right ascension ~= RAAN + mean anomaly, so the
        sub-longitude is RAAN + mean anomaly - GMST. The RAAN is carried in
        rather than fixed at zero so the elset and the newest SAMPLE describe
        the SAME plane; a zero here would put every demo object in the
        equatorial node and make the elset-derived head of the co-planar chart
        contradict the sample-derived trail beneath it."""
        jd, fr = jday(epoch.year, epoch.month, epoch.day, epoch.hour,
                      epoch.minute, epoch.second + epoch.microsecond / 1e6)
        gmst_deg = math.degrees(gmst_rad(jd + fr))
        mean_anom = (lon + gmst_deg - raan) % 360.0
        return _elset_dict(
            sat_no=oid, epoch=epoch, inclination_deg=inc, eccentricity=0.0002,
            raan_deg=raan, argp_deg=0.0, mean_anomaly_deg=mean_anom,
            mean_motion_rev_per_day=DemoElsetSource._GEO_MM, bstar=0.0,
            classification="U", intl_desig="",
            provenance=ElsetProvenance(source=DEMO_PROVIDER, ephem_type=0))

    def _span(self, now: datetime, days: int, lon0: float, inc: float,
              rate: float, raan: float = 0.0) -> list[dict]:
        """A synthetic trail. Each point carries RAAN from 1.6.17, as a real
        sample now does, so the offline demo exercises the co-planar trail
        instead of silently taking the no-history path that a store full of
        pre-1.6.17 samples takes. The node regresses at the GEO rate recorded
        in CONTEXT-001 (-0.0134 deg/day), which is small but not zero."""
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
                "raan_deg": round((raan - 0.0134 * (n - 1 - k)) % 360.0, 4),
            })
        return pts

    @staticmethod
    def _dense(now: datetime, *, lon0: float, inc: float, raan: float,
               burn_h: float | None = None, rate: float = 0.0,
               stop_h: float = 0.0) -> list[dict]:
        """Six days of fixes every two hours, with a small sidereal libration,
        optionally a burn `burn_h` hours before the newest fix (a constant
        drift `rate` after it) and optionally no fixes in the last `stop_h`
        hours. Quantised to the hour, so repeated refreshes dedup cleanly."""
        base = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        pts = []
        for k in range(72):
            hours_back = 2.0 * (71 - k)
            if hours_back < stop_h:
                continue
            t = base - timedelta(hours=hours_back)
            days = (144.0 - hours_back) / 24.0
            lon = lon0 + 0.08 * math.sin(2.0 * math.pi * days / SIDEREAL_DAY_DAYS)
            if burn_h is not None and hours_back <= burn_h:
                lon += rate * (burn_h - hours_back) / 24.0
            pts.append({"epoch": t.isoformat(),
                        "sub_lon_deg": round((lon + 180.0) % 360.0 - 180.0, 4),
                        "inclination_deg": round(inc, 4),
                        "raan_deg": round(raan, 4)})
        return pts

    @staticmethod
    def _track(oid, name, mode, origin, target, samples) -> dict:
        tags = {"source": DEMO_PROVIDER, "data_mode": mode,
                "classification_marking": "U"}
        for s in samples:
            s.update(tags)
        return {"name": name, **tags, "origin": origin, "target": target,
                "samples": samples}
