#!/usr/bin/env python3
"""Seed a representative local store so the Operator Guide's figures are
screenshots of the REAL interface rather than drawings of it.

The NAMES, COUNTRIES and HRR RANKS come from the bundled JCO snapshot, so the
watchlist, the country panel and the priority mix look like the real thing.
The ORBITAL POSITIONS are synthesised, deterministically from each catalogue
number, because this machine has no UDL access. Every figure in the guide says
so: illustrative geometry, real object metadata.

Stdlib only, single file, deterministic given the same arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

_SIDEREAL = 0.9972695787          # days, the east-west libration period
_GEO_MM = 1.0027379093            # rev/day
_SAMPLE_STEP_H = 2.0              # dense enough for the harmonic drift fit
_SPAN_DAYS = 4


def _unit(sat: str, salt: str) -> float:
    """A stable number in [0, 1) from a catalogue number and a label."""
    digest = hashlib.sha256(f"{sat}:{salt}".encode()).digest()
    return int.from_bytes(digest[:6], "big") / float(1 << 48)


def _profile(sat: str) -> dict:
    """Deterministic orbital character for one object."""
    lon0 = _unit(sat, "lon") * 360.0 - 180.0
    roll = _unit(sat, "kind")
    if roll < 0.62:                       # station-kept: the belt's bulk
        inc, drift = _unit(sat, "inc") * 0.12, (_unit(sat, "d") - 0.5) * 0.04
    elif roll < 0.86:                      # inclined, still on station
        inc, drift = 0.6 + _unit(sat, "inc") * 6.5, (_unit(sat, "d") - 0.5) * 0.3
    else:                                  # genuine drifters
        inc, drift = _unit(sat, "inc") * 3.0, (_unit(sat, "d") - 0.5) * 4.0
    return {
        "lon0": lon0, "inc": inc, "drift": drift,
        "ecc": 0.0002 + _unit(sat, "e") * 0.0024,
        "raan": _unit(sat, "raan") * 360.0,
        "argp": _unit(sat, "argp") * 360.0,
        "ma": _unit(sat, "ma") * 360.0,
    }


def _samples(sat: str, prof: dict, end: datetime) -> list[dict]:
    """A trail: secular drift plus the once-per-sidereal-day libration that
    `astro.drift_fit` exists to model out."""
    amp = math.degrees(2.0 * prof["ecc"])
    steps = int(_SPAN_DAYS * 24.0 / _SAMPLE_STEP_H)
    out = []
    for k in range(steps + 1):
        days_ago = (steps - k) * _SAMPLE_STEP_H / 24.0
        t = -days_ago
        lon = (prof["lon0"] + prof["drift"] * t
               + amp * math.sin(2.0 * math.pi * t / _SIDEREAL))
        out.append({
            "epoch": (end + timedelta(days=t)).isoformat(),
            "sub_lon_deg": round((lon + 180.0) % 360.0 - 180.0, 4),
            "inclination_deg": round(prof["inc"], 4),
        })
    return out


def _elset(sat: str, prof: dict, end: datetime, marking: str) -> dict:
    return {
        "sat_no": sat, "epoch": end.isoformat(),
        "inclination_deg": round(prof["inc"], 4),
        "eccentricity": round(prof["ecc"], 7),
        "raan_deg": round(prof["raan"], 4),
        "argp_deg": round(prof["argp"], 4),
        "mean_anomaly_deg": round(prof["ma"], 4),
        "mean_motion_rev_per_day": round(_GEO_MM - prof["drift"] / 360.0, 9),
        "bstar": 0.0, "classification": marking, "intl_desig": sat,
        "source": "ILLUSTRATIVE", "line1": "", "line2": "", "rev_no": None,
        "mean_motion_dot": 0.0, "mean_motion_ddot": 0.0, "ephem_type": 0,
    }


def build(snapshot: str, end: datetime, age_hours: float) -> dict:
    with open(snapshot, encoding="utf-8") as fh:
        objects = json.load(fh).get("objects", {})
    head = end - timedelta(hours=age_hours)
    out: dict[str, dict] = {}
    for sat, entry in objects.items():
        prof = _profile(sat)
        marking = "U//PR-MRKT" if _unit(sat, "mark") < 0.2 else "U"
        out[sat] = {
            "name": entry.get("name") or sat, "data_mode": "REAL",
            "classification_marking": marking, "source": "ILLUSTRATIVE",
            "origin": "UDL", "target": None,
            "samples": _samples(sat, prof, head),
            "elset": _elset(sat, prof, head, marking),
        }
    return {"objects": out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--age-hours", type=float, default=0.5,
                    help="age of the newest fix; above STALE_AFTER_HOURS the "
                         "staleness banner appears, which the guide needs a "
                         "real screenshot of")
    args = ap.parse_args(argv)

    os.makedirs(args.data_dir, exist_ok=True)
    end = datetime.now(timezone.utc).replace(microsecond=0)
    store = build(args.snapshot, end, args.age_hours)
    with open(os.path.join(args.data_dir, "history.json"), "w", encoding="utf-8") as fh:
        json.dump(store, fh, separators=(",", ":"))
    with open(args.snapshot, encoding="utf-8") as src:
        snap = json.load(src)
    with open(os.path.join(args.data_dir, "hrr.json"), "w", encoding="utf-8") as fh:
        json.dump(snap, fh, separators=(",", ":"))
    print(f"seeded {len(store['objects'])} objects into {args.data_dir} "
          f"(newest fix {args.age_hours}h old)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
