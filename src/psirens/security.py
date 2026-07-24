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
