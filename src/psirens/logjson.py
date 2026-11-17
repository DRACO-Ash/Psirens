"""The App Store logging standard (1.7.1): one JSON object per line, on stdout.

FACT, 11 November 2026, from the platform's Logging check (Advisory until it
starts blocking on 1 December 2026): PSIRENS wrote nothing to stdout, wrote
its lines to stderr, and no request line carried the X-Request-Id the check
sent. The cause was one line: `logging.basicConfig`, whose default handler is
stderr and whose format is plain text. Gunicorn's own error log defaults to
stderr as well, which is why the second reading counted ten lines, not nine.

THE FORMAT (CONTEXT-001 section 7): top-level `timestamp` (UTC, milliseconds),
`severity_text`, `severity_number` (TRACE=1, DEBUG=5, INFO=9, WARN=13,
ERROR=17, FATAL=21), `body` and a FLAT `attributes` object. Exceptions go into
attributes as `exception.type`, `exception.message`, `exception.stacktrace`.
Request lines carry `http.request.header.x-request-id`, and `user.email` from
`X-Auth-Request-Email` (on by default from 1.7.3; `LOG_USER_EMAIL=false` off).

THE PROHIBITIONS are enforced HERE, in the formatter, not trusted to call
sites: no `service.name`, no tenant or app identifiers, no Authorization or
Cookie values, no tokens, and no query strings. A query string can arrive
inside an exception message (httpx names the full URL it failed on), so the
body and every attribute value are scrubbed on the way out.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

_REQUEST: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "psirens_request_attributes", default=None)

REQUEST_ID_ATTR = "http.request.header.x-request-id"
EMAIL_ATTR = "user.email"
_MAX_HEADER = 200
_QUIET_PATHS = ("/healthz", "/readyz")

_FORBIDDEN_KEY = re.compile(
    r"^service\.name$|tenant|app[._-]?id|authorization|cookie|token|password|secret",
    re.IGNORECASE)
_QUERY = re.compile(r"((?:https?://|/)[^\s'\"?]*)\?[^\s'\"]*")
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def severity(levelno: int) -> tuple[str, int]:
    """The standard's severity pair for a Python level."""
    for floor, pair in ((logging.CRITICAL, ("FATAL", 21)), (logging.ERROR, ("ERROR", 17)),
                        (logging.WARNING, ("WARN", 13)), (logging.INFO, ("INFO", 9)),
                        (logging.DEBUG, ("DEBUG", 5))):
        if levelno >= floor:
            return pair
    return ("TRACE", 1)


def scrub(text: str) -> str:
    """Remove query strings and bearer credentials from free text."""
    return _BEARER.sub("Bearer [redacted]", _QUERY.sub(r"\1", text))


def _flat(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


def _exception_attributes(exc_info: Any) -> dict[str, str]:
    etype, evalue, tb = exc_info
    if etype is None:
        return {}
    return {"exception.type": etype.__name__,
            "exception.message": str(evalue),
            "exception.stacktrace": "".join(traceback.format_exception(etype, evalue, tb))}


class JsonFormatter(logging.Formatter):
    """One standard log line per record."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            body = record.getMessage()
        except (TypeError, ValueError):
            body = str(record.msg)
        attrs: dict[str, Any] = {"logger.name": record.name}
        attrs.update(_REQUEST.get() or {})
        extra = getattr(record, "attributes", None)
        if isinstance(extra, dict):
            attrs.update(extra)
        if record.exc_info:
            attrs.update(_exception_attributes(record.exc_info))
        clean = {k: _flat(v) for k, v in attrs.items() if not _FORBIDDEN_KEY.search(k)}
        name, number = severity(record.levelno)
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return json.dumps({
            "timestamp": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "severity_text": name, "severity_number": number,
            "body": scrub(body), "attributes": clean,
        }, separators=(",", ":"), ensure_ascii=False)


class StdoutHandler(logging.StreamHandler):
    """A handler that always writes to the CURRENT sys.stdout.

    Re-read at every emit rather than captured at construction, so a test
    harness that swaps stdout sees every line, and nothing can ever be left
    bound to stderr by accident."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Route every logger in the process to one JSON handler on stdout.

    uvicorn's own loggers propagate to it. Its access logger is silenced: it
    writes after the request context has closed, so its lines could never
    carry the request id, and it prints the full path WITH the query string.
    `RequestLog` writes a compliant request line instead. Gunicorn's loggers
    are handled by `gunicorn_json.JsonGunicornLogger`, set on the command
    line, because gunicorn configures them before the app is imported."""
    root = logging.getLogger()
    root.handlers = [StdoutHandler()]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.NullHandler()]
    access.propagate = False
    logging.captureWarnings(True)


def _header(value: bytes) -> str:
    return _CONTROL.sub("", value.decode("latin-1"))[:_MAX_HEADER]


def request_attributes(headers: list[tuple[bytes, bytes]], log_email: bool) -> dict[str, str]:
    """The attributes every line written while handling this request carries."""
    out: dict[str, str] = {}
    for key, value in headers:
        name = key.lower()
        if name == b"x-request-id":
            out[REQUEST_ID_ATTR] = _header(value)
        elif name == b"x-auth-request-email" and log_email:
            out[EMAIL_ATTR] = _header(value)
    return out


_HTTP = logging.getLogger("psirens.http")


class RequestLog:
    """ASGI middleware: request context for every line, and one request line.

    The request id is held in a context variable for the life of the request,
    so a line logged anywhere while the request is handled, including in the
    thread a sync route runs on, carries it. A health probe with no request id
    is logged at DEBUG: the platform polls those constantly."""

    def __init__(self, app: Any, *, log_email: bool = False) -> None:
        self.app = app
        self.log_email = log_email

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        attrs = request_attributes(scope.get("headers") or [], self.log_email)
        token = _REQUEST.set(attrs)
        status = {"code": 500}

        async def _send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, _send)
        finally:
            path = scope.get("path", "")
            quiet = path in _QUIET_PATHS and REQUEST_ID_ATTR not in attrs
            _HTTP.log(logging.DEBUG if quiet else logging.INFO, "%s %s %s",
                      scope.get("method", ""), path, status["code"], extra={"attributes": {
                          "http.request.method": scope.get("method", ""),
                          "url.path": path,
                          "http.response.status_code": status["code"],
                          "http.server.request.duration_ms":
                              round((time.perf_counter() - started) * 1000.0, 2)}})
            _REQUEST.reset(token)
