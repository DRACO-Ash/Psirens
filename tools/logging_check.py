#!/usr/bin/env python3
"""Run the app exactly as the container does and read its two output streams,
the way the App Store Logging check does (1.7.1).

The Logging check is DYNAMIC: it starts the image, sends requests carrying an
X-Request-Id header, and reads stdout and stderr. No grep can stand in for
that, so this runs the Dockerfile's own CMD (parsed from the Dockerfile, not
retyped, so the two cannot drift), sends two requests with distinct ids and a
query string carrying a fake secret, stops the server with SIGTERM, and
asserts:

  1. something was written to stdout;
  2. NOTHING was written to stderr;
  3. every stdout line is one JSON object in the standard's shape: timestamp
     (UTC, milliseconds), a valid severity_text/severity_number pair, a body,
     and a FLAT attributes object;
  4. each request id appears in some line's attributes;
  5. the query-string secret appears nowhere, and no forbidden attribute key
     (service.name, tenant or app ids, authorization, cookie, token) appears.

`--red` runs the same command WITHOUT the --logger-class flag, which is the
1.7.0 behaviour for gunicorn's own lines; the check MUST reject it. Run with
`--expect fail` that way, so a checker that can no longer see stderr fails the
build instead of passing it.

Exit codes: 0 as expected, 1 not as expected, 2 cannot run (no gunicorn).
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CMD = re.compile(r'^CMD \["sh","-c","(.*)"\]\s*$', re.M)
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
_PAIRS = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
_FORBIDDEN = re.compile(r"^service\.name$|tenant|app[._-]?id|authorization|cookie|token",
                        re.IGNORECASE)
REQUEST_ID = "http.request.header.x-request-id"
SECRET = "LOGCHECKSECRET42"


def container_command(red: bool) -> str:
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
        match = _CMD.search(fh.read())
    if match is None:
        raise SystemExit("cannot find the CMD in the Dockerfile")
    cmd = match.group(1)
    return re.sub(r"\s--logger-class\s+\S+", "", cmd) if red else cmd


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, rid: str) -> None:
    req = urllib.request.Request(url, headers={"X-Request-Id": rid})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (localhost)
        resp.read()


def _wait_healthy(base: str, proc: subprocess.Popen) -> bool:
    for _ in range(120):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2):  # noqa: S310
                return True
        except OSError:
            time.sleep(0.5)
    return False


def run_server(red: bool) -> tuple[str, str, list[str]]:
    """Start the container command, send the probes, stop it, return output."""
    port = _free_port()
    data = tempfile.mkdtemp(prefix="psirens-logcheck-")
    env = dict(os.environ, PORT=str(port), DATA_DIR=data,
               PYTHONPATH=os.path.join(ROOT, "src"), PYTHONUNBUFFERED="1")
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    out_f = tempfile.TemporaryFile(mode="w+")
    err_f = tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(["sh", "-c", container_command(red)], cwd=ROOT, env=env,
                            stdout=out_f, stderr=err_f)
    ids = [f"logcheck-{uuid.uuid4().hex[:12]}" for _ in range(2)]
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_healthy(base, proc):
            raise RuntimeError("the server did not become healthy")
        _get(base + "/", ids[0])
        _get(base + f"/api/meta?view=real&token={SECRET}", ids[1])
        time.sleep(1.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(data, ignore_errors=True)
    out_f.seek(0)
    err_f.seek(0)
    return out_f.read(), err_f.read(), ids


def _line_problems(n: int, line: str) -> list[str]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return [f"stdout line {n} is not JSON: {line[:100]!r}"]
    if not isinstance(obj, dict):
        return [f"stdout line {n} is not a JSON object"]
    out = []
    if not _STAMP.match(str(obj.get("timestamp", ""))):
        out.append(f"line {n}: timestamp {obj.get('timestamp')!r} is not UTC with milliseconds")
    if _PAIRS.get(obj.get("severity_text")) != obj.get("severity_number"):
        out.append(f"line {n}: severity pair {obj.get('severity_text')!r}/"
                   f"{obj.get('severity_number')!r} is not in the standard")
    if not isinstance(obj.get("body"), str):
        out.append(f"line {n}: body is not a string")
    attrs = obj.get("attributes")
    if not isinstance(attrs, dict) or any(isinstance(v, (dict, list)) for v in attrs.values()):
        out.append(f"line {n}: attributes is not a flat object")
    elif any(_FORBIDDEN.search(k) for k in attrs):
        out.append(f"line {n}: forbidden attribute key in {sorted(attrs)}")
    return out


def evaluate(stdout: str, stderr: str, ids: list[str]) -> list[str]:
    problems: list[str] = []
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    errs = [ln for ln in stderr.splitlines() if ln.strip()]
    if not lines:
        problems.append("nothing was written to stdout")
    if errs:
        problems.append(f"{len(errs)} line(s) went to stderr, first: {errs[0][:120]!r}")
    seen: set[str] = set()
    for n, line in enumerate(lines, 1):
        problems.extend(_line_problems(n, line))
        try:
            attrs = json.loads(line).get("attributes") or {}
            seen.add(str(attrs.get(REQUEST_ID, "")))
        except (json.JSONDecodeError, AttributeError):
            continue
    for rid in ids:
        if rid not in seen:
            problems.append(f"no log line carries request id {rid} in its attributes")
    if SECRET in stdout or SECRET in stderr:
        problems.append("a query-string secret was written to the logs")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--red", action="store_true",
                    help="drop --logger-class from the CMD: the 1.7.0 behaviour")
    ap.add_argument("--expect", choices=("pass", "fail"), default="pass")
    a = ap.parse_args(argv)
    if shutil.which("gunicorn", path=os.path.dirname(sys.executable)) is None \
            and shutil.which("gunicorn") is None:
        print("UNAVAILABLE: gunicorn is not installed")
        return 2
    try:
        stdout, stderr, ids = run_server(a.red)
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    problems = evaluate(stdout, stderr, ids)
    lines = len([ln for ln in stdout.splitlines() if ln.strip()])
    for p in problems:
        print(("  rejected: " if a.expect == "fail" else "FAIL: ") + p)
    passed = not problems
    print(f"logging check ({'red: no --logger-class' if a.red else 'container CMD'}): "
          f"{lines} stdout line(s), {len([ln for ln in stderr.splitlines() if ln.strip()])} "
          f"stderr line(s), {'compliant' if passed else 'NOT compliant'}")
    return 0 if passed == (a.expect == "pass") else 1


if __name__ == "__main__":
    sys.exit(main())
