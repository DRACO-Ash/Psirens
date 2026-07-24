#!/usr/bin/env python3
"""PSIRENS project scaffold.

Run `python scaffold.py` in an empty directory. It reconstructs the full source
tree (text files need no dependencies). If `Psirens.png` is present in the same
directory and Pillow is installed, it also regenerates the icon assets.

Verify the reconstruction:
    python -m venv .venv && . .venv/bin/activate   (Windows: .venv\\Scripts\\activate)
    pip install -r requirements.txt -r requirements-dev.txt
    python -m pytest -q        # expect 41 passed, coverage ~93%
"""
import os

MARKER = "# ===DATA BELOW==="


def main():
    raw = open(__file__, encoding="utf-8").read()
    data = raw.split(MARKER + "\n", 1)[1]
    files, path, buf = {}, None, []
    for line in data.splitlines(keepends=True):
        if line.startswith("===FILE=== "):
            if path is not None:
                files[path] = "".join(buf)
            path, buf = line[len("===FILE=== "):].strip(), []
        else:
            buf.append(line)
    if path is not None:
        files[path] = "".join(buf)
    for p, body in files.items():
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        print("wrote", p)
    make_icons()
    print("\nDone. Next: create a venv, install requirements, run pytest to verify.")


def make_icons():
    src = "Psirens.png"
    if not os.path.exists(src):
        print("NOTE: Psirens.png not found; skipping icons. "
              "Put the original artwork here and re-run to generate them.")
        return
    try:
        from PIL import Image
    except ImportError:
        print("NOTE: Pillow not installed; skipping icons. "
              "`pip install pillow` then re-run to generate them.")
        return
    im = Image.open(src).convert("RGBA")
    side, cx, cy = 600, 706, 392
    box = (cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2)
    icon = im.crop(box)
    out = "src/psirens/static"
    os.makedirs(out, exist_ok=True)
    icon.resize((512, 512), Image.LANCZOS).save(f"{out}/icon-512.png")
    icon.resize((192, 192), Image.LANCZOS).save(f"{out}/icon-192.png")
    icon.resize((32, 32), Image.LANCZOS).save(f"{out}/favicon-32.png")
    icon.resize((180, 180), Image.LANCZOS).save(f"{out}/apple-touch-icon.png")
    im.convert("RGB").save(f"{out}/psirens-banner.png")
    print("wrote icon-512/192/32, apple-touch-icon, psirens-banner")


if __name__ == "__main__":
    main()

# ===DATA BELOW===
===FILE=== src/psirens/__init__.py
"""PSIRENS - Plotted Satellite Inclination and Raan ENgagement Screening."""
===FILE=== src/psirens/models.py
"""Domain models for PSIRENS.

Every record that enters the store is validated here at the boundary and
rejected on failure; nothing is coerced into the store as junk.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1


# The four accepted UDL data modes (confirmed by owner, 20 July 2026).
class DataMode(str, enum.Enum):
    REAL = "REAL"
    SIMULATED = "SIMULATED"
    TEST = "TEST"
    EXERCISE = "EXERCISE"


# Which modes each hero tab shows by default.
VIEW_MODES: dict[str, list[DataMode]] = {
    "real": [DataMode.REAL],
    "combined": [DataMode.REAL, DataMode.SIMULATED, DataMode.TEST, DataMode.EXERCISE],
}


class ManualElsetIn(BaseModel):
    """A temporary, operator-supplied element set.

    Accepts classical mean Keplerian elements directly so no TLE-string
    construction (with its checksum and exponent traps) is required.
    """

    object_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    epoch: datetime
    inclination_deg: float = Field(ge=0.0, le=180.0)
    eccentricity: float = Field(ge=0.0, lt=1.0)
    raan_deg: float = Field(ge=0.0, le=360.0)
    argp_deg: float = Field(ge=0.0, le=360.0)
    mean_anomaly_deg: float = Field(ge=0.0, le=360.0)
    mean_motion_rev_per_day: float = Field(gt=0.0, le=20.0)
    bstar: float = 0.0
    data_mode: DataMode = DataMode.SIMULATED
    classification_marking: str = Field(default="U", max_length=64)
    source: str = Field(default="MANUAL", max_length=64)
    target: str | None = Field(default=None, max_length=120)
    ttl_hours: float = Field(default=24.0, gt=0.0, le=24.0 * 365.0)

    @field_validator("epoch")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class Sample(BaseModel):
    """One point on an object's track: its state at one elset epoch."""

    epoch: datetime
    sub_lon_deg: float
    inclination_deg: float


