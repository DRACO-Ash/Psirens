"""Prototype: the belt held in memory once, and only what changed written down.

Run from the repository root with PYTHONPATH=src. Nothing here ships.

`LedgerStore` is a drop-in for `psirens.store.Store` on the paths that matter
(load, merge_samples, retain_only, newest_epoch). It keeps the decoded store in
memory, applies a pull with the PRODUCTION merge helpers (so the rules are the
same code, not a copy), and appends one small, immutable segment file holding
only what the pull changed. It never renames and never overwrites a file, which
is all an S3-backed mount allows by default. A snapshot is written now and then
(compaction) so a restart replays at most a day of segments.

Boot: newest snapshot, then every later segment in sequence order. A segment is
written whole before it is closed, so a half-written one cannot be mistaken for
a whole one: each carries its own length check.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from psirens import store as S

_LOCK = threading.Lock()


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class LedgerStore:
    """In-memory store with an append-only ledger on disk."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.dir = os.path.join(data_dir, "ledger")
        os.makedirs(self.dir, exist_ok=True)
        self.bytes_written = 0       # measurement hook: bytes of the last write
        # The file the last write produced. `Resolver` and `EventEngine` key
        # their caches on a stat of `path`; in the build they key on a version
        # counter instead, and this goes.
        self.path = os.path.join(self.dir, "boot")
        self.data, self.seq, self._newest = self._boot()

    # -- boot ---------------------------------------------------------------
    def _files(self, prefix: str) -> list[tuple[int, str]]:
        out = []
        for name in os.listdir(self.dir):
            if name.startswith(prefix):
                seq = int(name[len(prefix):].split(".")[0])
                out.append((seq, os.path.join(self.dir, name)))
        return sorted(out)

    def _boot(self) -> tuple[dict, int, str | None]:
        snaps = self._files("snap-")
        if snaps:
            seq, path = snaps[-1]
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            # First start on a volume that holds the legacy single file:
            # adopt it once, as snapshot 0.
            data = S.Store(self.data_dir).load()
            seq = 0
            self._write_snapshot(data, seq)
        for s, path in self._files("seg-"):
            if s > seq:
                self._replay(path, data)
                seq = s
        return data, seq, _newest(data)

    # -- the read side ------------------------------------------------------
    def load(self) -> dict:
        """The held store. Read-only by contract: consumers that need to
        change it go through merge_samples / retain_only."""
        return self.data

    def newest_epoch(self) -> str | None:
        return self._newest

    # -- the write side -----------------------------------------------------
    def merge_samples(self, incoming: dict[str, dict], *, retention_days: int,
                      max_samples: int, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        added = 0
        delta: dict = {"op": "merge", "now": _now_iso(now),
                       "retention_days": retention_days,
                       "max_samples": max_samples, "objects": {}, "deleted": []}
        with _LOCK:
            objects = self.data.setdefault("objects", {})
            for oid, rec in incoming.items():
                existing = objects.get(oid)
                if existing is None:
                    existing = {"samples": []}
                    objects[oid] = existing
                before = _fingerprint(existing)
                S._copy_meta(existing, rec)
                S._retain_elset(existing, rec)
                S._retain_candidates(existing, rec)
                added += S._merge_object(existing, rec)
                _prune(existing, cutoff, max_samples)
                delta["objects"][oid] = _object_delta(existing, before)
            for oid in [o for o, r in objects.items() if not r.get("samples")]:
                del objects[oid]
                delta["deleted"].append(oid)
            self._append(delta)
            self._newest = _newest(self.data)
        return added

    def retain_only(self, ids: set[str], *, keep_origins: tuple[str, ...] = (),
                    keep_nonreal: bool = True) -> int:
        with _LOCK:
            objects = self.data.setdefault("objects", {})
            drop = [o for o, r in objects.items()
                    if o not in ids and r.get("origin") not in keep_origins
                    and not (keep_nonreal and r.get("data_mode", "REAL") != "REAL")]
            if not drop:
                return 0
            for o in drop:
                del objects[o]
            self._append({"op": "delete", "deleted": drop})
            self._newest = _newest(self.data)
            return len(drop)

    def compact(self) -> int:
        """Write a snapshot of the held store and retire older files. Returns
        the bytes written."""
        with _LOCK:
            self.seq += 1
            n = self._write_snapshot(self.data, self.seq)
            for s, path in self._files("seg-") + self._files("snap-"):
                if s < self.seq:
                    try:
                        os.remove(path)   # needs --allow-delete on an S3 mount;
                    except OSError:       # if refused, the files are ignored
                        pass              # (boot reads only what follows the
            return n                      # newest snapshot) and cost quota only

    # -- files --------------------------------------------------------------
    def _append(self, delta: dict) -> None:
        self.seq += 1
        body = json.dumps(delta, separators=(",", ":"))
        text = json.dumps({"len": len(body)}) + "\n" + body
        path = os.path.join(self.dir, "seg-%010d.json" % self.seq)
        with open(path, "x", encoding="utf-8") as fh:   # new file, never overwrite
            fh.write(text)
        self.path = path
        self.bytes_written = len(text.encode("utf-8"))

    def _write_snapshot(self, data: dict, seq: int) -> int:
        path = os.path.join(self.dir, "snap-%010d.json.gz" % seq)
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as fh:
            json.dump(data, fh, separators=(",", ":"))
        self.bytes_written = os.path.getsize(path)
        return self.bytes_written

    def _replay(self, path: str, data: dict) -> None:
        with open(path, encoding="utf-8") as fh:
            head, body = fh.read().split("\n", 1)
        if json.loads(head)["len"] != len(body):
            return   # a torn segment is ignored, never half-applied
        delta = json.loads(body)
        objects = data.setdefault("objects", {})
        if delta["op"] == "merge":
            now = datetime.fromisoformat(delta["now"])
            cutoff = now - timedelta(days=delta["retention_days"])
            for oid, d in delta["objects"].items():
                obj = objects.setdefault(oid, {"samples": []})
                _apply(obj, d)
                _prune(obj, cutoff, delta["max_samples"])
        for oid in delta.get("deleted", []):
            objects.pop(oid, None)


# -- the delta: what one pull changed in one object ------------------------
_SCALARS = S._META_KEYS


def _fingerprint(obj: dict) -> dict:
    return {
        "samples": {S._stored_key(obj, s): len(s) for s in obj["samples"]},
        "meta": {k: obj.get(k) for k in _SCALARS},
        "elset": obj.get("elset"),
        "cands": dict(obj.get("elset_candidates") or {}),
        "prov": len(obj.get("provenance") or []),
    }


def _object_delta(obj: dict, before: dict) -> dict:
    d: dict = {}
    meta = {k: obj.get(k) for k in _SCALARS if obj.get(k) != before["meta"][k]}
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
    held = before["samples"]
    now_keys = set()
    upserts = []
    for s in obj["samples"]:
        key = S._stored_key(obj, s)
        now_keys.add(key)
        if held.get(key) != len(s):
            upserts.append(s)
    removed = [list(k) for k in held if k not in now_keys]
    if upserts:
        d["up"] = upserts
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
    by_key = {S._stored_key(obj, s): s for s in obj["samples"]}
    for k in d.get("rm", []):
        by_key.pop(tuple(k), None)
    for s in d.get("up", []):
        by_key[S._stored_key(obj, s)] = s
    obj["samples"] = list(by_key.values())


def _prune(obj: dict, cutoff: datetime, max_samples: int) -> None:
    merged = [s for s in obj["samples"] if S._parse(s["epoch"]) >= cutoff]
    merged.sort(key=lambda s: s["epoch"])
    obj["samples"] = merged[-max_samples:]


def _newest(data: dict) -> str | None:
    newest = None
    for rec in data.get("objects", {}).values():
        samples = rec.get("samples") or []
        if samples and (newest is None or samples[-1]["epoch"] > newest):
            newest = samples[-1]["epoch"]
    return newest
