"""The history store: held in memory, with an append-only ledger on disk.

1.7.13, the ledger store (proposal of 24 November 2026, approved by Ash the
same day). Before it, every pull, refresh, readiness probe and /api/meta call
read or rewrote one 138 MB JSON file in full; measured at the retention cap, a
refresh with one overlapping probe peaked at 1,183 MB in a 1 GiB pod. Now the
history is decoded once, in the worker, and held; each change is written as a
small NEW file holding only what changed (a segment), and once a day a gzip
snapshot and a current ``history.json`` are written and older files retired.
New files and deletes are all an S3-backed mount allows by default: it
refuses renames, and overwrites need an option (Mountpoint semantics).

Contracts held here:
  * the merge RULES are unchanged: the same helpers below do the merging;
  * a restart sees exactly what was held (segments carry their own length,
    so a torn one is ignored, never half-applied);
  * copy on write: a change replaces the objects it touches rather than
    editing them, so a view built from the store before the change (the
    resolved view every route reads) is never altered underneath a reader;
  * ``history.json`` stays authoritative if anyone else writes it (a rollback
    to 1.7.12, or a test): a newer file is adopted, never overwritten blind;
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
import gzip
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import SCHEMA_VERSION

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


# The one message for a store used before it holds anything (1.7.14: these
# were asserts, which `python -O` strips; bandit B101).
NOT_LOADED = "store not loaded"
LEDGER_DIR = "ledger"
_SNAP, _SEG = "snap-", "seg-"
_META_FIELDS = _META_KEYS


def _sig(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _empty() -> dict:
    return {"schema": SCHEMA_VERSION, "objects": {}}


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return _empty()
    if data.get("schema") != SCHEMA_VERSION:  # forward-migrate additively
        data.setdefault("objects", {})
        data["schema"] = SCHEMA_VERSION
    return data


def dump_file(data_dir: str, path: str, obj: dict) -> None:
    """`resilient_write` for a large object: streamed to disk rather than
    built as one string first, which would cost the store's size again."""
    os.makedirs(data_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno not in _NO_RENAME:
                raise
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, separators=(",", ":"))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _newest(data: dict) -> str | None:
    newest: str | None = None
    for rec in data.get("objects", {}).values():
        samples = rec.get("samples") or []
        if samples and (newest is None or samples[-1]["epoch"] > newest):
            newest = samples[-1]["epoch"]
    return newest


def _prune(obj: dict, cutoff: datetime, max_samples: int) -> None:
    merged = [s for s in obj["samples"] if _parse(s["epoch"]) >= cutoff]
    merged.sort(key=lambda s: s["epoch"])
    obj["samples"] = merged[-max_samples:]


def _touched_keys(rec: dict) -> set:
    """Every stored key an incoming record could enrich or adopt."""
    want: set = set()
    for s in rec.get("samples", []):
        _, tags = _split_tags(s)
        want.add(((tags or {}).get("source") or "", s["epoch"]))
        want.add(("", s["epoch"]))
    return want


def _cow(held: dict | None, rec: dict) -> tuple[dict, dict]:
    """A private copy of one stored object for a merge to change, and a
    fingerprint of it before the change. Only the samples the incoming record
    could alter are copied: the rest are shared, and nothing shared is ever
    edited in place, so a reader holding the old object sees the old object."""
    if held is None:
        obj: dict = {"samples": []}
        return obj, {"samples": {}, "meta": {}, "elset": None, "cands": {}, "prov": 0}
    obj = dict(held)
    if isinstance(obj.get("provenance"), list):
        obj["provenance"] = list(obj["provenance"])
    if isinstance(obj.get("elset_candidates"), dict):
        obj["elset_candidates"] = dict(obj["elset_candidates"])
    want = _touched_keys(rec)
    samples, seen = [], {}
    for s in held["samples"]:
        key = _stored_key(obj, s)
        seen[key] = len(s)
        samples.append(dict(s) if key in want else s)
    obj["samples"] = samples
    before = {"samples": seen, "meta": {k: obj.get(k) for k in _META_FIELDS},
              "elset": obj.get("elset"), "cands": dict(obj.get("elset_candidates") or {}),
              "prov": len(obj.get("provenance") or [])}
    return obj, before