class Track(BaseModel):
    """An object and its ordered samples over the retention window."""

    object_id: str
    name: str
    data_mode: DataMode
    classification_marking: str
    source: str
    origin: str  # UDL | MANUAL | DEMO
    target: str | None = None
    samples: list[Sample]
    drift_deg_per_day: float | None = None  # longitude drift rate at the head
===FILE=== src/psirens/astro.py
"""Astrodynamics: sub-satellite longitude and drift rate from a mean elset.

Validated numerically (see tests/test_astro.py):
  * GMST against Vallado's 1992-08-20 worked example (152.5788 deg).
  * Sub-satellite longitude against derivable geostationary cases
    (angle-sum == GMST -> 0 deg; +90 deg -> +90 deg east) to < 0.01 deg.

We propagate with the standard Vallado SGP4 (the `sgp4` package) rather than a
hand-rolled propagator, deliberately: an in-house closed-form substitution is
exactly the class of defect that produced a large velocity error elsewhere in
the estate, and is not worth repeating for a plot. Recorded dependency reason.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sgp4.api import WGS72, Satrec
from sgp4.functions import jday

_log = logging.getLogger("psirens.astro")

_EPOCH_1949 = datetime(1949, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
_TWO_PI = 2.0 * math.pi


def gmst_rad(jd_ut1: float) -> float:
    """Greenwich Mean Sidereal Time in radians (IAU-82, Vallado).

    UT1 is approximated by UTC; the sub-degree UT1-UTC offset is negligible
    for a belt-knowledge plot and is not corrected here (stated, not hidden).
    """
    tut1 = (jd_ut1 - 2451545.0) / 36525.0
    gmst_sec = (
        67310.54841
        + (876600.0 * 3600.0 + 8640184.812866) * tut1
        + 0.093104 * tut1 * tut1
        - 6.2e-6 * tut1 * tut1 * tut1
    )
    gmst = math.radians((gmst_sec % 86400.0) / 240.0)  # 240 s of time == 1 deg
    return gmst % _TWO_PI


def _days_since_1949(epoch: datetime) -> float:
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    return (epoch - _EPOCH_1949).total_seconds() / 86400.0


def sub_longitude_deg(
    *,
    inclination_deg: float,
    eccentricity: float,
    raan_deg: float,
    argp_deg: float,
    mean_anomaly_deg: float,
    mean_motion_rev_per_day: float,
    bstar: float,
    epoch: datetime,
) -> float | None:
    """Sub-satellite longitude in degrees, range (-180, 180], or None on failure.

    Builds an SGP4 satellite record directly from mean elements, propagates to
    the elset epoch (tsince = 0), and rotates the TEME position into an
    Earth-fixed frame by GMST to read off the sub-point longitude.
    """
    if epoch.tzinfo is None:
        epoch = epoch.replace(tzinfo=timezone.utc)
    no_kozai = mean_motion_rev_per_day * _TWO_PI / 1440.0  # rad / minute
    sat = Satrec()
    try:
        sat.sgp4init(
            WGS72,
            "i",
            0,
            _days_since_1949(epoch),
            float(bstar),
            0.0,
            0.0,
            float(eccentricity),
            math.radians(argp_deg),
            math.radians(inclination_deg),
            math.radians(mean_anomaly_deg),
            no_kozai,
            math.radians(raan_deg),
        )
    except (ValueError, OverflowError) as exc:  # pragma: no cover - defensive
        _log.warning("sgp4init failed: %s", exc)
        return None

    jd, fr = jday(
        epoch.year,
        epoch.month,
        epoch.day,
        epoch.hour,
        epoch.minute,
        epoch.second + epoch.microsecond / 1_000_000.0,
    )
    err, r, _v = sat.sgp4(jd, fr)
    if err != 0:
        _log.debug("sgp4 propagation error code %s for epoch %s", err, epoch)
        return None

    theta = gmst_rad(jd + fr)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    x_ecef = r[0] * cos_t + r[1] * sin_t
    y_ecef = -r[0] * sin_t + r[1] * cos_t
    lon = math.degrees(math.atan2(y_ecef, x_ecef))
    return (lon + 180.0) % 360.0 - 180.0


def drift_deg_per_day(samples: list[tuple[datetime, float]]) -> float | None:
    """Longitude drift rate (deg/day) from the two most recent samples.

    Uses shortest angular difference so a wrap across the +/-180 seam does not
    register as a spurious ~360 deg/day jump. Returns None with < 2 samples.
    """
    if len(samples) < 2:
        return None
    (t0, lon0), (t1, lon1) = samples[-2], samples[-1]
    dt_days = (t1 - t0).total_seconds() / 86400.0
    if abs(dt_days) < 1e-9:
        return None
    dlon = (lon1 - lon0 + 180.0) % 360.0 - 180.0
    return dlon / dt_days
===FILE=== src/psirens/config.py
"""Configuration, read from the environment only (never a committed file).

