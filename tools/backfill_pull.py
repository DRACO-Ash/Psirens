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
THREE JOBS from 1.7.4: a re-pull also TAGS each stored position with the
provider, data mode and marking of the record it came from, wherever the
re-pulled record reproduces the stored values exactly (`store._adopt_legacy`).
Before 1.7.4 the same re-pull enriched samples across providers, so the
minimum is now 1.7.4, not 1.6.17. Run `--check` first: it reads `/api/meta` and
refuses to start against an older deployment.

Nothing here is destructive. `/api/pull` merges; the store is anti-shrink and
never deletes a sample a pull happened not to include.

SIGN-IN (23 November 2026). The App Store puts a sign-in in front of the app,
so a script without a browser session gets the sign-in page, not PSIRENS. Copy
the session from a browser that has the app open: developer tools (F12),
Network, reload, click any `/api/meta` request, Request Headers, and copy the
whole value of the `Cookie` header into PSIRENS_COOKIE. Treat it like a
password: it is your signed-in session until it expires. Never paste it on the
command line, and never into `Read-Host` without masking: plain Read-Host
ECHOES it (FACT, 23 November 2026, a live session cookie ended up on screen
and in a screenshot that way). Use --ask-cookie, which prompts without echo:
    python backfill_pull.py --base https://<app-url> --ask-cookie --check
The token prompts without echo too (press Enter if the deployment sets none).

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
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("backfill_pull")

