#!/usr/bin/env python3
"""Probe /udl/elset for the facts PSIRENS 1.5.0 relies on but cannot verify offline.

PSIRENS now serves the provider's native `line1`/`line2` in place of a
reconstructed TLE. That change rests on four assumptions about the tenant feed
which were established from a prior environment and cannot be re-checked from a
sandbox with no UDL egress. This probe checks all four against live data and
prints a verdict per assumption:

  A1  native lines are present on close to 100% of elset records;
  A2  the catalogue number embedded in a native line matches the record's satNo
      (this is the cross-check PSIRENS enforces before serving a line);
  A3  the `source` values on the feed all classify under the selection table in
      `psirens.tle`, so no provider silently falls into `unknown`;
  A4  the `classificationMarking` values are what the copy-out gate expects, so
      the proportion of records with the copy control withheld is known rather
      than guessed.

It also reports how often the optional fields PSIRENS captures (`revNo`,
`meanMotionDot`, `meanMotionDDot`, `ephemType`) are actually populated.

Read-only: it issues GET requests and writes nothing back to UDL.

Usage
    python3 udl_elset_probe.py --hours 6
    python3 udl_elset_probe.py --hours 24 --json report.json
    python3 udl_elset_probe.py --self-test          # no network, no credentials

Credentials are read from ~/.config/phase_offset/credentials.ini, section
[udl], keys `user` and `password`, with an interactive prompt as fallback.
They are never logged, printed, or written to the report.

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import getpass
import json
import logging
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("udl_elset_probe")

DEFAULT_BASE = "https://unifieddatalibrary.com"
ELSET_PATH = "/udl/elset"
CREDENTIALS_PATH = "~/.config/phase_offset/credentials.ini"

# Mirrors psirens.tle._GOVERNMENT_MARKERS / _COMMERCIAL_MARKERS. Kept as a
# literal copy rather than an import so this file stays single-file and
# dependency-free; if the application table changes, change this one too.
GOVERNMENT_MARKERS = (
    "18 SPCS", "18TH SPCS", "SPCS", "18 SDS", "18SDS", "19 SDS", "19SDS",
    "USSF", "SPACE FORCE", "AFSPC", "SPADOC", "SPACETRACK", "SPACE-TRACK",
    "JCO",
)
COMMERCIAL_MARKERS = (
    "CLOUDSTONE", "NORTHSTAR", "NORTH STAR", "EXO", "KBR", "KRTL",
    "LEOLABS", "LEO LABS",
)

# A1 is reported as met at or above this share of records carrying a line.
NATIVE_PRESENCE_TARGET = 0.95


# --------------------------------------------------------------------------
# Pure helpers (all covered by --self-test)
# --------------------------------------------------------------------------
def classify_source(source: Optional[str]) -> str:
    """Map a UDL `source` onto the PSIRENS selection class."""
    name = (source or "").strip().upper()
    if not name:
        return "unknown"
    for marker in GOVERNMENT_MARKERS:
        if marker in name:
            return "government"
    for marker in COMMERCIAL_MARKERS:
        if marker in name:
            return "commercial"
    return "unknown"


def tle_checksum(line: str) -> int:
    """TLE mod-10 checksum over the first 68 columns."""
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def checksum_ok(line: str) -> bool:
    tail = line[68:69]
    return tail.isdigit() and int(tail) == tle_checksum(line)


def alpha5_to_int(field: str) -> Optional[int]:
    """Decode a five-character catalogue field, Alpha-5 aware (A=10, no I/O)."""
    text = field.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    head, rest = text[0].upper(), text[1:]
    if not rest.isdigit() or not head.isalpha():
        return None
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    if head not in alphabet:
        return None
    return (alphabet.index(head) + 10) * 10000 + int(rest)


def is_copyable(marking: Optional[str]) -> bool:
    """Whether PSIRENS would offer the copy control for this marking."""
    text = (marking or "").strip().upper()
    if not text:
        return False
    parts = [p.strip() for p in text.split("//") if p.strip()]
    if not parts or parts[0] not in ("U", "UNCLASSIFIED"):
        return False
    return len(parts) == 1


def udl_ts(dt: datetime) -> str:
    """The trailing-Z microsecond form the UDL epoch filter requires. A
    '+00:00' offset returns 200 and matches nothing (LEARNED register)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def line_verdict(row: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """Classify one record's native line pair.

    Returns (verdict, claimed_satnum) where verdict is one of:
    `absent`, `malformed`, `bad_checksum`, `satno_mismatch`, `ok`.
    """
    l1, l2 = row.get("line1"), row.get("line2")
    if not isinstance(l1, str) or not isinstance(l2, str) or not l1 or not l2:
        return "absent", None
    l1, l2 = l1.rstrip(), l2.rstrip()
    if len(l1) != 69 or len(l2) != 69 or not l1.startswith("1 ") or not l2.startswith("2 "):
        return "malformed", None
    if not checksum_ok(l1) or not checksum_ok(l2):
        return "bad_checksum", alpha5_to_int(l1[2:7])
    claimed = alpha5_to_int(l1[2:7])
    expected = alpha5_to_int(str(row.get("satNo") or "").strip())
    if claimed is None or (expected is not None and claimed != expected):
        return "satno_mismatch", claimed
    return "ok", claimed


# --------------------------------------------------------------------------
# Credentials and transport
# --------------------------------------------------------------------------
def load_credentials(path: str = CREDENTIALS_PATH) -> Tuple[str, str]:
    """Read [udl] user/password, prompting if the file is absent or partial.

    interpolation=None so a '%' in a password survives verbatim.
    """
    parser = configparser.ConfigParser(interpolation=None)
    resolved = os.path.expanduser(path)
    user = password = ""
    try:
        if parser.read(resolved) and parser.has_section("udl"):
            user = parser.get("udl", "user", fallback="")
            password = parser.get("udl", "password", fallback="")
    except (configparser.Error, OSError) as exc:
        LOG.warning("could not read %s (%s); falling back to prompt", path, exc)
    if not user:
        user = input("UDL username: ").strip()
    if not password:
        password = getpass.getpass("UDL password: ")
    if not user or not password:
        raise SystemExit("no UDL credentials supplied")
    return user, password


def fetch_elsets(base: str, user: str, password: str, *, hours: int,
                 timeout: int = 60) -> List[Dict[str, Any]]:
    """GET one bounded epoch window of /udl/elset records."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    query = urllib.parse.urlencode({"epoch": udl_ts(start) + ".." + udl_ts(end)})
    url = base.rstrip("/") + ELSET_PATH + "?" + query
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic " + token,
        "Accept": "application/json",
    })
    LOG.info("GET %s%s (window %dh)", base.rstrip("/"), ELSET_PATH, hours)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"UDL returned HTTP {exc.code} ({exc.reason})") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach UDL: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"UDL response was not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemExit(f"expected a JSON list, got {type(payload).__name__}")
    return payload


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def analyse(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Reduce a window of elset records to the four assumption verdicts."""
    total = len(rows)
    verdicts: Counter = Counter()
    sources: Counter = Counter()
    classes: Counter = Counter()
    markings: Counter = Counter()
    optional: Counter = Counter()
    mismatches: List[Dict[str, Any]] = []
    copyable = 0

    for row in rows:
        verdict, claimed = line_verdict(row)
        verdicts[verdict] += 1
        if verdict == "satno_mismatch" and len(mismatches) < 20:
            mismatches.append({"satNo": row.get("satNo"), "claimed": claimed,
                               "source": row.get("source")})
        source = str(row.get("source") or "")
        sources[source] += 1
        classes[classify_source(source)] += 1
        marking = str(row.get("classificationMarking") or "")
        markings[marking] += 1
        if is_copyable(marking):
            copyable += 1
        for field in ("revNo", "meanMotionDot", "meanMotionDDot", "ephemType"):
            if row.get(field) not in (None, ""):
                optional[field] += 1

    present = total - verdicts["absent"]
    usable = verdicts["ok"]
    share = (present / total) if total else 0.0
    unknown_sources = sorted({s for s in sources if classify_source(s) == "unknown"})

    return {
        "records": total,
        "A1_native_present": {
            "present": present, "share": round(share, 4),
            "target": NATIVE_PRESENCE_TARGET,
            "met": bool(total) and share >= NATIVE_PRESENCE_TARGET,
        },
        "A2_satno_crosscheck": {
            "usable": usable,
            "share_of_present": round(usable / present, 4) if present else 0.0,
            "malformed": verdicts["malformed"],
            "bad_checksum": verdicts["bad_checksum"],
            "satno_mismatch": verdicts["satno_mismatch"],
            "met": present > 0 and verdicts["satno_mismatch"] == 0,
            "examples": mismatches,
        },
        "A3_source_classes": {
            "by_class": dict(classes),
            "by_source": dict(sources.most_common()),
            "unclassified_sources": unknown_sources,
            "met": not unknown_sources,
        },
        "A4_markings": {
            "by_marking": dict(markings.most_common()),
            "copyable": copyable,
            "withheld": total - copyable,
            "share_withheld": round((total - copyable) / total, 4) if total else 0.0,
            "met": total > 0,
        },
        "optional_fields": {k: optional[k] for k in
                            ("revNo", "meanMotionDot", "meanMotionDDot", "ephemType")},
    }


def render(report: Dict[str, Any]) -> None:
    """Print the verdicts in the order the assumptions were stated."""
    def mark(ok: bool) -> str:
        return "MET    " if ok else "NOT MET"

    a1, a2, a3, a4 = (report["A1_native_present"], report["A2_satno_crosscheck"],
                      report["A3_source_classes"], report["A4_markings"])
    print(f"\nrecords in window: {report['records']}\n")
    print(f"A1 {mark(a1['met'])} native lines on {a1['present']} records "
          f"({a1['share'] * 100:.1f}%, target {a1['target'] * 100:.0f}%)")
    print(f"A2 {mark(a2['met'])} satNo cross-check: {a2['usable']} usable, "
          f"{a2['satno_mismatch']} mismatched, {a2['bad_checksum']} bad checksum, "
          f"{a2['malformed']} malformed")
    for ex in a2["examples"]:
        print(f"        mismatch: satNo={ex['satNo']} line claims {ex['claimed']} "
              f"(source {ex['source']})")
    print(f"A3 {mark(a3['met'])} source classes: {a3['by_class']}")
    if a3["unclassified_sources"]:
        print(f"        unclassified sources (tell Claude to add these): "
              f"{a3['unclassified_sources']}")
    print(f"A4 {mark(a4['met'])} copy gate: {a4['copyable']} copyable, "
          f"{a4['withheld']} withheld ({a4['share_withheld'] * 100:.1f}%)")
    for marking, count in list(a4["by_marking"].items())[:12]:
        state = "copyable" if is_copyable(marking) else "withheld"
        print(f"        {marking or '(blank)':<28} {count:>7}  {state}")
    print(f"\noptional fields populated: {report['optional_fields']}\n")


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def _case(tid: str, desc: str, inputs: Any, expected: Any, observed: Any) -> Dict[str, Any]:
    return {"test_id": tid, "assertion": desc, "inputs": inputs,
            "expected": expected, "observed": observed,
            "status": "PASS" if observed == expected else "FAIL",
            "timestamp": datetime.now(timezone.utc).isoformat()}


def self_test() -> int:
    """Offline assertions over the pure helpers; emits a JSON manifest."""
    # A genuine checksum-correct pair (exporter-produced) and its mutations.
    ok1 = "1 41836U          26218.00000000  .00000000  00000-0  10000-4 0    10"
    ok2 = "2 41836   0.0500  80.0000 0002000  90.0000 100.0000  1.00273790    08"
    a51 = "1 A0172U          26218.00000000  .00000000  00000-0  10000-4 0    18"
    a52 = "2 A0172   0.0500  80.0000 0002000  90.0000 100.0000  1.00273790    06"
    bad1 = ok1[:68] + str((int(ok1[68]) + 1) % 10)

    cases = [
        _case("T01", "exporter line checksums clean", ok1, True, checksum_ok(ok1)),
        _case("T02", "mutated checksum is rejected", bad1, False, checksum_ok(bad1)),
        _case("T03", "Alpha-5 A0172 decodes to 100172", "A0172", 100172,
              alpha5_to_int("A0172")),
        _case("T04", "plain five-digit field decodes as itself", "41836", 41836,
              alpha5_to_int("41836")),
        _case("T05", "matching line and satNo verdict is ok",
              {"satNo": "41836"}, ("ok", 41836),
              line_verdict({"satNo": "41836", "line1": ok1, "line2": ok2})),
        _case("T06", "line for another object is a satno_mismatch",
              {"satNo": "28924"}, ("satno_mismatch", 41836),
              line_verdict({"satNo": "28924", "line1": ok1, "line2": ok2})),
        _case("T07", "absent lines report absent", {"satNo": "41836"},
              ("absent", None), line_verdict({"satNo": "41836"})),
        _case("T08", "short line reports malformed", {"satNo": "41836"},
              ("malformed", None),
              line_verdict({"satNo": "41836", "line1": ok1[:40], "line2": ok2})),
        _case("T09", "bad checksum reported as bad_checksum", {"satNo": "41836"},
              ("bad_checksum", 41836),
              line_verdict({"satNo": "41836", "line1": bad1, "line2": ok2})),
        _case("T10", "Alpha-5 record validates against six-digit satNo",
              {"satNo": "100172"}, ("ok", 100172),
              line_verdict({"satNo": "100172", "line1": a51, "line2": a52})),
        _case("T11", "18SDS classifies as government", "18SDS", "government",
              classify_source("18SDS")),
        _case("T12", "LeoLabs classifies as commercial", "LeoLabs", "commercial",
              classify_source("LeoLabs")),
        _case("T13", "unrecognised source is unknown", "NEWFEED", "unknown",
              classify_source("NEWFEED")),
        _case("T14", "plain U is copyable", "U", True, is_copyable("U")),
        _case("T15", "proprietary caveat is not copyable", "U//PR-EXO", False,
              is_copyable("U//PR-EXO")),
        _case("T16", "above unclassified is not copyable", "S//NF", False,
              is_copyable("S//NF")),
        _case("T17", "epoch filter uses the trailing-Z microsecond form",
              "2026-08-06T00:00:00+00:00", "2026-08-06T00:00:00.000000Z",
              udl_ts(datetime(2026, 8, 6, tzinfo=timezone.utc))),
        _case("T18", "analyse counts an empty window without dividing by zero",
              [], 0, analyse([])["records"]),
    ]
    failed = [c for c in cases if c["status"] == "FAIL"]
    manifest = {
        "tool": "udl_elset_probe",
        "generated": datetime.now(timezone.utc).isoformat(),
        "total": len(cases), "passed": len(cases) - len(failed),
        "failed": len(failed), "cases": cases,
    }
    print(json.dumps(manifest, indent=2, default=str))
    return 1 if failed else 0


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=6,
                    help="epoch window to sample, in hours (default 6)")
    ap.add_argument("--base", default=DEFAULT_BASE, help="UDL base URL")
    ap.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline assertions and emit a JSON manifest")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr, level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.self_test:
        return self_test()

    user, password = load_credentials()
    rows = fetch_elsets(args.base, user, password, hours=args.hours)
    del password
    report = analyse(rows)
    report["window_hours"] = args.hours
    report["generated"] = datetime.now(timezone.utc).isoformat()
    render(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        LOG.info("wrote %s", args.json)
    every = (report["A1_native_present"]["met"] and report["A2_satno_crosscheck"]["met"]
             and report["A3_source_classes"]["met"])
    return 0 if every else 2


if __name__ == "__main__":
    sys.exit(main())