Injected add-on values (the storage mount) are read at request/boot time, not
at import time, so an empty value is never captured before the platform injects
it. The storage path is resolved fail-closed but recoverable: a bad path is
reported clearly rather than silently writing to an ephemeral layer.

All UDL wire details are TBC and marked: the LEARNED register only verifies
/udl/eoobservation behaviour, so nothing about /udl/elset is assumed. Every
UDL knob is overridable by environment so it can be corrected against the
tenant without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _clean(value: str | None) -> str:
    """Strip surrounding quotes and control characters an operator console
    may smuggle into a pasted value (a trailing newline or tab has broken
    saves and token matches before)."""
    if value is None:
        return ""
    return value.strip().strip('"').strip("'").strip()


def _env(name: str, default: str = "") -> str:
    return _clean(os.environ.get(name, default))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Config:
    # Runtime contract
    port: int
    allowed_origin: str
    team_token: str

    # Data window and cadence
    retention_days: int
    refresh_seconds: int
    lon_min: float
    lon_max: float
    inc_min: float
    inc_max: float
    max_samples_per_object: int

    # Source selection
    demo_mode: bool
    udl_enabled: bool

    # UDL wire config (ALL TBC: verify against the tenant before live use)
    udl_base_url: str
    udl_elset_path: str
    udl_user: str
    udl_password: str = field(repr=False, default="")
    udl_target_field: str = "tags"  # TBC: which labelled field carries target
    udl_epoch_param: str = "epoch"
    udl_accept: str = "application/json"
    data_dir: str = ""  # explicit override (tests); empty means resolve from env

    def storage_dir(self) -> str:
        """Resolve at call time: explicit override/var, platform mount, default."""
        explicit = self.data_dir or _env("DATA_DIR")
        mount = _env("STORAGE_MOUNT_PATH")  # FILE_STORAGE add-on injects /data
        return explicit or mount or "/tmp/psirens-data"


def load_config() -> Config:
    port = int(_env("PORT", "8080") or "8080")
    return Config(
        port=port,
        allowed_origin=_env("ALLOWED_ORIGIN"),
        team_token=_env("TEAM_TOKEN"),
        retention_days=int(_env("RETENTION_DAYS", "90") or "90"),
        refresh_seconds=int(_env("REFRESH_SECONDS", "3600") or "3600"),
        lon_min=float(_env("LON_MIN", "-180") or "-180"),
        lon_max=float(_env("LON_MAX", "180") or "180"),
        inc_min=float(_env("INC_MIN", "0") or "0"),
        inc_max=float(_env("INC_MAX", "15") or "15"),
        max_samples_per_object=int(_env("MAX_SAMPLES", "2000") or "2000"),
        demo_mode=_env_bool("DEMO_MODE", default=not bool(_env("UDL_BASE_URL"))),
        udl_enabled=bool(_env("UDL_BASE_URL")),
        udl_base_url=_env("UDL_BASE_URL"),
        udl_elset_path=_env("UDL_ELSET_PATH", "/udl/elset"),
        udl_user=_env("UDL_USER"),
        udl_password=_env("UDL_PASSWORD"),
        udl_target_field=_env("UDL_TARGET_FIELD", "tags"),
        udl_epoch_param=_env("UDL_EPOCH_PARAM", "epoch"),
        udl_accept=_env("UDL_ACCEPT", "application/json"),
    )