TOKEN_ENV = "PSIRENS_TOKEN"
# The platform puts a sign-in in front of the app. A browser carries the
# session as a cookie; this script has no browser, so it can carry the same
# cookie, copied from a signed-in browser (FACT, 23 November 2026: --check
# against the live host got a non-JSON page back, not PSIRENS). Read from the
# environment only, never printed, for the same reasons as the token.
COOKIE_ENV = "PSIRENS_COOKIE"
# Chunk size in hours: ONE upstream day per call. The old default (144) assumed
# one request per day; the live /api/meta of 23 November 2026 showed a 24 h
# refresh costs 33 upstream requests and ~423,000 rows because the truncation
# guard halves every full response. A 24 h chunk is exactly what the hourly
# refresh already completes; six of them in one request risks the gateway.
DEFAULT_CHUNK_H = 24
# `/api/pull` is behind the strict limiter: 6 calls per 60 seconds, keyed by
# client. Chunks take far longer than that to run, so this pause exists only to
# keep a fast failure path (every chunk erroring instantly) from tripping it.
MIN_GAP_S = 11.0
# FACT, 23 November 2026, live run against 1.7.8: one 24 h chunk outlasted a
# 300 s client timeout (the gateway held the request at least that long), the
# server kept working, and the next 54 chunks were each told "busy" 11 seconds
# apart and skipped. So a busy answer now WAITS for the server and retries the
# same window, and the default timeout is longer.
BUSY_WAIT_S = 30.0
DEFAULT_MAX_WAIT_MIN = 45.0
DEFAULT_TIMEOUT_S = 900.0
# The first build that retains raan_deg per sample and enriches a held sample
# on re-pull. Below this a backfill cannot fill the co-planar trail history.
MIN_ENRICH_VERSION = (1, 7, 4)   # provenance tagging on re-pull (1.7.4)


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
class NotPsirens(Exception):
    """Something answered, and it was not PSIRENS JSON. The message says what
    did answer, because "cannot reach" for a page that arrived is a lie."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects. Followed silently, a sign-in redirect turns into a
    200 carrying a login page, and the only symptom is a JSON parse error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def bare_url(url: str) -> str:
    """scheme://host/path only. A sign-in redirect's query carries state and
    sometimes tokens, so it is never printed."""
    parts = urllib.parse.urlsplit(str(url or ""))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def page_title(body: bytes) -> str:
    """The <title> of an HTML page, or "" : enough to name a sign-in page
    without printing its body."""
    match = re.search(rb"<title[^>]*>(.*?)</title>", body[:20000], re.I | re.S)
    if not match:
        return ""
    return " ".join(match.group(1).decode("utf-8", "replace").split())[:80]


def explain_http(code: int, location: str) -> str:
    """One line for an HTTP status, naming the likely fix."""
    if 300 <= code < 400:
        return ("HTTP %d redirect to %s: the platform sign-in answered, not "
                "PSIRENS. Set %s from a signed-in browser (see --help)."
                % (code, bare_url(location) or "an unstated address", COOKIE_ENV))
    if code == 401:
        return ("HTTP 401: refused. If the sign-in is satisfied, the team token "
                "is wrong or missing (%s)." % TOKEN_ENV)
    if code == 403:
        return "HTTP 403: signed in but not permitted (platform entitlement)."
    if code == 429:
        return "HTTP 429: rate limited (6 pulls per minute); re-run the window."
    return "HTTP %d" % code


def _request(url: str, token: str, method: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    cookie = os.environ.get(COOKIE_ENV, "").strip()
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise NotPsirens(explain_http(exc.code, exc.headers.get("Location", ""))) from exc
    with resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        final = resp.geturl()
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        title = page_title(body)
        raise NotPsirens("HTTP %s %s from %s%s: not PSIRENS JSON. Probably the "
                         "platform sign-in page; set %s (see --help)."
                         % (resp.status, ctype or "no content type", bare_url(final),
                            ' titled "%s"' % title if title else "", COOKIE_ENV)) from exc


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
    except NotPsirens as exc:
        row["detail"] = str(exc)
        return row
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        row["detail"] = "%s: %s" % (type(exc).__name__,
                                    getattr(exc, "reason", None) or exc)
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
    except NotPsirens as exc:
        print("REACHED %s BUT NOT PSIRENS: %s" % (bare_url(base), exc))
        return 2
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        print("CANNOT REACH %s: %s: %s" % (bare_url(base), type(exc).__name__,
                                           getattr(exc, "reason", None) or exc))
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


def pull_with_wait(pull: Any, sleep: Any, max_wait_s: float) -> Dict[str, Any]:
    """Call `pull()`; while the server answers "busy" (its single pull slot is
    held by an earlier chunk still running, or by the hourly refresh), wait
    and retry the SAME window rather than skipping it. Gives up after
    `max_wait_s` and reports the window as failed for a re-run."""
    waited = 0.0
    row = pull()
    while row.get("detail") == "busy" and waited < max_wait_s:
        sleep(BUSY_WAIT_S)
        waited += BUSY_WAIT_S
        row = pull()
    if waited:
        row["waited_s"] = waited
    return row


def run(base: str, token: str, mode: str, start: datetime, end: datetime,
        chunk_h: int, timeout: float, sleep: Any,
        max_wait_s: float = DEFAULT_MAX_WAIT_MIN * 60) -> Dict[str, Any]:
    windows = chunks(start, end, chunk_h)
    print("%d chunk(s) of up to %dh, %s to %s, mode %s"
          % (len(windows), chunk_h, iso_z(start), iso_z(end), mode))
    results = []
    for i, (c_start, c_end) in enumerate(windows, 1):
        if i > 1:
            sleep(MIN_GAP_S)
        began = time.monotonic()
        row = pull_with_wait(lambda s=c_start, e=c_end: pull_once(base, token, mode, s, e, timeout),
                             sleep, max_wait_s)
        row["seconds"] = round(time.monotonic() - began, 1)
        results.append(row)
        if row["ok"]:
            print("  %2d/%d  %s  added %-6s fetched %-6s %5.1fs"
                  % (i, len(windows), iso_z(c_start)[:10], row["added"],
                     row["fetched"], row["seconds"]))
        else:
            detail = row["detail"]
            if detail.startswith("TimeoutError"):
                detail += " (the server may still be working on it; the next chunk waits)"
            print("  %2d/%d  %s  FAILED: %s"
                  % (i, len(windows), iso_z(c_start)[:10], detail))
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
    check("1.7.4 tags", enriches_samples("1.7.4"), True)
    check("1.8.0 tags", enriches_samples("1.8.0"), True)
    check("1.7.3 does not (cross-provider enrichment)", enriches_samples("1.7.3"), False)
    check("1.6.17 does not", enriches_samples("1.6.17"), False)
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

    # Saying what answered (23 November 2026).
    check("a redirect query is never printed",
          bare_url("https://sso.example/oauth2/start?rd=x&state=secret"),
          "https://sso.example/oauth2/start")
    check("a redirect names the cookie fix",
          COOKIE_ENV in explain_http(302, "https://sso.example/login?x=1"), True)
    check("a redirect never prints its query",
          "x=1" in explain_http(302, "https://sso.example/login?x=1"), False)
    check("401 names the token", TOKEN_ENV in explain_http(401, ""), True)
    check("a page title is found", page_title(b"<html><TITLE> Sign in\n</TITLE>"),
          "Sign in")
    check("no title is empty", page_title(b"{}"), "")
    check("the default chunk is one upstream day", DEFAULT_CHUNK_H, 24)

    # Busy means wait, not skip (live run, 23 November 2026).
    answers = iter([{"detail": "busy"}, {"detail": "busy"}, {"ok": True, "added": 1}])
    naps: List[float] = []
    got = pull_with_wait(lambda: next(answers), naps.append, 600)
    check("busy is retried until the slot frees", got.get("ok"), True)
    check("it waited between tries", naps, [BUSY_WAIT_S, BUSY_WAIT_S])
    stuck = pull_with_wait(lambda: {"detail": "busy"}, naps.append, BUSY_WAIT_S * 2)
    check("a slot that never frees is reported", stuck.get("detail"), "busy")
    check("the default timeout outlasts the 300 s that failed live", DEFAULT_TIMEOUT_S > 300, True)

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
        return getpass.getpass("PSIRENS team token (not echoed; Enter if the "
                               "deployment sets none): ").strip()
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
    ap.add_argument("--max-wait-min", type=float, default=DEFAULT_MAX_WAIT_MIN,
                    help="minutes to wait on a busy server before giving up on a window")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="per-call HTTP timeout in seconds")
    ap.add_argument("--check", action="store_true",
                    help="report the deployment's version and ingest health, pull nothing")
    ap.add_argument("--ask-cookie", action="store_true",
                    help="prompt for the browser session cookie WITHOUT echo "
                         "(else it is read from %s)" % COOKIE_ENV)
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

    if args.ask_cookie:
        try:
            os.environ[COOKIE_ENV] = getpass.getpass(
                "Browser Cookie header value (not echoed): ").strip()
        except (EOFError, OSError):
            print("No terminal to prompt on; set %s instead." % COOKIE_ENV,
                  file=sys.stderr)
            return 2
    token = read_token(required=not args.check)
    if args.check:
        return check_build(args.base, token, args.timeout)

    start, end = build_window(args)
    report = run(args.base, token, args.mode, start, end,
                 args.chunk_hours, args.timeout, time.sleep, args.max_wait_min * 60)
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
