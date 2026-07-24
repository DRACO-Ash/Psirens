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