===FILE=== src/psirens/store.py
"""Atomic JSON store on the file-storage add-on.

Contracts held here:
  * atomic writes (temp file then rename on the same filesystem);
  * anti-shrink merge (a refresh never deletes an object or its history that
    the new pull happened not to include);
  * dedup by (object_id, epoch) so overlapping pulls do not double-count;
  * age prune to the retention window and a per-object sample cap (newest kept);
  * a schema version stamp for forward migration.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from .models import SCHEMA_VERSION

_LOCK = threading.Lock()  # single-writer per process


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt)


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "history.json")

    # -- durability -------------------------------------------------------
    def _write_atomic(self, payload: dict) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self.path)  # atomic on the same filesystem
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"schema": SCHEMA_VERSION, "objects": {}}
        if data.get("schema") != SCHEMA_VERSION:  # forward-migrate additively
            data.setdefault("objects", {})
            data["schema"] = SCHEMA_VERSION
        return data

    # -- health -----------------------------------------------------------
    def probe_write(self, timeout_s: float = 2.0) -> tuple[bool, str]:
        """Prove storage with a real WRITE, racing a hard timeout strictly
        shorter than the platform probe. Returns (ok, detail-with-errno)."""
        deadline = time.monotonic() + timeout_s
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".probe")
            with os.fdopen(fd, "w") as fh:
                fh.write("ok")
            os.remove(tmp)
            if time.monotonic() > deadline:
                return False, f"storage write exceeded {timeout_s}s at {self.data_dir}"
            return True, self.data_dir
        except OSError as exc:
            return False, f"errno {exc.errno} writing {self.data_dir}: {exc.strerror}"

    def remove_object(self, object_id: str) -> bool:
        """Remove one object from the store (used when a manual elset is
        deleted, so it disappears from the plot immediately)."""
        with _LOCK:
            data = self.load()
            objects = data.setdefault("objects", {})
            if object_id not in objects:
                return False
            del objects[object_id]
            self._write_atomic(data)
            return True

    # -- merge ------------------------------------------------------------
    def merge_samples(
        self,
        incoming: dict[str, dict],
        *,
        retention_days: int,
        max_samples: int,
        now: datetime | None = None,
    ) -> int:
        """Merge a pull into the store without shrinking it.

        `incoming` maps object_id -> {meta..., "samples": [{epoch, sub_lon_deg,
        inclination_deg}, ...]}. Existing objects and samples absent from the
        pull are retained; samples are deduped by epoch; the result is age-
        pruned and capped. Returns the number of new samples added.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        added = 0
        with _LOCK:
            data = self.load()
            objects: dict[str, dict] = data.setdefault("objects", {})
            for oid, rec in incoming.items():
                existing = objects.get(oid)
                if existing is None:
                    existing = {"samples": []}
                    objects[oid] = existing
                # meta: latest pull wins for display fields, but never blanks
                for key in (
                    "name", "data_mode", "classification_marking",
                    "source", "origin", "target",
                ):
                    val = rec.get(key)
                    if val is not None:
                        existing[key] = val
                by_epoch = {s["epoch"]: s for s in existing["samples"]}
                for s in rec.get("samples", []):
                    if s["epoch"] not in by_epoch:
                        by_epoch[s["epoch"]] = s
                        added += 1
                merged = [
                    s for s in by_epoch.values()
                    if _parse(s["epoch"]) >= cutoff
                ]
                merged.sort(key=lambda s: s["epoch"])
                existing["samples"] = merged[-max_samples:]
            # prune objects that have no surviving samples in the window
            for oid in [o for o, r in objects.items() if not r.get("samples")]:
                del objects[oid]
            self._write_atomic(data)
        return added
===FILE=== src/psirens/security.py
"""Security primitives: constant-time token compare, two-tier rate limiting,
and a single-flight guard for the refresh job.

The high-value asset is the refresh path (it does outbound UDL work and writes
the store) and the state-changing manual-elset routes. Reads of the belt data
are open by design: this build is Not Classified (owner-confirmed, 20 July
2026) and carries only low-sensitivity element sets. The token gates writes.
"""

from __future__ import annotations

import hmac
import threading
import time


def token_ok(given: str | None, expected: str) -> bool:
    """Constant-time compare with a length guard; never `==`."""
    a = (given or "").encode()
    b = (expected or "").encode()
    return len(a) == len(b) and hmac.compare_digest(a, b)


class RateLimiter:
    """Fixed-window limiter, keyed. Two instances give the two tiers."""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


class SingleFlight:
    """At most one refresh in flight; concurrent callers see `busy`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    def begin(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            return True

    def end(self) -> None:
        with self._lock:
            self._running = False

    @property
    def running(self) -> bool:
        return self._running
===FILE=== src/psirens/refresh.py
"""The refresh job: pull active sources, merge into the store single-flight.

A refresh is the one long, state-changing operation. It runs at most once at a
time; the hourly scheduler and an operator-triggered POST both go through the
same guard. It never raises out of the background loop: a source failure logs
and the store keeps its last-good state (integrity over freshness).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .config import Config
from .security import SingleFlight
from .sources import ElsetSource
from .store import Store

_log = logging.getLogger("psirens.refresh")


