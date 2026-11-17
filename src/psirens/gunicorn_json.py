"""Gunicorn's own log lines, in the platform logging standard (1.7.1).

Gunicorn configures `gunicorn.error` and `gunicorn.access` itself, before the
application is imported, and its error log defaults to STDERR. Lines such as
"Starting gunicorn" and "Booting worker" therefore went to stderr whatever the
app did, which the Logging check counts against the app. Selected in the
Dockerfile CMD with `--logger-class psirens.gunicorn_json.JsonGunicornLogger`.

The uvicorn worker copies `gunicorn.error`'s handlers onto `uvicorn.error`
when it boots, so this one class also covers uvicorn's startup lines.
"""

from __future__ import annotations

import logging
from typing import Any

from gunicorn.glogging import Logger

from .logjson import StdoutHandler


class JsonGunicornLogger(Logger):
    def setup(self, cfg: Any) -> None:
        super().setup(cfg)
        self.error_log.handlers = [StdoutHandler()]
        self.error_log.propagate = False
        # The ACCESS log is silenced, not converted. The uvicorn worker copies
        # these handlers onto uvicorn.access, whose lines duplicate the app's
        # own request line (logjson.RequestLog) and carry the client address
        # and port, which the standard does not ask for.
        self.access_log.handlers = [logging.NullHandler()]
        self.access_log.propagate = False
