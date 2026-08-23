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

import errno
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from .models import SCHEMA_VERSION

_LOCK = threading.Lock()  # single-writer per process
_log = logging.getLogger("psirens.store")

# errnos raised by volumes that do not implement rename (seen on some
# Kubernetes NFS/CephFS/CSI mounts): "Function not implemented" and kin.
_NO_RENAME = (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP)


def resilient_write(data_dir: str, path: str, text: str) -> None:
    """Write text to path durably.

    Atomic (temp file then rename) where the filesystem supports it, which is
    the safe default on normal disks and tmpfs. On volumes that refuse rename
    (ENOSYS/EINVAL/ENOTSUP), fall back to a direct write so the app still
    functions; readers tolerate a rare partial read by returning the empty
    default and succeeding on the next read.
    """
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.replace(tmp, path)  # atomic where the filesystem supports rename
        except OSError as exc:
            if exc.errno not in _NO_RENAME:
                raise
            _log.warning("atomic rename unsupported on %s (errno %s: %s); "
                         "writing directly", data_dir, exc.errno, exc.strerror)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt)


_META_KEYS = ("name", "data_mode", "classification_marking",
              "source", "origin", "target")


def _copy_meta(existing: dict, rec: dict) -> None:
    """Latest pull wins for display fields, but a missing field never blanks
    an existing value."""
    for key in _META_KEYS:
        val = rec.get(key)
        if val is not None:
            existing[key] = val


def _retain_elset(existing: dict, rec: dict) -> None:
    """Keep the newest full element set (by epoch) so conjunctions and TLE
    export always use the freshest mean elements."""
    in_el = rec.get("elset")
    if in_el is None:
        return
    cur = existing.get("elset")
    if cur is None or in_el.get("epoch", "") >= cur.get("epoch", ""):
        existing["elset"] = in_el


def _retain_candidates(existing: dict, rec: dict) -> None:
    """Keep the newest element set per provider, anti-shrink.

    A pull that returns nothing from one provider must not erase what that
    provider supplied on an earlier pull: the TLE selection policy chooses
    between providers, so silently losing one would change which line an
    operator sees for reasons that have nothing to do with the data.
    """
    incoming = rec.get("elset_candidates")
    if not isinstance(incoming, dict):
        return
    bucket = existing.setdefault("elset_candidates", {})
    for source, el in incoming.items():
        held = bucket.get(source)
        if held is None or el.get("epoch", "") >= held.get("epoch", ""):
            bucket[source] = el


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "history.json")

    # -- durability -------------------------------------------------------
    def _write_atomic(self, payload: dict) -> None:
        resilient_write(self.data_dir, self.path,
                        json.dumps(payload, separators=(",", ":")))

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

    def retain_only(self, ids: set[str], *, keep_origins: tuple[str, ...] = (),
                    keep_nonreal: bool = True) -> int:
        """Drop every object whose id is not in `ids`, except those whose origin
        is in keep_origins or (when keep_nonreal) whose data_mode is not REAL.
        The HRR set is a REAL-world construct, so this prune only ever removes
        REAL objects that dropped off the list; SIMULATED/TEST/EXERCISE data
        that an operator pulled for a scenario is never touched. Returns the
        number removed."""
        with _LOCK:
            data = self.load()
            objects = data.setdefault("objects", {})
            drop = [o for o, r in objects.items()
                    if o not in ids and r.get("origin") not in keep_origins
                    and not (keep_nonreal and r.get("data_mode", "REAL") != "REAL")]
            if not drop:
                return 0
            for o in drop:
                del objects[o]
            self._write_atomic(data)
            return len(drop)

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
                _copy_meta(existing, rec)  # display fields; latest non-blank wins
                _retain_elset(existing, rec)  # newest full element set
                _retain_candidates(existing, rec)  # newest per provider
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