class Refresher:
    def __init__(self, cfg: Config, store: Store, sources: list[ElsetSource],
                 flight: SingleFlight):
        self.cfg = cfg
        self.store = store
        self.sources = sources
        self.flight = flight
        self.last_run: datetime | None = None
        self.last_added = 0

    def merge_one(self, src: ElsetSource, now: datetime | None = None) -> int:
        """Merge a single source immediately, lock-safe, WITHOUT the single-
        flight guard. Used by the manual-elset routes so an operator injection
        lands deterministically even while the scheduled refresh is running
        (the store's own lock serialises the concurrent writes)."""
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(days=self.cfg.retention_days)
        try:
            incoming = src.fetch(start, now)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("merge_one source failed: %s", exc)
            return 0
        if not incoming:
            return 0
        return self.store.merge_samples(
            incoming, retention_days=self.cfg.retention_days,
            max_samples=self.cfg.max_samples_per_object, now=now,
        )

    def run_once(self, now: datetime | None = None) -> dict:
        """Synchronous refresh. Returns a small status dict."""
        if not self.flight.begin():
            return {"status": "busy"}
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(days=self.cfg.retention_days)
        added = 0
        errors: list[str] = []
        try:
            for src in self.sources:
                try:
                    incoming = src.fetch(start, now)
                except Exception as exc:  # never fatal to the loop
                    errors.append(f"{type(src).__name__}: {exc}")
                    _log.warning("source %s failed: %s", type(src).__name__, exc)
                    continue
                if incoming:
                    added += self.store.merge_samples(
                        incoming,
                        retention_days=self.cfg.retention_days,
                        max_samples=self.cfg.max_samples_per_object,
                        now=now,
                    )
            self.last_run = now
            self.last_added = added
            return {"status": "ok", "added": added, "errors": errors}
        finally:
            self.flight.end()

    async def scheduler(self, stop: asyncio.Event) -> None:
        """Hourly background loop; a run failure never stops the loop."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as exc:  # pragma: no cover - belt and braces
                _log.error("scheduled refresh errored: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.refresh_seconds)
            except asyncio.TimeoutError:
                pass
===FILE=== src/psirens/sources.py
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
===FILE=== src/psirens/main.py
"""PSIRENS server: the createApp factory and the ASGI `app`.

Runtime contract (App Store): reads PORT (default 8080), binds 0.0.0.0 via the
gunicorn CMD, returns 200 unauthenticated at `/` and `/healthz`, runs non-root.
The operator Environment Variables tab must stay EMPTY for a code-defaults run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response

from .astro import drift_deg_per_day
from .config import Config, load_config
from .models import VIEW_MODES, DataMode, ManualElsetIn
from .refresh import Refresher
from .security import RateLimiter, SingleFlight, token_ok
from .sources import DemoElsetSource, ManualElsetSource, UDLElsetSource
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
_log = logging.getLogger("psirens")

_STATIC = os.path.join(os.path.dirname(__file__), "static")

# Classification ranking so the banner shows the most restrictive marking present.
_RANK = {"U": 0, "UNCLASSIFIED": 0, "CUI": 1, "C": 2, "S": 3, "TS": 4}


def _banner(markings: list[str], default: str = "UNCLASSIFIED") -> str:
    best, best_rank = default, -1
    for m in markings:
        r = _RANK.get(m.upper().split("//")[0], 0)
        if r > best_rank:
            best_rank, best = r, m
    # Show a readable word for the unclassified case rather than a bare "U".
    return "UNCLASSIFIED" if best_rank <= 0 else best


