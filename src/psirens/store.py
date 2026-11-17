"""Atomic JSON store on the file-storage add-on.

Contracts held here:
  * atomic writes (temp file then rename on the same filesystem);
  * anti-shrink merge (a refresh never deletes an object or its history that
    the new pull happened not to include);
  * dedup by (object_id, provider, epoch) so overlapping pulls do not
    double-count, and two providers' fixes at one instant stay two (1.7.4);
  * provenance per sample: every sample points into its object's
    `provenance` table (provider, data mode, marking), never inferred;
  * age prune to the retention window and a per-object sample cap (newest kept);
  * a schema version stamp for forward migration.
"""

from __future__ import annotations

import errno
import json
import math
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


def _is_demo_source(source: object) -> bool:
    """The demo provider, or one of the synthetic providers of the 1.7.5
    agreement demo (DEMO-ALPHA and kin)."""
    text = str(source or "")
    return text == "DEMO" or text.startswith("DEMO-")


def _drop_demo_parts(rec: dict) -> int:
    """Strip DEMO-tagged samples and element sets from one real object."""
    keep = [s for s in rec.get("samples") or []
            if not _is_demo_source((sample_provenance(rec, s) or {}).get("source"))]
    removed = len(rec.get("samples") or []) - len(keep)
    rec["samples"] = keep
    cands = rec.get("elset_candidates") or {}
    for key in [k for k, el in cands.items() if _is_demo_source((el or {}).get("source"))]:
        del cands[key]
        removed += 1
    if _is_demo_source((rec.get("elset") or {}).get("source")):
        rec.pop("elset")
        removed += 1
    return removed


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


def _enrich_sample(held: dict, incoming: dict) -> None:
    """Fill fields a stored sample is MISSING; never overwrite one it has.

    Only ever called for the SAME provider at the same epoch (1.7.4). Before
    that it was keyed on epoch alone, so where two providers shared an
    instant one provider's sample could be completed from the other's.

    The store's anti-shrink rule, applied at sample level. A re-pull at an
    epoch already held used to be discarded whole, so a sample written before
    a field existed stayed without it for the entire retention window. That is
    what `raan_deg` hit in 1.6.17: every sample already in the store was frozen
    without the one value the co-planar trail needs.

    Deliberately NOT counted as an added sample. `last_ingest_added` is
    load-bearing for the staleness diagnosis and has to keep meaning "new
    epochs arrived", or a store that only ever re-reads the same window would
    look healthy.
    """
    for key, value in incoming.items():
        if value is not None and held.get(key) is None:
            held[key] = value


_TAGS = ("source", "data_mode", "classification_marking")
_SAME_VALUE_DEG = 5e-5   # samples are stored to 4 decimal places


def _split_tags(sample: dict) -> tuple[dict, dict | None]:
    """The sample without its inline provenance, and that provenance (None
    for an untagged sample, as written by a caller before 1.7.4)."""
    tags = {k: sample.get(k) for k in _TAGS}
    clean = {k: v for k, v in sample.items() if k not in _TAGS}
    return clean, (tags if all(tags.values()) else None)


def _prov_index(existing: dict, tags: dict) -> int:
    """Index of these tags in the object's provenance table, added if new.
    A table rather than three strings on every sample: the store is read
    whole on every request, and a marking such as U//PR-NORTHSTAR-ELSET
    repeated on 2000 samples per object would nearly double it."""
    table = existing.setdefault("provenance", [])
    for i, row in enumerate(table):
        if row == tags:
            return i
    table.append(dict(tags))
    return len(table) - 1


def sample_provenance(obj: dict, sample: dict) -> dict | None:
    """The provider, data mode and marking of the record a stored sample was
    calculated from, or None for a sample stored before 1.7.4 that no re-pull
    has yet matched."""
    idx = sample.get("p")
    table = obj.get("provenance") or []
    if isinstance(idx, int) and 0 <= idx < len(table):
        return table[idx]
    return None


def _stored_key(obj: dict, sample: dict) -> tuple[str, str]:
    prov = sample_provenance(obj, sample)
    return ((prov or {}).get("source") or "", sample["epoch"])


