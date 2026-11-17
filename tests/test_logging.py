"""The platform logging standard (1.7.1): JSON lines on stdout, request ids in
the attributes, nothing on stderr, and the prohibitions enforced.

The headline test drives the real app with an X-Request-Id and reads the two
streams, which is what the platform's Logging check does. Removing the
RequestLog middleware from create_app fails two tests; reverting main.py to
logging.basicConfig fails the import-time test. Both PROVEN by revert.
"""

from __future__ import annotations

import json
import logging
import re
import sys

import pytest
from fastapi.testclient import TestClient

from psirens.config import Config
from psirens.gunicorn_json import JsonGunicornLogger
from psirens.logjson import (EMAIL_ATTR, REQUEST_ID_ATTR, JsonFormatter, StdoutHandler,
                             configure_logging, request_attributes, scrub, severity)
from psirens.main import create_app

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def _cfg(tmp_path, **kw) -> Config:
    base = dict(
        port=8080, allowed_origin="", team_token="",
        retention_days=90, refresh_seconds=3600,
        lon_min=-180.0, lon_max=180.0, inc_min=0.0, inc_max=15.0,
        max_samples_per_object=2000, demo_mode=True, udl_enabled=False,
        udl_base_url="", udl_elset_path="/udl/elset", udl_user="",
        udl_password="", udl_target_field="tags", udl_epoch_param="epoch",
        udl_accept="application/json", data_dir=str(tmp_path),
        scheduler_enabled=False,
    )
    base.update(kw)
    return Config(**base)


def _record(level=logging.INFO, msg="hello %s", args=("there",), exc_info=None, **extra):
    rec = logging.LogRecord("psirens.test", level, __file__, 1, msg, args, exc_info)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# -- the format --------------------------------------------------------------
def test_a_line_carries_every_top_level_field_in_the_standard_shape():
    line = json.loads(JsonFormatter().format(_record()))
    assert set(line) == {"timestamp", "severity_text", "severity_number", "body", "attributes"}
    assert STAMP.match(line["timestamp"])
    assert (line["severity_text"], line["severity_number"]) == ("INFO", 9)
    assert line["body"] == "hello there"
    assert line["attributes"] == {"logger.name": "psirens.test"}


@pytest.mark.parametrize("level,pair", [
    (logging.DEBUG, ("DEBUG", 5)), (logging.INFO, ("INFO", 9)),
    (logging.WARNING, ("WARN", 13)), (logging.ERROR, ("ERROR", 17)),
    (logging.CRITICAL, ("FATAL", 21)), (1, ("TRACE", 1))])
def test_severity_pairs_follow_the_standard(level, pair):
    assert severity(level) == pair


def test_an_exception_goes_into_flat_attributes():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = _record(logging.ERROR, "failed", (), sys.exc_info())
    attrs = json.loads(JsonFormatter().format(rec))["attributes"]
    assert attrs["exception.type"] == "ValueError"
    assert attrs["exception.message"] == "boom"
    assert "Traceback" in attrs["exception.stacktrace"]
    assert all(not isinstance(v, (dict, list)) for v in attrs.values())


def test_the_prohibitions_are_enforced_by_the_formatter_not_the_call_site():
    rec = _record(msg="GET https://udl.example/udl/elset?epoch=2026&token=abc failed, "
                      "header Bearer s3cr3t", args=(),
                  attributes={"service.name": "psirens", "tenant.id": "t",
                              "http.request.header.authorization": "Bearer x",
                              "cookie": "c", "app_id": "a", "kept": "/api/x?q=1"})
    line = json.loads(JsonFormatter().format(rec))
    assert "token=abc" not in line["body"] and "s3cr3t" not in line["body"]
    assert "https://udl.example/udl/elset" in line["body"]
    assert set(line["attributes"]) == {"logger.name", "kept"}
    assert line["attributes"]["kept"] == "/api/x"


def test_scrub_leaves_ordinary_questions_alone():
    assert scrub("why is this old? see /api/meta?x=1") == "why is this old? see /api/meta"


def test_a_bad_format_string_never_raises():
    line = json.loads(JsonFormatter().format(_record(msg="%d items", args=("x",))))
    assert line["body"] == "%d items"


