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
import tempfile
from dataclasses import dataclass, field

# Default storage lives under the system temp dir resolved at runtime, not a
# hardcoded world-writable path, and the store creates it with owner-only mode.
_DEFAULT_DATA_DIR = os.path.join(tempfile.gettempdir(), "psirens-data")


def _clean(value: str | None) -> str:
    """Strip surrounding quotes and control characters an operator console
    may smuggle into a pasted value (a trailing newline or tab has broken
    saves and token matches before)."""
    if value is None:
        return ""
    return value.strip().strip('"').strip("'").strip()


def _env(name: str, default: str = "") -> str:
    return _clean(os.environ.get(name, default))


# A STARTING POINT for the owner to amend, not an assertion. Defined here, at
# module level, so a Config built directly carries the same policy as one built
# from the environment: a default that exists only inside load_config is a
# second, quieter default, and the two drift.
DEFAULT_AFFILIATION_RED = ("CHN", "RUS", "IRN", "PRK")
DEFAULT_AFFILIATION_BLUE = (
    "USA", "GBR", "AUS", "CAN", "NZL", "JPN", "KOR", "FRA", "DEU", "ITA",
    "ESP", "NOR", "SWE", "DNK", "NLD", "BEL", "POL", "TUR", "ISR", "IND")


def _env_codes(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma-separated list of country codes, upper-cased and de-duplicated.

    Affiliation is a POLICY, not a fact about an orbit, so it lives in env
    rather than in the code or the feed.
    """
    raw = _env(name, ",".join(default))
    out: list[str] = []
    for part in raw.split(","):
        code = part.strip().upper()
        if code and code not in out:
            out.append(code)
    return tuple(out)


def _env_int(name: str, default: int) -> int:
    """An integer setting. An env var set to empty string, or to something
    unparseable, falls back to the default rather than crashing the boot: a
    container that will not start is a worse outcome than one running on a
    documented default, and the value is logged nowhere secret."""
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


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
    # HRR list (JCO High Risk Register) pulled from UDL notifications.
    udl_notification_path: str = "/udl/notification"
    hrr_msg_type: str = "JCO-HRR-SATELLITES"
    hrr_source: str = "JCO"
    hrr_regime: str = "GEO"
    hrr_lookback_hours: int = 6
    hrr_refresh_seconds: int = 21600  # 6h cadence for the dynamic HRR list
    conj_window_hours: int = 168  # closest-approach screening window (7 days)
    # Ingest windowing. LEARNED (CONTEXT-001): the tenant hard-caps results
    # server-side and the documented remedy is to SLICE BY TIME WINDOW, never
    # to offset-paginate. Before 1.6.6 the scheduled refresh asked for the
    # whole retention window (90 days) of every object in one request, which
    # is the thing that fact forbids; the response came back truncated and the
    # newest fixes never arrived, leaving the plot a constant ~14 days behind.
    refresh_lookback_hours: int = 24   # scheduled pull: a short overlap, not 90 days
    udl_slice_hours: int = 24          # split any longer window into slices
    udl_slice_pause_seconds: float = 2.0   # be a good citizen between slices
    udl_max_results: int = 0           # 0 = do not send the parameter (TBC per tenant)
    # A slice returning at least this many rows is treated as TRUNCATED and
    # subdivided, because a silently truncated page is indistinguishable from
    # a complete one and that is exactly how this defect hid.
    udl_truncation_threshold: int = 9000
    stale_after_hours: float = 24.0    # newest fix older than this raises the alarm
    # Affiliation, used only for the RA bearing needle's colour. Anything on
    # neither list is OTHER, and a blank country is UNKNOWN; neither is an
    # assertion about the operator. Commercial and intergovernmental operators
    # are 20.4% of the bundled 594-object list, so two buckets would be a lie.
    affiliation_red: tuple[str, ...] = DEFAULT_AFFILIATION_RED
    affiliation_blue: tuple[str, ...] = DEFAULT_AFFILIATION_BLUE
    # TLE source selection: ordered classes, first non-empty class wins, newest
    # epoch within it. `any` disables class ranking (plain newest-fix-wins).
    tle_source_priority: str = "commercial,government,unknown"
    coplanar_half_width_deg: float = 20.0  # +/- longitude window for the coplanar view
    scheduler_enabled: bool = True  # background refresh loop; off in tests
    data_dir: str = ""  # explicit override (tests); empty means resolve from env

    def storage_dir(self) -> str:
        """Resolve at call time: explicit override/var, platform mount, default."""
        explicit = self.data_dir or _env("DATA_DIR")
        mount = _env("STORAGE_MOUNT_PATH")  # FILE_STORAGE add-on injects /data
        return explicit or mount or _DEFAULT_DATA_DIR


def load_config() -> Config:
    port = _env_int("PORT", 8080)
    return Config(
        port=port,
        allowed_origin=_env("ALLOWED_ORIGIN"),
        team_token=_env("TEAM_TOKEN"),
        retention_days=_env_int("RETENTION_DAYS", 90),
        refresh_seconds=_env_int("REFRESH_SECONDS", 3600),
        lon_min=_env_float("LON_MIN", -180),
        lon_max=_env_float("LON_MAX", 180),
        inc_min=_env_float("INC_MIN", 0),
        inc_max=_env_float("INC_MAX", 15),
        max_samples_per_object=_env_int("MAX_SAMPLES", 2000),
        demo_mode=_env_bool("DEMO_MODE", default=not bool(_env("UDL_BASE_URL"))),
        udl_enabled=bool(_env("UDL_BASE_URL")),
        udl_base_url=_env("UDL_BASE_URL"),
        udl_elset_path=_env("UDL_ELSET_PATH", "/udl/elset"),
        udl_user=_env("UDL_USER"),
        udl_password=_env("UDL_PASSWORD"),
        udl_target_field=_env("UDL_TARGET_FIELD", "tags"),
        udl_epoch_param=_env("UDL_EPOCH_PARAM", "epoch"),
        udl_accept=_env("UDL_ACCEPT", "application/json"),
        udl_notification_path=_env("UDL_NOTIFICATION_PATH", "/udl/notification"),
        hrr_msg_type=_env("HRR_MSG_TYPE", "JCO-HRR-SATELLITES"),
        hrr_source=_env("HRR_SOURCE", "JCO"),
        hrr_regime=_env("HRR_REGIME", "GEO"),
        hrr_lookback_hours=_env_int("HRR_LOOKBACK_HOURS", 6),
        hrr_refresh_seconds=_env_int("HRR_REFRESH_SECONDS", 21600),
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", default=True),
        conj_window_hours=_env_int("CONJ_WINDOW_HOURS", 168),
        refresh_lookback_hours=_env_int("REFRESH_LOOKBACK_HOURS", 24),
        udl_slice_hours=_env_int("UDL_SLICE_HOURS", 24),
        udl_slice_pause_seconds=_env_float("UDL_SLICE_PAUSE_SECONDS", 2),
        udl_max_results=_env_int("UDL_MAX_RESULTS", 0),
        udl_truncation_threshold=_env_int("UDL_TRUNCATION_THRESHOLD", 9000),
        stale_after_hours=_env_float("STALE_AFTER_HOURS", 24),
        affiliation_red=_env_codes("AFFILIATION_RED", DEFAULT_AFFILIATION_RED),
        affiliation_blue=_env_codes("AFFILIATION_BLUE", DEFAULT_AFFILIATION_BLUE),
        tle_source_priority=_env("TLE_SOURCE_PRIORITY",
                                 "commercial,government,unknown"),
        coplanar_half_width_deg=_env_float("COPLANAR_HALF_WIDTH_DEG", 20),
    )
