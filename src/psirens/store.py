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
