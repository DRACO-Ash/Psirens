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
from .models import DataMode
from .security import SingleFlight
from .sources import ElsetSource
from .store import Store

_log = logging.getLogger("psirens.refresh")


def _mode_of(rec: dict) -> DataMode:
    try:
        return DataMode(rec.get("data_mode", "REAL"))
    except ValueError:
        return DataMode.REAL


class Refresher:
    def __init__(self, cfg: Config, store: Store, sources: list[ElsetSource],
                 flight: SingleFlight, hrr=None):
        self.cfg = cfg
        self.store = store
        self.sources = sources
        self.flight = flight
        self.hrr = hrr
        self.last_run: datetime | None = None
        self.last_hrr: datetime | None = None
        self.last_added = 0

    def _maybe_refresh_hrr(self, now: datetime, force: bool) -> None:
        """Refresh the dynamic HRR list on its own slow cadence (or on force).
        Only pulls when UDL is enabled; offline keeps the static/last-known-good
        list. Never fatal to the refresh cycle."""
        if self.hrr is None or not self.cfg.udl_enabled:
            return
        stale = (self.last_hrr is None
                 or (now - self.last_hrr).total_seconds() >= self.cfg.hrr_refresh_seconds)
        if not (force or stale):
            return
        try:
            self.hrr.refresh(now)
            self.last_hrr = now
        except Exception as exc:  # never fatal
            _log.warning("HRR refresh errored (keeping last-known-good): %s", exc)

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

    def _ingest(self, start: datetime, now: datetime, *,
                modes: set[DataMode] | None, hrr_filter: bool,
                prune: bool) -> tuple[int, list[str]]:
        """Pull every source over [start, now] and merge, optionally restricted
        to a set of data modes and/or the HRR set. When prune is set and an HRR
        set is in play, drop REAL objects that fell off the list (simulated data
        is retained, see Store.retain_only)."""
        hrr_set = (self.hrr.geo_set()
                   if (self.hrr is not None and self.cfg.udl_enabled and hrr_filter)
                   else None)
        added = 0
        errors: list[str] = []
        for src in self.sources:
            try:
                incoming = src.fetch(start, now)
            except Exception as exc:  # never fatal to the loop
                errors.append(f"{type(src).__name__}: {exc}")
                _log.warning("source %s failed: %s", type(src).__name__, exc)
                continue
            if modes is not None:
                incoming = {k: v for k, v in incoming.items()
                            if _mode_of(v) in modes}
            if hrr_set is not None:
                incoming = {k: v for k, v in incoming.items() if k in hrr_set}
            if incoming:
                added += self.store.merge_samples(
                    incoming, retention_days=self.cfg.retention_days,
                    max_samples=self.cfg.max_samples_per_object, now=now)
        if prune and hrr_set is not None:
            self.store.retain_only(hrr_set)  # REAL-only prune; sim data kept
        return added, errors

    def run_once(self, now: datetime | None = None, force_hrr: bool = False) -> dict:
        """Automatic refresh: complete HRR list, then REAL elsets over the
        retention window, HRR-filtered and pruned. Returns a status dict."""
        if not self.flight.begin():
            return {"status": "busy"}
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(days=self.cfg.retention_days)
        try:
            self._maybe_refresh_hrr(now, force_hrr)  # list first; ingest filters on it
            added, errors = self._ingest(start, now, modes=None,
                                         hrr_filter=True, prune=True)
            self.last_run = now
            self.last_added = added
            hrr_set = self.hrr.geo_set() if (self.hrr and self.cfg.udl_enabled) else None
            return {"status": "ok", "added": added, "errors": errors,
                    "hrr_count": len(hrr_set) if hrr_set is not None else None}
        finally:
            self.flight.end()

    def pull_window(self, *, mode: str, start: datetime, end: datetime,
                    now: datetime | None = None) -> dict:
        """Operator-defined pull over an explicit [start, end] window. REAL and
        combined refresh the HRR list and pull REAL (HRR-filtered, pruned);
        SIMULATION pulls SIMULATED with no HRR filter and no prune, so a scenario
        picture stays clean and isolated from the live REAL belt. Retention is
        anchored to `end`, so a historical or future-dated scenario window is
        retained rather than pruned against wall-clock now."""
        if mode not in ("real", "sim", "combined"):
            return {"status": "error", "detail": f"unknown mode {mode!r}"}
        if not self.flight.begin():
            return {"status": "busy"}
        real_now = now or datetime.now(timezone.utc)
        added = 0
        errors: list[str] = []
        try:
            if mode in ("real", "combined"):
                self._maybe_refresh_hrr(real_now, True)  # HRR list is always current
                a, e = self._ingest(start, end, modes={DataMode.REAL},
                                    hrr_filter=True, prune=True)
                added += a
                errors += e
            if mode in ("sim", "combined"):
                a, e = self._ingest(start, end, modes={DataMode.SIMULATED},
                                    hrr_filter=False, prune=False)
                added += a
                errors += e
            self.last_run = real_now
            self.last_added = added
            return {"status": "ok", "mode": mode, "start": start.isoformat(),
                    "end": end.isoformat(), "added": added, "errors": errors}
        finally:
            self.flight.end()

    async def scheduler(self, stop: asyncio.Event) -> None:
        """Background loop. Seeds on the first tick then repeats on cadence, and
        runs each refresh off the event loop so app startup is never blocked by
        a slow source. A run failure never stops the loop."""
        first = True
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:  # pragma: no cover - belt and braces
                _log.exception("scheduled refresh errored")
            if first:
                first = False
                self._log_first_verdict()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.refresh_seconds)
            except asyncio.TimeoutError:
                pass

    def _log_first_verdict(self) -> None:
        """After the first refresh, say plainly whether UDL produced anything,
        naming the wire settings to check if it did not."""
        if not self.cfg.udl_enabled:
            return
        n = len(self.store.load().get("objects", {}))
        if n == 0:
            _log.warning(
                "UDL enabled but first refresh added 0 objects; the demo belt "
                "is OFF while UDL_BASE_URL is set, so the plot will be empty. "
                "Verify against the tenant: UDL_ELSET_PATH=%s, UDL_ACCEPT=%s, "
                "UDL_EPOCH_PARAM=%s, UDL_TARGET_FIELD=%s, and the UDL_USER/"
                "UDL_PASSWORD credentials.",
                self.cfg.udl_elset_path, self.cfg.udl_accept,
                self.cfg.udl_epoch_param, self.cfg.udl_target_field,
            )
        else:
            _log.info("UDL first refresh: %d objects in store", n)