def _tracks_payload(cfg: Config, store: Store, view: str,
                    modes: set[DataMode]) -> dict:
    data = store.load()
    tracks, markings = [], []
    for oid, rec in data.get("objects", {}).items():
        try:
            mode = DataMode(rec.get("data_mode", "REAL"))
        except ValueError:
            mode = DataMode.REAL
        if mode not in modes:
            continue
        samples = rec.get("samples", [])
        if not samples:
            continue
        pairs = [
            (datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in samples
        ]
        markings.append(rec.get("classification_marking", "U"))
        tracks.append({
            "object_id": oid,
            "name": rec.get("name", oid),
            "data_mode": mode.value,
            "classification_marking": rec.get("classification_marking", "U"),
            "source": rec.get("source", ""),
            "origin": rec.get("origin", ""),
            "target": rec.get("target"),
            "samples": samples,
            "drift_deg_per_day": drift_deg_per_day(pairs),
        })
    return {
        "view": view,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification_banner": _banner(markings),
        "bounds": {
            "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
            "inc_min": cfg.inc_min, "inc_max": cfg.inc_max,
        },
        "count": len(tracks),
        "tracks": tracks,
    }


def create_app(cfg: Config | None = None,
               sources: list | None = None,
               http_client: httpx.Client | None = None) -> FastAPI:
    cfg = cfg or load_config()

    # Fail closed in production: a token with a wildcard origin refuses to start.
    if cfg.team_token and cfg.allowed_origin in ("", "*"):
        raise RuntimeError(
            "Refusing to start: TEAM_TOKEN is set but ALLOWED_ORIGIN is "
            "unset or '*'. Set ALLOWED_ORIGIN to the app's real origin."
        )

    store = Store(cfg.storage_dir())
    manual = ManualElsetSource(cfg.storage_dir())
    if sources is None:
        sources = []
        if cfg.udl_enabled:
            sources.append(UDLElsetSource(cfg, client=http_client))
        sources.append(manual)
        if cfg.demo_mode or not cfg.udl_enabled:
            sources.append(DemoElsetSource())
    flight = SingleFlight()
    refresher = Refresher(cfg, store, sources, flight)
    global_rl = RateLimiter(limit=120, window_s=60.0)
    strict_rl = RateLimiter(limit=6, window_s=60.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio
        ok, detail = store.probe_write()
        _log.info("storage boot verdict: %s (%s)", "WRITABLE" if ok else "UNWRITABLE", detail)
        refresher.run_once()  # seed the store immediately so `/` is never empty
        if cfg.udl_enabled:
            n = len(store.load().get("objects", {}))
            if n == 0:
                _log.warning(
                    "UDL enabled but first refresh added 0 objects; the demo "
                    "belt is OFF while UDL_BASE_URL is set, so the plot will be "
                    "empty. Verify against the tenant: UDL_ELSET_PATH=%s, "
                    "UDL_ACCEPT=%s, UDL_EPOCH_PARAM=%s, UDL_TARGET_FIELD=%s, "
                    "and the UDL_USER/UDL_PASSWORD credentials.",
                    cfg.udl_elset_path, cfg.udl_accept,
                    cfg.udl_epoch_param, cfg.udl_target_field,
                )
            else:
                _log.info("UDL first refresh: %d objects in store", n)
        stop = asyncio.Event()
        task = asyncio.create_task(refresher.scheduler(stop))
        try:
            yield
        finally:
            stop.set()
            task.cancel()

    app = FastAPI(title="PSIRENS", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.manual = manual
    app.state.refresher = refresher

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "anon"

    async def _global_gate(request: Request):
        if not global_rl.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="rate limit")

    def _require_token(authorization: str | None = Header(default=None)):
        if not cfg.team_token:
            return  # single-user local mode, auth off
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not token_ok(given, cfg.team_token):
            raise HTTPException(status_code=401, detail="unauthorized")

    def _cors(resp: Response) -> Response:
        if cfg.allowed_origin and cfg.allowed_origin != "*":
            resp.headers["Access-Control-Allow-Origin"] = cfg.allowed_origin
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # -- health (unauthenticated) ----------------------------------------
    @app.get("/healthz")
    def healthz():
        ok, detail = store.probe_write()
        if ok:
            return JSONResponse({"status": "ok", "data_dir": detail})
        return JSONResponse({"status": "unwritable", "detail": detail}, status_code=503)

    @app.get("/readyz")
    def readyz():
        return {"status": "ready", "last_refresh":
                refresher.last_run.isoformat() if refresher.last_run else None}

    # -- SPA + data (open reads) -----------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(_STATIC, "index.html"), encoding="utf-8") as fh:
            return _cors(HTMLResponse(fh.read()))

    # Icon and manifest assets (whitelisted; no path traversal).
    _ASSETS = {
        "icon-512.png": "image/png",
        "icon-192.png": "image/png",
        "favicon-32.png": "image/png",
        "apple-touch-icon.png": "image/png",
        "psirens-banner.png": "image/png",
        "manifest.webmanifest": "application/manifest+json",
    }

    @app.get("/favicon.ico")
    def favicon():
        return _cors(FileResponse(os.path.join(_STATIC, "favicon-32.png"),
                                  media_type="image/png"))

    @app.get("/static/{name}")
    def static_asset(name: str):
        media = _ASSETS.get(name)
        if media is None:
            raise HTTPException(status_code=404, detail="not found")
        return _cors(FileResponse(os.path.join(_STATIC, name), media_type=media))

    @app.get("/api/tracks", dependencies=[Depends(_global_gate)])
    def tracks(request: Request, view: str = "combined", modes: str = "",
               if_none_match: str | None = Header(default=None)):
        if view not in VIEW_MODES:
            raise HTTPException(status_code=400, detail="unknown view")
        if modes.strip():
            wanted = set()
            for m in modes.split(","):
                try:
                    wanted.add(DataMode(m.strip().upper()))
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"bad mode {m!r}")
        else:
            wanted = set(VIEW_MODES[view])
        payload = _tracks_payload(cfg, store, view, wanted)
        body = json.dumps(payload, separators=(",", ":"))
        # ETag is a CACHE KEY (not security): hash stable content only, so an
        # unchanged dataset returns 304 even though generated_at advances.
        stable = json.dumps(
            {k: payload[k] for k in ("view", "classification_banner", "bounds",
                                     "count", "tracks")},
            separators=(",", ":"), sort_keys=True,
        )
        etag = '"' + hashlib.sha1(stable.encode()).hexdigest() + '"'  # noqa: S324
        if if_none_match == etag:
            return _cors(Response(status_code=304))
        resp = Response(content=body, media_type="application/json")
        resp.headers["ETag"] = etag
        return _cors(resp)

    @app.get("/api/meta")
    def meta():
        return _cors(JSONResponse({
            "classification_default": "UNCLASSIFIED",
            "views": {k: [m.value for m in v] for k, v in VIEW_MODES.items()},
            "refresh_seconds": cfg.refresh_seconds,
            "retention_days": cfg.retention_days,
            "last_refresh": refresher.last_run.isoformat() if refresher.last_run else None,
            "manual_count": len(manual.list_active()),
        }))

    # -- state-changing (token-gated + strict rate limit) ----------------
    @app.post("/api/manual-elset", dependencies=[Depends(_require_token)])
    def add_manual(elset: ManualElsetIn, request: Request):
        if not strict_rl.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="rate limit")
        manual.add(elset)
        refresher.merge_one(manual)  # deterministic, not single-flight
        return _cors(JSONResponse({"status": "added", "object_id": elset.object_id}))

    @app.delete("/api/manual-elset/{object_id}", dependencies=[Depends(_require_token)])
    def del_manual(object_id: str):
        removed = manual.remove(object_id)
        if not removed:
            raise HTTPException(status_code=404, detail="not found")
        store.remove_object(object_id)  # drop from the plot immediately
        return _cors(JSONResponse({"status": "removed", "object_id": object_id}))

    @app.post("/api/refresh", dependencies=[Depends(_require_token)])
    def refresh(request: Request):
        if not strict_rl.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="rate limit")
        return _cors(JSONResponse(refresher.run_once()))

    return app