def _object_delta(obj: dict, before: dict) -> dict:
    """What one change did to one object: the fields, element sets and
    samples that differ, and the sample keys that went."""
    d: dict = {}
    meta = {k: obj.get(k) for k in _META_FIELDS if obj.get(k) != before["meta"].get(k)}
    if meta:
        d["meta"] = meta
    if obj.get("elset") != before["elset"]:
        d["elset"] = obj.get("elset")
    cands = {k: v for k, v in (obj.get("elset_candidates") or {}).items()
             if before["cands"].get(k) != v}
    if cands:
        d["cands"] = cands
    if len(obj.get("provenance") or []) != before["prov"]:
        d["prov"] = obj.get("provenance")
    held, now_keys, upserts = before["samples"], set(), []
    for s in obj["samples"]:
        key = _stored_key(obj, s)
        now_keys.add(key)
        if held.get(key) != len(s):
            upserts.append(s)
    if upserts:
        d["up"] = upserts
    removed = [list(k) for k in held if k not in now_keys]
    if removed:
        d["rm"] = removed
    return d


def _apply(obj: dict, d: dict) -> None:
    for k, v in (d.get("meta") or {}).items():
        obj[k] = v
    if "prov" in d:
        obj["provenance"] = d["prov"]
    if "elset" in d:
        obj["elset"] = d["elset"]
    if d.get("cands"):
        obj.setdefault("elset_candidates", {}).update(d["cands"])
    by_key = {_stored_key(obj, s): s for s in obj["samples"]}
    for k in d.get("rm", []):
        by_key.pop(tuple(k), None)
    for s in d.get("up", []):
        by_key[_stored_key(obj, s)] = s
    obj["samples"] = list(by_key.values())


def _replay(delta: dict, data: dict) -> None:
    objects = data.setdefault("objects", {})
    if delta.get("op") == "merge":
        cutoff = _parse(delta["now"]) - timedelta(days=delta["retention_days"])
        for oid, d in delta["objects"].items():
            obj = objects.setdefault(oid, {"samples": []})
            _apply(obj, d)
            _prune(obj, cutoff, delta["max_samples"])
    for oid in delta.get("deleted", []):
        objects.pop(oid, None)


