"""The refresh job: pull active sources, merge into the store single-flight.

A refresh is the one long, state-changing operation. It runs at most once at a
time; the hourly scheduler and an operator-triggered POST both go through the
same guard. It never raises out of the background loop: a source failure logs
and the store keeps its last-good state (integrity over freshness).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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


def _filtered(incoming: dict[str, dict], modes: set[DataMode] | None,
              hrr_set: set[str] | None) -> dict[str, dict]:
    """Apply the data-mode and high-interest filters, in that order."""
    out = incoming
    if modes is not None:
        out = {k: v for k, v in out.items() if _mode_of(v) in modes}
    if hrr_set is not None:
        out = {k: v for k, v in out.items() if k in hrr_set}
    return out


@dataclass
class IngestResult:
    """One refresh cycle, reported at every stage rather than only the last.

    `added` alone cannot tell three very different situations apart: the
    upstream returned nothing, the upstream returned plenty and the
    high-interest filter dropped all of it, or every upstream request was
    rejected. Live evidence, 26 September 2026: `last_ingest_added: 0` with
    `last_ingest_errors: []` and a 403-hour-old picture, which narrowed to
    nothing because the counts between the wire and the store were not kept.
    """

    added: int = 0
    errors: list[str] = field(default_factory=list)
    requests: int = 0           # upstream requests attempted
    request_failures: int = 0   # ...of which failed (never fatal, now never silent)
    rows: int = 0               # raw records returned by the upstream
    fetched: int = 0            # distinct objects offered, before any filter
    kept: int = 0               # ...surviving the mode and high-interest filters
    hrr_size: int | None = None


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
        # Ingest health. Kept because the scheduler used to discard everything
        # run_once returned, so a permanently failing pull produced no log
        # after the first tick, no banner and no API signal.
        self.last_errors: list[str] = []
        self.last_ok: datetime | None = None   # last run that added something
        # The stage-by-stage counts (1.6.9). Without them a zero is mute.
        self.last_ingest = IngestResult()

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
        # The short lookback, for the same reason as run_once: a 90-day sweep
        # on every manual injection is both wasteful and exposed to truncation.
        start = now - timedelta(hours=self.cfg.refresh_lookback_hours)
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

    @staticmethod
    def _absorb_stats(src: ElsetSource, res: IngestResult) -> None:
        """Fold a source's wire counters into the cycle result.

        A failed request is recorded as an ERROR here. It is still never fatal
        to the cycle, but a swallowed failure that reported no error is
        precisely what let a dead feed read as a quiet one.
        """
        stats = getattr(src, "last_stats", None)
        if stats is None:
            return
        res.requests += stats.requests
        res.request_failures += stats.failures
        res.rows += stats.rows
        if stats.failures:
            res.errors.append(f"{type(src).__name__}: {stats.summary()}")

    def _ingest_source(self, src: ElsetSource, start: datetime, now: datetime,
                       *, modes: set[DataMode] | None,
                       hrr_set: set[str] | None, res: IngestResult) -> None:
        """One source: pull, count at each stage, filter, merge."""
        incoming: dict[str, dict] = {}
        try:
            incoming = src.fetch(start, now)
        except Exception as exc:  # never fatal to the loop
            res.errors.append(f"{type(src).__name__}: {exc}")
            _log.warning("source %s failed: %s", type(src).__name__, exc)
        self._absorb_stats(src, res)
        res.fetched += len(incoming)
        kept = _filtered(incoming, modes, hrr_set)
        res.kept += len(kept)
        if kept:
            res.added += self.store.merge_samples(
                kept, retention_days=self.cfg.retention_days,
                max_samples=self.cfg.max_samples_per_object, now=now)

    def _ingest(self, start: datetime, now: datetime, *,
                modes: set[DataMode] | None, hrr_filter: bool,
                prune: bool) -> IngestResult:
        """Pull every source over [start, now] and merge, optionally restricted
        to a set of data modes and/or the HRR set. When prune is set and an HRR
        set is in play, drop REAL objects that fell off the list (simulated data
        is retained, see Store.retain_only)."""
        hrr_set = (self.hrr.geo_set()
                   if (self.hrr is not None and self.cfg.udl_enabled and hrr_filter)
                   else None)
        res = IngestResult(hrr_size=None if hrr_set is None else len(hrr_set))
        for src in self.sources:
            self._ingest_source(src, start, now, modes=modes,
                                hrr_set=hrr_set, res=res)
        if prune and hrr_set is not None:
            self.store.retain_only(hrr_set)  # REAL-only prune; sim data kept
        return res

    def run_once(self, now: datetime | None = None, force_hrr: bool = False) -> dict:
        """Automatic refresh: complete HRR list, then REAL elsets over the
        retention window, HRR-filtered and pruned. Returns a status dict."""
        if not self.flight.begin():
            return {"status": "busy"}
        now = now or datetime.now(timezone.utc)
        # A SHORT OVERLAP, not the retention window. Asking for 90 days of
        # every object in one hourly request is what produced a permanently
        # truncated response; the store is cumulative, so a rolling lookback
        # that comfortably covers the refresh cadence is all this needs.
        start = now - timedelta(hours=self.cfg.refresh_lookback_hours)
        try:
            self._maybe_refresh_hrr(now, force_hrr)  # list first; ingest filters on it
            res = self._ingest(start, now, modes=None,
                               hrr_filter=True, prune=True)
            self.last_run = now
            self.last_added = res.added
            self.last_errors = list(res.errors)
            self.last_ingest = res
            if res.added > 0:
                self.last_ok = now
            return {"status": "ok", "added": res.added, "errors": res.errors,
                    "hrr_count": res.hrr_size, "fetched": res.fetched,
                    "kept": res.kept, "rows": res.rows,
                    "requests": res.requests,
                    "request_failures": res.request_failures}
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
        fetched = 0
        try:
            if mode in ("real", "combined"):
                self._maybe_refresh_hrr(real_now, True)  # HRR list is always current
                res = self._ingest(start, end, modes={DataMode.REAL},
                                   hrr_filter=True, prune=True)
                added += res.added
                errors += res.errors
                fetched += res.fetched
            if mode in ("sim", "combined"):
                res = self._ingest(start, end, modes={DataMode.SIMULATED},
                                   hrr_filter=False, prune=False)
                added += res.added
                errors += res.errors
                fetched += res.fetched
            self.last_run = real_now
            self.last_added = added
            self.last_errors = list(errors)
            if added > 0:
                self.last_ok = real_now
            return {"status": "ok", "mode": mode, "start": start.isoformat(),
                    "end": end.isoformat(), "added": added, "errors": errors,
                    "fetched": fetched}
        finally:
            self.flight.end()

    async def scheduler(self, stop: asyncio.Event) -> None:
        """Background loop. Seeds on the first tick then repeats on cadence, and
        runs each refresh off the event loop so app startup is never blocked by
        a slow source. A run failure never stops the loop."""
        first = True
        while not stop.is_set():
            try:
                self.report(await asyncio.to_thread(self.run_once))
            except Exception:  # pragma: no cover - belt and braces
                _log.exception("scheduled refresh errored")
            if first:
                first = False
                self._log_first_verdict()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.refresh_seconds)
            except asyncio.TimeoutError:
                pass

    def staleness_hours(self, now: datetime | None = None) -> float | None:
        """Age of the newest fix held, in hours, or None when the store is
        empty. The number the alarm is built on."""
        newest = self.store.newest_epoch()
        if not newest:
            return None
        try:
            when = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - when).total_seconds() / 3600.0

    def is_stale(self, now: datetime | None = None) -> bool:
        age = self.staleness_hours(now)
        return age is not None and age > self.cfg.stale_after_hours

    def health(self, now: datetime | None = None) -> dict:
        """Everything needed to tell a working feed from a frozen one.

        The counts between the wire and the store are here on purpose. Added
        alone is ambiguous: `last_ingest_requests` against
        `last_ingest_request_failures` says whether the upstream answered at
        all, and `last_ingest_fetched` against `last_ingest_kept` says whether
        what it sent was then filtered away by the high-interest list.
        """
        age = self.staleness_hours(now)
        res = self.last_ingest
        return {
            "last_refresh": self.last_run.isoformat() if self.last_run else None,
            "last_ingest_added": self.last_added,
            "last_ingest_errors": list(self.last_errors),
            "last_successful_ingest": self.last_ok.isoformat() if self.last_ok else None,
            "last_ingest_requests": res.requests,
            "last_ingest_request_failures": res.request_failures,
            "last_ingest_rows": res.rows,
            "last_ingest_fetched": res.fetched,
            "last_ingest_kept": res.kept,
            "hrr_list_size": res.hrr_size,
            "newest_sample_epoch": self.store.newest_epoch(),
            "data_age_hours": None if age is None else round(age, 2),
            "stale_after_hours": self.cfg.stale_after_hours,
            "stale": self.is_stale(now),
            "ingest_diagnosis": self.diagnosis(),
        }

    def diagnosis(self) -> str:
        """One line naming the most likely reason the feed is dry.

        Written for the operator, not the log reader: the interface shows it
        beside the staleness banner so the answer to "why is this old?" does
        not require a container shell.
        """
        res = self.last_ingest
        if res.request_failures:
            return ("upstream requests are failing: " +
                    (res.errors[0] if res.errors else "reason not recorded"))
        if res.requests == 0 and res.fetched == 0:
            return "no upstream pull has completed since start-up"
        if res.fetched == 0:
            return "the upstream answered but returned no records for the window"
        if res.kept == 0:
            return ("records arrived but none matched the high-interest list "
                    f"({res.hrr_size} objects)")
        return "records arrived and were merged; no new epochs in the window"

    def report(self, result: dict, now: datetime | None = None) -> None:
        """Say out loud what every refresh did. Not only the first.

        A silent ingest failure is worse than a loud one: the interface keeps
        serving the last-good picture, which is the correct behaviour, and
        nothing else distinguishes it from a live feed.
        """
        if result.get("status") != "ok":
            _log.warning("refresh did not run: %s", result.get("status"))
            return
        added, errors = result.get("added", 0), result.get("errors") or []
        age = self.staleness_hours(now)
        age_txt = "unknown" if age is None else f"{age:.1f}h"
        if errors:
            _log.warning("refresh added %d samples with %d source error(s): %s",
                         added, len(errors), "; ".join(errors[:3]))
        if self.is_stale(now):
            _log.warning(
                "STALE DATA: newest fix is %s old, past the %.1fh threshold. "
                "The picture on screen is last-known-good, not current. "
                "Likely cause: %s. Counts this cycle: %d request(s), %d "
                "failed, %d row(s), %d object(s) offered, %d kept after "
                "filtering. Window settings UDL_SLICE_HOURS=%s, "
                "REFRESH_LOOKBACK_HOURS=%s.",
                age_txt, self.cfg.stale_after_hours, self.diagnosis(),
                self.last_ingest.requests, self.last_ingest.request_failures,
                self.last_ingest.rows, self.last_ingest.fetched,
                self.last_ingest.kept, self.cfg.udl_slice_hours,
                self.cfg.refresh_lookback_hours)
            return
        _log.info("refresh ok: +%d samples, newest fix %s old", added, age_txt)

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