def test_request_attributes_sanitise_and_respect_the_email_switch():
    headers = [(b"X-Request-Id", b"abc\r\ninjected"), (b"X-Auth-Request-Email", b"a@b.c")]
    assert request_attributes(headers, False) == {REQUEST_ID_ATTR: "abcinjected"}
    assert request_attributes(headers, True)[EMAIL_ATTR] == "a@b.c"


# -- the app, read the way the platform reads it -----------------------------
def test_the_app_configures_json_logging_on_stdout_at_import():
    """RED CASE for main.py: restoring logging.basicConfig there installs a
    plain StreamHandler on stderr and no StdoutHandler, and this fails. The
    fixtures below deliberately do NOT call configure_logging themselves; the
    first version did, and it masked exactly that revert."""
    import psirens.main  # noqa: F401  (import-time configuration under test)
    root = logging.getLogger()
    assert any(isinstance(h, StdoutHandler) for h in root.handlers)
    assert not any(type(h) is logging.StreamHandler for h in root.handlers)


@pytest.fixture
def app_client(tmp_path):
    with TestClient(create_app(_cfg(tmp_path), sources=[])) as c:
        yield c


def test_requests_log_json_to_stdout_with_their_id_and_nothing_to_stderr(app_client, capsys):
    capsys.readouterr()
    app_client.get("/", headers={"X-Request-Id": "probe-one"})
    app_client.get("/api/meta?token=leak", headers={"X-Request-Id": "probe-two"})
    out, err = capsys.readouterr()
    lines = _lines(out)
    ids = {ln["attributes"].get(REQUEST_ID_ATTR) for ln in lines}
    assert {"probe-one", "probe-two"} <= ids, "no request line carried the X-Request-Id"
    assert err == "", "a log line went to stderr"
    assert "leak" not in out
    req = [ln for ln in lines if ln["attributes"].get("logger.name") == "psirens.http"]
    assert {ln["attributes"]["url.path"] for ln in req} >= {"/", "/api/meta"}
    assert all(ln["attributes"]["http.response.status_code"] == 200 for ln in req)


def test_an_unidentified_health_probe_is_not_logged_at_info(app_client, capsys):
    capsys.readouterr()
    app_client.get("/healthz")
    assert not [ln for ln in _lines(capsys.readouterr().out)
                if ln["attributes"].get("url.path") == "/healthz"]


def test_user_email_is_logged_only_when_switched_on(tmp_path, capsys):
    hdr = {"X-Request-Id": "e1", "X-Auth-Request-Email": "operator@example.test"}
    with TestClient(create_app(_cfg(tmp_path), sources=[])) as c:
        capsys.readouterr()
        c.get("/", headers=hdr)
        assert "operator@example.test" not in capsys.readouterr().out
    with TestClient(create_app(_cfg(tmp_path, log_user_email=True), sources=[])) as c:
        capsys.readouterr()
        c.get("/", headers=hdr)
        lines = _lines(capsys.readouterr().out)
        assert any(ln["attributes"].get(EMAIL_ATTR) == "operator@example.test" for ln in lines)


def test_configure_logging_silences_the_uvicorn_access_line():
    configure_logging()
    access = logging.getLogger("uvicorn.access")
    assert access.propagate is False
    assert all(isinstance(h, logging.NullHandler) for h in access.handlers)
    assert [type(h) for h in logging.getLogger().handlers].count(StdoutHandler) == 1


def test_the_stdout_handler_follows_the_current_stdout(capsys):
    handler = StdoutHandler()
    handler.stream = sys.stderr          # overridden at the next emit, by design
    handler.emit(_record(msg="routed", args=()))
    out, err = capsys.readouterr()
    assert json.loads(out)["body"] == "routed" and err == ""


def test_gunicorns_own_lines_go_to_stdout_as_json():
    from gunicorn.config import Config as GConfig
    glog = JsonGunicornLogger(GConfig())
    assert [type(h) for h in glog.error_log.handlers] == [StdoutHandler]
    assert glog.error_log.propagate is False
    assert all(isinstance(h, logging.NullHandler) for h in glog.access_log.handlers)