class Store:
    """The history, held once per worker, with an append-only ledger.

    `load()` returns the HELD store, not a copy: it is read-only by contract.
    Every change goes through a method here, under the lock, and replaces the
    objects it touches. The store is decoded lazily on first use, so the
    gunicorn master (``--preload``) never holds a copy beside its worker.
    """

    def __init__(self, data_dir: str, *, purge_on_boot: bool = False,
                 compact_hours: float = 24.0, compact_segments: int = 48,
                 keep_history: bool = True):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "history.json")
        self.ledger_dir = os.path.join(data_dir, LEDGER_DIR)
        self.purge_on_boot = purge_on_boot
        self.compact_hours = compact_hours
        self.compact_segments = compact_segments   # 51: early, during a backfill
        self.keep_history = keep_history   # Ash, 24 Nov: history.json kept current daily
        self._lock = threading.RLock()
        self._data: dict | None = None
        self._seq = 0
        self._version = 0
        self._newest: str | None = None
        self._file_sig: tuple | None = None
        self._snap_seq = 0
        self._snap_at: float | None = None
        self._since_snap = 0
        self.last_write_bytes = 0
        self.boot_seconds: float | None = None

    # -- files --------------------------------------------------------------
    def _files(self, prefix: str) -> list[tuple[int, str]]:
        try:
            names = os.listdir(self.ledger_dir)
        except OSError:
            return []
        out = []
        for name in names:
            if name.startswith(prefix):
                try:
                    out.append((int(name[len(prefix):].split(".")[0]),
                                os.path.join(self.ledger_dir, name)))
                except ValueError:
                    continue
        return sorted(out)

    def _write_new(self, name: str, text: str) -> int:
        os.makedirs(self.ledger_dir, exist_ok=True)
        with open(os.path.join(self.ledger_dir, name), "x", encoding="utf-8") as fh:
            fh.write(text)   # a NEW file, never an overwrite
        return len(text.encode("utf-8"))

    def _next_seq(self) -> int:
        """The next file number, never one already on disk: another process
        (a rolling deploy, or a test's second instance) may have written."""
        on_disk = [s for s, _ in self._files(_SNAP) + self._files(_SEG)]
        self._seq = max([self._seq] + on_disk) + 1
        return self._seq

    def _append(self, delta: dict) -> None:
        self._next_seq()
        body = json.dumps(delta, separators=(",", ":"))
        self.last_write_bytes = self._write_new(
            "%s%010d.json" % (_SEG, self._seq), json.dumps({"len": len(body)}) + "\n" + body)
        self._since_snap += 1
        self._changed()

    def _snapshot(self) -> int:
        """A full snapshot as a new file; with `keep_history`, history.json
        first, so the snapshot is the newer of the two."""
        if self._data is None:
            raise RuntimeError(NOT_LOADED)
        if self.keep_history:
            dump_file(self.data_dir, self.path, self._data)
            self._file_sig = _sig(self.path)
        self._next_seq()
        os.makedirs(self.ledger_dir, exist_ok=True)
        path = os.path.join(self.ledger_dir, "%s%010d.json.gz" % (_SNAP, self._seq))
        with gzip.open(path, "xt", encoding="utf-8", compresslevel=1) as fh:
            json.dump(self._data, fh, separators=(",", ":"))
        self._snap_seq, self._snap_at, self._since_snap = self._seq, time.time(), 0
        self.last_write_bytes = os.path.getsize(path)
        return self.last_write_bytes

    def _retire(self) -> None:
        for seq, path in self._files(_SEG) + self._files(_SNAP):
            if seq < self._snap_seq:
                try:
                    os.remove(path)   # refused on a mount without delete: the
                except OSError:       # file is simply never read again
                    pass

    # -- boot ---------------------------------------------------------------
    def _read_ledger(self) -> tuple[dict | None, float]:
        """The newest readable snapshot with every later segment replayed,
        and the mtime of the newest ledger file (0 when there is none)."""
        snaps, segs = self._files(_SNAP), self._files(_SEG)
        newest = max([os.path.getmtime(p) for _, p in snaps + segs] or [0.0])
        self._seq = max([s for s, _ in snaps + segs] or [0])
        for seq, path in reversed(snaps):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, EOFError, ValueError):
                continue   # a torn snapshot: fall back to the one before
            self._snap_seq, self._snap_at = seq, os.path.getmtime(path)
            self._replay_after(segs, seq, data)
            return data, newest
        if segs and not snaps:
            # Segments from a store that began empty, before any snapshot.
            data = _empty()
            self._replay_after(segs, 0, data)
            return data, newest
        if snaps:
            _log.error("no ledger snapshot in %s could be read", self.ledger_dir)
        return None, newest

    def _replay_after(self, segs: list[tuple[int, str]], seq: int, data: dict) -> None:
        for s, seg in segs:
            if s > seq and self._replay_file(seg, data):
                self._since_snap += 1

    @staticmethod
    def _replay_file(path: str, data: dict) -> bool:
        try:
            with open(path, encoding="utf-8") as fh:
                head, body = fh.read().split("\n", 1)
            if json.loads(head)["len"] != len(body):
                return False   # torn: ignored, never half-applied
            _replay(json.loads(body), data)
            return True
        except (OSError, ValueError, KeyError):
            return False

    def _boot(self) -> None:
        started = time.monotonic()
        data, ledger_mtime = self._read_ledger()
        sig = _sig(self.path)
        # The file wins when it is newer than everything in the ledger: the
        # first start of 1.7.13, or a return after a rollback that wrote it.
        if sig is not None and (data is None or sig[0] / 1e9 > ledger_mtime):
            data = _read_json(self.path)
            self._data = data
            self._file_sig = sig
            keep, self.keep_history = self.keep_history, False
            try:
                self._snapshot()
            except OSError as exc:
                _log.warning("ledger snapshot refused at %s: %s", self.ledger_dir, exc)
            finally:
                self.keep_history = keep
        else:
            self._data = data if data is not None else _empty()
            self._file_sig = sig
        if self.purge_on_boot:
            self._purge_locked()
        self._changed()
        self.boot_seconds = round(time.monotonic() - started, 2)

    def _check_external(self) -> None:
        sig = _sig(self.path)
        if sig is not None and sig != self._file_sig:
            _log.warning("history.json changed outside this process; adopting it")
            self._data = _read_json(self.path)
            self._file_sig = sig
            keep, self.keep_history = self.keep_history, False
            try:
                self._snapshot()
            finally:
                self.keep_history = keep
            self._changed()

    def _changed(self) -> None:
        self._version += 1
        self._newest = _newest(self._data or {})

    def ensure(self) -> dict:
        """The held store, decoding it on first use."""
        with self._lock:
            if self._data is None:
                self._boot()
            else:
                self._check_external()
            if self._data is None:
                raise RuntimeError(NOT_LOADED)
            return self._data

    # -- the read side ------------------------------------------------------
    def load(self) -> dict:
        """The HELD store. Read-only by contract; see the class docstring."""
        return self.ensure()

    def read(self, fn: Callable[[dict], Any]) -> Any:
        """Run `fn` over the held store with no change able to land mid-read."""
        with self._lock:
            return fn(self.ensure())

    @property
    def version(self) -> int:
        """Changes whenever the held store does: what caches key on."""
        with self._lock:
            self.ensure()
            return self._version

    def newest_epoch(self) -> str | None:
        """The newest sample epoch held, across every object, or None if empty.

        The single number that says whether ingest is actually working. Kept
        as the store changes (1.7.13): until then each call parsed the whole
        file, three times per readiness probe. Read WITHOUT the lock once the
        store is held: a probe must never wait behind a merge or a resolve
        (measured: 1.7 s when it did), and one attribute read is atomic.
        """
        if self._data is None:
            # Still decoding (12 s on the first start, which adopts the old
            # file): answer at once as an empty store would, never hang a probe.
            if not self._lock.acquire(blocking=False):
                return None
            try:
                self.ensure()
            finally:
                self._lock.release()
        return self._newest

    def stats(self) -> dict:
        with self._lock:
            self.ensure()
            age = None if self._snap_at is None else (time.time() - self._snap_at) / 3600
            return {"backend": "ledger", "version": self._version,
                    "snapshot_seq": self._snap_seq,
                    "snapshot_age_hours": None if age is None else round(age, 2),
                    "segments_since_snapshot": self._since_snap,
                    "last_write_bytes": self.last_write_bytes,
                    "boot_seconds": self.boot_seconds,
                    "compact_hours": self.compact_hours,
                    "compact_segments": self.compact_segments}

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

    # -- whole-store operations ---------------------------------------------
    def _write_atomic(self, payload: dict) -> None:
        """Replace the whole store (tests, migrations): a snapshot, and with
        `keep_history` a current history.json."""
        with self._lock:
            if self._data is None:
                self._boot()
            self._data = payload
            self._snapshot()
            self._changed()

    def compact(self) -> int:
        """Write a snapshot (and history.json) and retire older files.
        Returns the snapshot's size in bytes."""
        with self._lock:
            self.ensure()
            size = self._snapshot()
            self._retire()
            return size

    def compaction_due(self, now: float | None = None) -> bool:
        """Once a day (Ash, 24 Nov), or sooner once `compact_segments` segments
        have piled up since the snapshot (Ash, 26 Nov): a backfill writes one
        per chunk, and every boot replays them all from the mount (131 gave a
        67 s boot on 25 Nov). Hourly pulls alone write 24 a day, so the daily
        cadence is unchanged in normal running."""
        with self._lock:
            if self._data is None:
                return False
            if self._snap_at is None:
                return True
            if 0 < self.compact_segments <= self._since_snap:
                return True
            return ((now or time.time()) - self._snap_at) >= self.compact_hours * 3600

    def compact_if_due(self) -> int | None:
        """Compact only if still due, decided under the lock, so two callers
        racing for the same backlog compact it once. Returns the snapshot's
        size, or None when nothing was due."""
        with self._lock:
            return self.compact() if self.compaction_due() else None

    # -- deletions ------------------------------------------------------------
    def _delete(self, drop: list[str]) -> None:
        objects = self.ensure().setdefault("objects", {})
        for oid in drop:
            del objects[oid]
        self._append({"op": "delete", "deleted": drop})

    def remove_object(self, object_id: str) -> bool:
        """Remove one object from the store (used when a manual elset is
        deleted, so it disappears from the plot immediately)."""
        with self._lock:
            if object_id not in self.ensure().get("objects", {}):
                return False
            self._delete([object_id])
            return True

    def retain_only(self, ids: set[str], *, keep_origins: tuple[str, ...] = (),
                    keep_nonreal: bool = True) -> int:
        """Drop every object whose id is not in `ids`, except those whose origin
        is in keep_origins or (when keep_nonreal) whose data_mode is not REAL.
        The HRR set is a REAL-world construct, so this prune only ever removes
        REAL objects that dropped off the list; SIMULATED/TEST/EXERCISE data
        that an operator pulled for a scenario is never touched. Returns the
        number removed."""
        with self._lock:
            objects = self.ensure().get("objects", {})
            drop = [o for o, r in objects.items()
                    if o not in ids and r.get("origin") not in keep_origins
                    and not (keep_nonreal and r.get("data_mode", "REAL") != "REAL")]
            if drop:
                self._delete(drop)
            return len(drop)

    def purge_synthetic(self) -> int:
        """Remove every trace of the offline demo belt. Run when the app
        starts against live UDL (from 1.7.13 at boot, in the worker), so a
        store that was ever run offline on the same volume cannot carry
        synthetic objects into the live picture. This is the one deletion the
        archive rule allows: the data was never observed. Returns the number
        of samples and objects removed."""
        with self._lock:
            self.ensure()
            return self._purge_locked()

    def _purge_locked(self) -> int:
        if self._data is None:
            raise RuntimeError(NOT_LOADED)
        objects = self._data.setdefault("objects", {})
        removed = 0
        # Decide first, change after: the dict cannot change size while it is
        # walked (python:S7504 on the 1.7.5 upload). Kept objects are copied
        # before they are trimmed, so no reader sees one change.
        doomed, trimmed = [], {}
        for oid, rec in objects.items():
            if rec.get("origin") == "DEMO" or oid.startswith("DEMO-"):
                removed += 1 + len(rec.get("samples") or [])
                doomed.append(oid)
                continue
            copy = dict(rec, elset_candidates=dict(rec.get("elset_candidates") or {}))
            dropped = _drop_demo_parts(copy)
            if dropped:
                removed += dropped
                trimmed[oid] = copy
        for oid, rec in trimmed.items():
            if rec.get("samples"):
                objects[oid] = rec
            else:
                doomed.append(oid)
        for oid in doomed:
            del objects[oid]
        if removed:
            self._snapshot()
            self._changed()
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
        pull are retained; samples are deduped by provider and epoch; the result
        is age-pruned and capped. Only what changed is written, as one new
        segment. Returns the number of new samples added.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        added = 0
        delta: dict = {"op": "merge", "now": now.astimezone(timezone.utc).isoformat(),
                       "retention_days": retention_days, "max_samples": max_samples,
                       "objects": {}, "deleted": []}
        with self._lock:
            objects = self.ensure().setdefault("objects", {})
            for oid, rec in incoming.items():
                existing, before = _cow(objects.get(oid), rec)
                _copy_meta(existing, rec)  # display fields; latest non-blank wins
                _retain_elset(existing, rec)  # newest full element set
                _retain_candidates(existing, rec)  # newest per provider
                added += _merge_object(existing, rec)
                _prune(existing, cutoff, max_samples)
                objects[oid] = existing
                delta["objects"][oid] = _object_delta(existing, before)
            # prune objects that have no surviving samples in the window
            doomed = [o for o, r in objects.items() if not r.get("samples")]
            for oid in doomed:
                del objects[oid]
            delta["deleted"] = doomed
            self._append(delta)
        return added
