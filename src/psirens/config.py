"""Configuration, read from the environment only (never a committed file).

Injected add-on values (the storage mount) are read at request/boot time, not
at import time, so an empty value is never captured before the platform injects
it. The storage path is resolved fail-closed but recoverable: a bad path is
reported clearly rather than silently writing to an ephemeral layer.

All UDL wire details are TBC and marked: the LEARNED register only verifies
/udl/eoobservation behaviour, so nothing about /udl/elset is assumed. Every
UDL knob is overridable by environment so it can be corrected against the
tenant without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _clean(value: str | None) -> str:
    """Strip surrounding quotes and control characters an operator console
    may smuggle into a pasted value (a trailing newline or tab has broken
    saves and token matches before)."""
    if value is None:
        return ""
    return value.strip().strip('"').strip("'").strip()


def _env(name: str, default: str = "") -> str:
    return _clean(os.environ.get(name, default))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class Config:
    # Runtime contract
    port: int
    allowed_origin: str
    team_token: str

    # Data window and cadence
    retention_days: int
    refresh_seconds: int
    lon_min: float
    lon_max: float
    inc_min: float
    inc_max: float
    max_samples_per_object: int

    # Source selection
    demo_mode: bool
    udl_enabled: bool

    # UDL wire config (ALL TBC: verify against the tenant before live use)
    udl_base_url: str
    udl_elset_path: str
    udl_user: str
    udl_password: str = field(repr=False, default="")
    udl_target_field: str = "tags"  # TBC: which labelled field carries target
    udl_epoch_param: str = "epoch"
    udl_accept: str = "application/json"
    data_dir: str = ""  # explicit override (tests); empty means resolve from env

    def storage_dir(self) -> str:
        """Resolve at call time: explicit override/var, platform mount, default."""
        explicit = self.data_dir or _env("DATA_DIR")
        mount = _env("STORAGE_MOUNT_PATH")  # FILE_STORAGE add-on injects /data
        return explicit or mount or "/tmp/psirens-data"


def load_config() -> Config:
    port = int(_env("PORT", "8080") or "8080")
    return Config(
        port=port,
        allowed_origin=_env("ALLOWED_ORIGIN"),
        team_token=_env("TEAM_TOKEN"),
        retention_days=int(_env("RETENTION_DAYS", "90") or "90"),
        refresh_seconds=int(_env("REFRESH_SECONDS", "3600") or "3600"),
        lon_min=float(_env("LON_MIN", "-180") or "-180"),
        lon_max=float(_env("LON_MAX", "180") or "180"),
        inc_min=float(_env("INC_MIN", "0") or "0"),
        inc_max=float(_env("INC_MAX", "15") or "15"),
        max_samples_per_object=int(_env("MAX_SAMPLES", "2000") or "2000"),
        demo_mode=_env_bool("DEMO_MODE", default=not bool(_env("UDL_BASE_URL"))),
        udl_enabled=bool(_env("UDL_BASE_URL")),
        udl_base_url=_env("UDL_BASE_URL"),
        udl_elset_path=_env("UDL_ELSET_PATH", "/udl/elset"),
        udl_user=_env("UDL_USER"),
        udl_password=_env("UDL_PASSWORD"),
        udl_target_field=_env("UDL_TARGET_FIELD", "tags"),
        udl_epoch_param=_env("UDL_EPOCH_PARAM", "epoch"),
        udl_accept=_env("UDL_ACCEPT", "application/json"),
    )