def _same_values(a: dict, b: dict) -> bool:
    """Whether two samples carry the same position. Used ONLY to recognise a
    re-pull of the very record an untagged sample was made from: two
    different providers do not agree to four decimal places in both longitude
    and inclination at the same instant."""
    try:
        return all(math.isclose(float(a[k]), float(b[k]), abs_tol=_SAME_VALUE_DEG)
                   for k in ("sub_lon_deg", "inclination_deg"))
    except (KeyError, TypeError, ValueError):
        return False


def _adopt_legacy(by_key: dict, clean: dict, source: str) -> bool:
    """Give an untagged (pre-1.7.4) sample its provenance when a re-pull
    reproduces it exactly. Anything short of an exact match is treated as a
    different record: provenance is attached on evidence, never assumed."""
    legacy_key = ("", clean["epoch"])
    legacy = by_key.get(legacy_key)
    if legacy is None or not _same_values(legacy, clean):
        return False
    _enrich_sample(legacy, clean)
    del by_key[legacy_key]
    by_key[(source, clean["epoch"])] = legacy
    return True


def _merge_object(existing: dict, rec: dict) -> int:
    """Merge one incoming record's samples into a stored object. Returns the
    number of NEW samples (an adopted or enriched sample is not new)."""
    by_key = {_stored_key(existing, s): s for s in existing["samples"]}
    added = 0
    for s in rec.get("samples", []):
        clean, tags = _split_tags(s)
        source = ""
        if tags is not None:
            clean["p"] = _prov_index(existing, tags)
            source = tags["source"]
        key = (source, clean["epoch"])
        held = by_key.get(key)
        if held is not None:
            _enrich_sample(held, clean)   # same provider, same epoch only
        elif not (source and _adopt_legacy(by_key, clean, source)):
            by_key[key] = clean
            added += 1
    existing["samples"] = list(by_key.values())
    return added


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

    def newest_epoch(self) -> str | None:
        """The newest sample epoch held, across every object, or None if empty.

        The single number that says whether ingest is actually working. Before
        1.6.6 nothing exposed it, so a feed that had been silently returning
        truncated pages for a fortnight looked identical to a healthy one: the
        refresh timestamp kept advancing, the object count was unchanged, and
        the staleness was visible only by opening one object and reading its
        epoch by eye. Which is how it was found.
        """
        newest: str | None = None
        for rec in self.load().get("objects", {}).values():
            samples = rec.get("samples") or []
            if not samples:
                continue
            latest = samples[-1].get("epoch")
            if latest and (newest is None or latest > newest):
                newest = latest
        return newest

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

    def purge_synthetic(self) -> int:
        """Remove every trace of the offline demo belt. Called when the app
        starts against live UDL, so a store that was ever run offline on the
        same volume cannot carry synthetic objects into the live picture.
        This is the one deletion the archive rule allows: the data was never
        observed. Returns the number of samples and objects removed."""
        removed = 0
        with _LOCK:
            data = self.load()
            objects = data.setdefault("objects", {})
            # Decide first, delete after: the dict cannot change size while
            # it is being walked, and wrapping it in list() to allow that was
            # python:S7504 on the 1.7.5 upload.
            doomed = []
            for oid, rec in objects.items():
                if rec.get("origin") == "DEMO" or oid.startswith("DEMO-"):
                    removed += 1 + len(rec.get("samples") or [])
                    doomed.append(oid)
                    continue
                removed += _drop_demo_parts(rec)
                if not rec.get("samples"):
                    doomed.append(oid)
            for oid in doomed:
                del objects[oid]
            if removed:
                self._write_atomic(data)
        if removed:
            _log.warning("purged %d synthetic (DEMO) item(s) from the live store", removed)
        return removed

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
                added += _merge_object(existing, rec)
                merged = [
                    s for s in existing["samples"]
                    if _parse(s["epoch"]) >= cutoff
                ]
                merged.sort(key=lambda s: s["epoch"])
                existing["samples"] = merged[-max_samples:]
            # prune objects that have no surviving samples in the window
            for oid in [o for o, r in objects.items() if not r.get("samples")]:
                del objects[oid]
            self._write_atomic(data)
        return added
