"""Domain models for PSIRENS.

Every record that enters the store is validated here at the boundary and
rejected on failure; nothing is coerced into the store as junk.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1


def display_name(object_id: str, stored_name: object, names: dict | None = None) -> str:
    """The human name to show for an object, best source first.

    The store's name for a UDL record comes from `origObjectId`, which this
    tenant leaves empty, so it falls back to `satNo` and the record ends up
    named after its own catalogue number. A row then reads "63157 63157"
    instead of "TJS-15 63157".

    The JCO HRR list is the naming authority (it carries `commonName`), so it
    wins. The stored name is used only when it is genuinely a name rather than
    the catalogue number repeated back. Failing both, the id stands alone: an
    unnamed object should show its id once, never twice.
    """
    from_hrr = str((names or {}).get(object_id) or "").strip()
    if from_hrr:
        return from_hrr
    stored = str(stored_name or "").strip()
    if stored and stored != str(object_id):
        return stored
    return str(object_id)

# The four accepted UDL data modes (confirmed by owner, 20 July 2026).
class DataMode(str, enum.Enum):
    REAL = "REAL"
    SIMULATED = "SIMULATED"
    TEST = "TEST"
    EXERCISE = "EXERCISE"


# Which modes each hero tab shows by default.
VIEW_MODES: dict[str, list[DataMode]] = {
    "real": [DataMode.REAL],
    "sim": [DataMode.SIMULATED],
    "combined": [DataMode.REAL, DataMode.SIMULATED, DataMode.TEST, DataMode.EXERCISE],
}


class ManualElsetIn(BaseModel):
    """A temporary, operator-supplied element set.

    Accepts classical mean Keplerian elements directly so no TLE-string
    construction (with its checksum and exponent traps) is required.
    """

    object_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    epoch: datetime
    inclination_deg: float = Field(ge=0.0, le=180.0)
    eccentricity: float = Field(ge=0.0, lt=1.0)
    raan_deg: float = Field(ge=0.0, le=360.0)
    argp_deg: float = Field(ge=0.0, le=360.0)
    mean_anomaly_deg: float = Field(ge=0.0, le=360.0)
    mean_motion_rev_per_day: float = Field(gt=0.0, le=20.0)
    bstar: float = 0.0
    data_mode: DataMode = DataMode.SIMULATED
    classification_marking: str = Field(default="U", max_length=64)
    source: str = Field(default="MANUAL", max_length=64)
    target: str | None = Field(default=None, max_length=120)
    ttl_hours: float = Field(default=24.0, gt=0.0, le=24.0 * 365.0)

    @field_validator("epoch")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class Sample(BaseModel):
    """One point on an object's track: its state at one elset epoch."""

    epoch: datetime
    sub_lon_deg: float
    inclination_deg: float


class Track(BaseModel):
    """An object and its ordered samples over the retention window."""

    object_id: str
    name: str
    data_mode: DataMode
    classification_marking: str
    source: str
    origin: str  # UDL | MANUAL | DEMO
    target: str | None = None
    samples: list[Sample]
    drift_deg_per_day: float | None = None  # longitude drift rate at the head
