#!/usr/bin/env python3
"""Drive a PSIRENS backfill over `POST /api/pull`, in chunks the gateway survives.

WHY THIS EXISTS, and it is not convenience. `POST /api/pull` runs the whole
window SYNCHRONOUSLY inside the request: `UDLElsetSource.fetch` slices the
window into `UDL_SLICE_HOURS` (24) chunks with a `UDL_SLICE_PAUSE_SECONDS` (2)
pause between them, and halves and retries any slice that comes back at or over
the truncation threshold. A 90-day backfill is therefore at least 90 upstream
requests and 178 seconds of deliberate pausing before any network time at all,
held open on one HTTP connection. Platform ingresses commonly close an idle
response well before that, and the operator is then left unable to tell a
timed-out proxy from a failed pull.

So this walks the window in SHORT pulls, each comfortably inside a normal
gateway timeout, and reports what every one of them added. If a chunk fails the
rest still run, and the summary names the chunks that did not land so they can
be re-run on their own.

TWO JOBS, one pass. Beyond closing a feed outage it also RETRO-FILLS fields the
stored samples are missing: from 1.6.17 `Store._enrich_sample` fills a field an
already-held sample lacks on a re-pull without overwriting anything it has. A
sample written before 1.6.17 carries no `raan_deg`, and the co-planar trail
needs one per sample, so re-pulling the retention window is what turns the
trails on across the existing history instead of growing them from nothing.
THAT ONLY WORKS AGAINST A BUILD OF 1.6.17 OR LATER. Run `--check` first: it
reads `/api/meta` and refuses to start against an older deployment, because the
pull would otherwise have to be run twice.

Nothing here is destructive. `/api/pull` merges; the store is anti-shrink and
never deletes a sample a pull happened not to include.

Usage
    export PSIRENS_TOKEN=...                  # never passed on the command line
    python3 backfill_pull.py --base https://<app-url> --check
    python3 backfill_pull.py --base https://<app-url> --days 90
    python3 backfill_pull.py --base https://<app-url> \
        --start 2026-11-08T00:00:00Z --end 2026-11-10T12:00:00Z
    python3 backfill_pull.py --self-test      # no network, no token

The token is read from PSIRENS_TOKEN (or prompted for, without echo) and is
never logged, printed, or written to the report.

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("backfill_pull")

TOKEN_ENV = "PSIRENS_TOKEN"
# Chunk size in hours. Six slices per call at UDL_SLICE_HOURS=24, so ten
# seconds of deliberate pausing plus six upstream requests: minutes of headroom
# under a 60-second gateway timeout even on a slow tenant.
DEFAULT_CHUNK_H = 144
# `/api/pull` is behind the strict limiter: 6 calls per 60 seconds, keyed by
# client. Chunks take far longer than that to run, so this pause exists only to
# keep a fast failure path (every chunk erroring instantly) from tripping it.
MIN_GAP_S = 11.0
# The first build that retains raan_deg per sample and enriches a held sample
# on re-pull. Below this a backfill cannot fill the co-planar trail history.
MIN_ENRICH_VERSION = (1, 6, 17)


# --------------------------------------------------------------------------
# Pure helpers (all covered by --self-test)
# --------------------------------------------------------------------------
def parse_version(raw: str) -> Optional[Tuple[int, ...]]:
    """'1.6.18' -> (1, 6, 18). None when it is not a plain dotted number."""
    parts = str(raw or "").strip().split(".")
    if not parts or any(not p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def enriches_samples(version: str) -> bool:
    """True when this build fills a missing field on a re-pull of a held epoch."""
    parsed = parse_version(version)
    return parsed is not None and parsed >= MIN_ENRICH_VERSION


def parse_dt(raw: str) -> datetime:
    """Accept the trailing-Z form as well as a plain offset. UTC always."""
    text = str(raw).strip().replace("Z", "+00:00")
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def iso_z(when: datetime) -> str:
    """The form `_parse_dt` on the server accepts, seconds resolution."""
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def chunks(start: datetime, end: datetime, hours: int) -> List[Tuple[datetime, datetime]]:
    """Split [start, end] into windows of at most `hours`, oldest FIRST.

    Oldest first on purpose: the newest fixes are the ones an operator is
    waiting on, so if the run is interrupted the remaining gap is the oldest
    and least urgent part of the window, not the most recent.
    """
    if end <= start:
        return []
    step = timedelta(hours=max(1, hours))
    out = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + step, end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


def summarise(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals plus the windows that did not land, for a targeted re-run."""
    added = sum(int(r.get("added") or 0) for r in results if r.get("ok"))
    fetched = sum(int(r.get("fetched") or 0) for r in results if r.get("ok"))
    failed = [r for r in results if not r.get("ok")]
    errors: List[str] = []
    for r in results:
        errors.extend(r.get("errors") or [])
    return {"chunks": len(results), "ok": len(results) - len(failed),
            "added": added, "fetched": fetched,
            "failed_windows": [(r["start"], r["end"]) for r in failed],
            "upstream_errors": errors}


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------
def _request(url: str, token: str, method: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_meta(base: str, token: str, timeout: float) -> Dict[str, Any]:
    return _request(base.rstrip("/") + "/api/meta", token, "GET", timeout)


def pull_once(base: str, token: str, mode: str, start: datetime, end: datetime,
              timeout: float) -> Dict[str, Any]:
    """One `/api/pull` call. Never raises: a failed chunk is data, not a stop."""
    query = urllib.parse.urlencode({"mode": mode, "start": iso_z(start),
                                    "end": iso_z(end)})
    url = base.rstrip("/") + "/api/pull?" + query
    row: Dict[str, Any] = {"start": iso_z(start), "end": iso_z(end), "ok": False}
    try:
        body = _request(url, token, "POST", timeout)
    except urllib.error.HTTPError as exc:
        row["detail"] = "HTTP %s" % exc.code
        return row
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        row["detail"] = type(exc).__name__
        return row
    status = body.get("status")
    if status != "ok":
        # "busy" means the scheduler or another operator holds the single-flight
        # lock. Reported, not retried: a silent retry would hide a second pull.
        row["detail"] = str(status or "no status")
        return row
    row.update({"ok": True, "added": body.get("added", 0),
                "fetched": body.get("fetched", 0),
                "errors": body.get("errors") or []})
    return row


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def check_build(base: str, token: str, timeout: float) -> int:
    """Report the deployment's version and whether a backfill can fill RAAN."""
    try:
        meta = fetch_meta(base, token, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            TimeoutError, ValueError) as exc:
        print("CANNOT REACH %s: %s" % (base, type(exc).__name__))
        return 2
    version = str(meta.get("version", ""))
    print("version            %s" % (version or "not reported"))
    print("newest_sample_epoch %s" % meta.get("newest_sample_epoch"))
    print("data_age_hours      %s" % meta.get("data_age_hours"))
    print("stale               %s" % meta.get("stale"))
    print("ingest_diagnosis    %s" % meta.get("ingest_diagnosis"))
    if enriches_samples(version):
        print("\nOK: this build retains raan_deg per sample and enriches a held "
              "sample on re-pull, so one backfill closes the gap AND fills the "
              "co-planar trail history.")
        return 0
    print("\nSTOP: this build is older than %s, so a re-pull of an epoch it "
          "already holds is DISCARDED and no raan_deg is filled. A backfill now "
          "would have to be run again after the upgrade. Upload the newer build "
          "first." % ".".join(str(n) for n in MIN_ENRICH_VERSION))
    return 1


def run(base: str, token: str, mode: str, start: datetime, end: datetime,
        chunk_h: int, timeout: float, sleep: Any) -> Dict[str, Any]:
    windows = chunks(start, end, chunk_h)
    print("%d chunk(s) of up to %dh, %s to %s, mode %s"
          % (len(windows), chunk_h, iso_z(start), iso_z(end), mode))
    results = []
    for i, (c_start, c_end) in enumerate(windows, 1):
        if i > 1:
            sleep(MIN_GAP_S)
        began = time.monotonic()
        row = pull_once(base, token, mode, c_start, c_end, timeout)
        row["seconds"] = round(time.monotonic() - began, 1)
        results.append(row)
        if row["ok"]:
            print("  %2d/%d  %s  added %-6s fetched %-6s %5.1fs"
                  % (i, len(windows), iso_z(c_start)[:10], row["added"],
                     row["fetched"], row["seconds"]))
        else:
            print("  %2d/%d  %s  FAILED: %s"
                  % (i, len(windows), iso_z(c_start)[:10], row["detail"]))
    return {"summary": summarise(results), "chunks": results}


def self_test() -> int:
    """Offline checks of every pure helper. No network, no token."""
    failures: List[str] = []
    ran = []

    def check(label: str, got: Any, want: Any) -> None:
        # The count is COUNTED, not written down. A self-test that reports a
        # hardcoded total still says "OK" after half of it is deleted.
        ran.append(label)
        if got != want:
            failures.append("%s: got %r, want %r" % (label, got, want))

    check("version parse", parse_version("1.6.18"), (1, 6, 18))
    check("version parse rejects text", parse_version("1.6.18-rc"), None)
    check("version parse rejects empty", parse_version(""), None)
    check("1.6.18 enriches", enriches_samples("1.6.18"), True)
    check("1.6.17 enriches", enriches_samples("1.6.17"), True)
    check("1.6.12 does not", enriches_samples("1.6.12"), False)
    check("1.7.0 enriches", enriches_samples("1.7.0"), True)
    check("unreported version does not", enriches_samples(""), False)

    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    check("iso has no microseconds", iso_z(base), "2026-11-01T00:00:00+00:00")
    check("trailing Z parses", parse_dt("2026-11-01T00:00:00Z"), base)
    check("naive is treated as UTC", parse_dt("2026-11-01T00:00:00"), base)

    week = chunks(base, base + timedelta(days=7), 48)
    check("chunk count", len(week), 4)
    check("chunks start at the window start", week[0][0], base)
    check("chunks end at the window end", week[-1][1], base + timedelta(days=7))
    check("chunks are contiguous",
          [w[0] for w in week[1:]], [w[1] for w in week[:-1]])
    check("oldest first", week[0][0] < week[-1][0], True)
    check("empty window", chunks(base, base, 24), [])
    check("reversed window", chunks(base + timedelta(days=1), base, 24), [])
    check("short window is one chunk", len(chunks(base, base + timedelta(hours=3), 24)), 1)

    rows = [{"ok": True, "added": 5, "fetched": 9, "errors": [], "start": "a", "end": "b"},
            {"ok": False, "detail": "HTTP 401", "start": "c", "end": "d"},
            {"ok": True, "added": 2, "fetched": 4, "errors": ["x"], "start": "e", "end": "f"}]
    summary = summarise(rows)
    check("added totals only successful chunks", summary["added"], 7)
    check("fetched totals only successful chunks", summary["fetched"], 13)
    check("ok count", summary["ok"], 2)
    check("failed windows are named", summary["failed_windows"], [("c", "d")])
    check("upstream errors are collected", summary["upstream_errors"], ["x"])

    if failures:
        for line in failures:
            print("FAIL " + line)
        return 1
    print("SELF-TEST OK (%d checks)" % len(ran))
    return 0


def read_token(required: bool) -> str:
    """The token comes from the environment, or an unechoed prompt.

    Never a command-line argument: that would put it in the shell history and
    in the process list for anyone running `ps`.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token or not required:
        return token
    try:
        return getpass.getpass("PSIRENS team token (not echoed): ").strip()
    except (EOFError, OSError):
        # No terminal to prompt on: a cron job, a pipe, a CI step. Say what is
        # missing rather than dying in a traceback.
        print("No %s in the environment and no terminal to prompt on. "
              "Set %s and re-run." % (TOKEN_ENV, TOKEN_ENV), file=sys.stderr)
        raise SystemExit(2)


def build_window(args: argparse.Namespace) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if args.start and args.end:
        return parse_dt(args.start), parse_dt(args.end)
    return now - timedelta(days=args.days), now


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", help="PSIRENS base URL, e.g. https://psirens...")
    ap.add_argument("--mode", default="real", choices=("real", "sim", "combined"))
    ap.add_argument("--days", type=int, default=90,
                    help="relative lookback when --start/--end are not given")
    ap.add_argument("--start", help="absolute window start (ISO, Z accepted)")
    ap.add_argument("--end", help="absolute window end (ISO, Z accepted)")
    ap.add_argument("--chunk-hours", type=int, default=DEFAULT_CHUNK_H,
                    help="hours per /api/pull call (default %d)" % DEFAULT_CHUNK_H)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="per-call HTTP timeout in seconds")
    ap.add_argument("--check", action="store_true",
                    help="report the deployment's version and ingest health, pull nothing")
    ap.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    ap.add_argument("--self-test", action="store_true",
                    help="offline checks of the pure helpers; no network, no token")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.self_test:
        return self_test()
    if not args.base:
        ap.error("--base is required unless --self-test is given")
    if bool(args.start) != bool(args.end):
        ap.error("--start and --end must both be given, or neither")

    token = read_token(required=not args.check)
    if args.check:
        return check_build(args.base, token, args.timeout)

    start, end = build_window(args)
    report = run(args.base, token, args.mode, start, end,
                 args.chunk_hours, args.timeout, time.sleep)
    s = report["summary"]
    print("\n%d/%d chunk(s) ok, %d sample(s) added, %d object-record(s) fetched"
          % (s["ok"], s["chunks"], s["added"], s["fetched"]))
    for window in s["failed_windows"]:
        print("  re-run: --start %s --end %s" % window)
    for err in s["upstream_errors"]:
        print("  upstream: %s" % err)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("report written to %s" % args.json)
    return 0 if s["ok"] == s["chunks"] else 1


if __name__ == "__main__":
    sys.exit(main())