# ASGI entrypoint for gunicorn: `gunicorn --pythonpath src psirens.main:app`
app = create_app()
===FILE=== src/psirens/static/manifest.webmanifest
{
  "name": "PSIRENS",
  "short_name": "PSIRENS",
  "description": "Plotted Satellite Inclination and Raan ENgagement Screening",
  "icons": [
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
  ],
  "theme_color": "#162646",
  "background_color": "#0f1b33",
  "display": "standalone"
}
===FILE=== requirements.txt
# PSIRENS runtime dependencies (pinned; true resolved closure).
# sgp4 is a deliberate, recorded runtime dependency: the validated Vallado
# SGP4 propagator for sub-satellite longitude, chosen over a hand-rolled
# propagator to avoid the closed-form-substitution class of defect.
#
# NOTE: hashes omitted for portability. If the tenant requires --require-hashes,
# regenerate with `pip-compile --generate-hashes` before the live upload.
annotated-doc==0.0.4
annotated-types==0.7.0
anyio==4.14.2
certifi==2026.6.17
click==8.4.2
fastapi==0.139.2
gunicorn==26.0.0
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.18
packaging==26.2
pydantic==2.13.4
pydantic_core==2.46.4
sgp4==2.27
starlette==1.3.1
typing-inspection==0.4.2
typing_extensions==4.16.0
uvicorn==0.51.0
uvicorn-worker==0.4.0
===FILE=== requirements-dev.txt
# Dev/test only (never in the runtime image; see .dockerignore).
pytest==8.4.2
pytest-cov==7.0.0
===FILE=== pyproject.toml
[project]
name = "psirens"
version = "1.0.0"
description = "PSIRENS - Plotted Satellite Inclination and Raan ENgagement Screening"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
addopts = "--cov=psirens --cov-report=xml --cov-report=term-missing --cov-fail-under=80"

[tool.coverage.run]
source = ["psirens"]
omit = ["*/tests/*"]

[tool.coverage.report]
show_missing = true
===FILE=== sonar-project.properties
# The platform forces sonar.sources=src; declared here for local parity.
sonar.projectKey=psirens
sonar.sources=src
sonar.tests=tests
sonar.python.version=3.12
sonar.python.coverage.reportPaths=coverage.xml
# Coverage-metric exclusion only (never analysis): the served SPA is a static
# asset exercised by the browser smoke test, deferred to CI, not by pytest.
sonar.coverage.exclusions=src/psirens/static/**
===FILE=== .dockerignore
# Shapes the image (distinct from the packaging allowlist).
.venv/
tests/
coverage.xml
.coverage
.pytest_cache/
__pycache__/
*.pyc
.git/
.gitignore
.env
.env.*
requirements-dev.txt
simulate-pipeline.sh
package-appstore.sh
README.md
docs/
scaffold.py
===FILE=== .gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.coverage
coverage.xml
.env
.env.local
.env.*.local
/data/
/tmp/
*.tmp
dist/
===FILE=== .env.example
# PSIRENS configuration (environment only; never commit a real .env).
# For a code-defaults App Store run, the operator env tab stays EMPTY.

# Runtime (platform injects PORT; do not set it in the console or Dockerfile)
# PORT=8080
ALLOWED_ORIGIN=https://psirens.apps.bluestaq.com
TEAM_TOKEN=[REDACTED:token]

# Storage (FILE_STORAGE add-on injects STORAGE_MOUNT_PATH=/data)
# DATA_DIR=/data

# Window and cadence
RETENTION_DAYS=90
REFRESH_SECONDS=3600
LON_MIN=-180
LON_MAX=180
INC_MIN=0
INC_MAX=15

# UDL wire config (ALL TBC: verify against the tenant before live use).
# Leaving UDL_BASE_URL empty runs the offline demo belt.
UDL_BASE_URL=https://unifieddatalibrary.com
UDL_ELSET_PATH=/udl/elset
UDL_USER=[REDACTED:username]
UDL_PASSWORD=[REDACTED:password]
UDL_TARGET_FIELD=tags
UDL_EPOCH_PARAM=epoch
UDL_ACCEPT=application/json
===FILE=== Dockerfile
# PSIRENS - App Store python template. Multi-stage, non-root, port 8080.
# Contract: read PORT (default 8080), bind 0.0.0.0, GET / and /healthz return
# 200 unauthenticated, no ENV PORT, no ENV DATA_DIR (code defaults carry them;
# platform injection wins). Pin the base with @sha256:<digest> before upload.

FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM python:3.12-slim AS prep
# Patch OS packages (fail-open, in its own layer so it cannot mask the strip).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* 2>/dev/null || true
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY src ./src
# Create the non-root user, THEN strip suid/sgid as the LAST mutation, so no
# later instruction can re-introduce the class (fail-closed).
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser \
 && chown -R 10001:10001 /app \
 && find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} + 2>/dev/null || true

# Flatten to a single clean layer so the image-policy scanner finds no
# setuid/setgid bit in layer history.
FROM scratch
COPY --from=prep / /
ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
USER 10001:10001
EXPOSE 8080
# exec so SIGTERM reaches gunicorn; uvicorn worker for the ASGI app.
CMD ["sh","-c","exec gunicorn psirens.main:app -k uvicorn_worker.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers 2 --timeout 60"]
===FILE=== simulate-pipeline.sh
#!/bin/sh
# Reproduce the platform's test stage against the ACTUAL upload artefact,
# including its added CI file and GITLAB_CI=true, and verify coverage.xml.
# sh-portable (the platform runs under BusyBox sh, not bash).
set -eu
VERSION="${1:-1.0.0}"
sh package-appstore.sh "$VERSION"
SIM="$(mktemp -d)"
unzip -q "psirens-appstore-${VERSION}.zip" -d "$SIM"
printf 'stages: [test]\n' > "$SIM/.gitlab-ci.yml"   # platform commits its own
cd "$SIM"
python -m venv .venv
. .venv/bin/activate
pip install --quiet -r requirements.txt -r requirements-dev.txt
GITLAB_CI=true python -m pytest -q
test -s coverage.xml || { echo "FAIL: coverage.xml missing/empty"; exit 1; }
echo "SIMULATION GREEN: tests passed and coverage.xml present at $SIM/coverage.xml"
===FILE=== package-appstore.sh
#!/bin/sh
# Produce the flat App Store upload: Dockerfile, lockfiles, src, tests, config
# at the ROOT (no wrapping folder). Runs sh-portable, no bash features.
set -eu
VERSION="${1:-1.0.0}"
OUT="psirens-appstore-${VERSION}.zip"
rm -f "$OUT"
zip -r "$OUT" \
  Dockerfile .dockerignore requirements.txt requirements-dev.txt \
  pyproject.toml sonar-project.properties README.md \
  src tests \
  -x '*/__pycache__/*' '*.pyc' >/dev/null
echo "wrote $OUT"
unzip -l "$OUT" | sed -n '1,40p'
===FILE=== README.md
# PSIRENS - Plotted Satellite Inclination and Raan ENgagement Screening

A server-archetype dashboard for the Bluestaq App Store. It ingests UDL element
sets (including exercise tracks), computes each object's sub-satellite longitude
and inclination at every elset epoch, and renders the GEO belt as a longitude vs
inclination plot: historic trails, drift-direction heads sized by drift rate,
and dashed links to each object's labelled target.

Classification: Not Classified (owner-confirmed, 20 July 2026).

## Data modes and hero tabs

Ingests four UDL data modes: REAL, SIMULATED, TEST, EXERCISE. Two hero tabs:
- Real World - REAL only.
- Combined (Real + SIM) - all four modes, with per-mode toggle chips.

## Run locally