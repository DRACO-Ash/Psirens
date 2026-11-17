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
        # Node regression, deg/day. Small and NEGATIVE for every object, which
        # is why a co-planar trail runs mostly up and down: longitudes move far
        # faster than planes do. Synthesised like the rest of the geometry.
        "node_rate": -(0.005 + _unit(sat, "node") * 0.02),
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
            # Per-sample RAAN, as a real sample carries from 1.6.17. The NEWEST
            # sample lands on prof["raan"], the same value the elset below
            # carries, so the chart's elset-derived head joins its sample-
            # derived trail instead of jumping away from it.
            "raan_deg": round((prof["raan"] + prof["node_rate"] * t) % 360.0, 4),
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
        "data_mode": "REAL",
    }


def _illustrate_events(out: dict[str, dict], head: datetime) -> None:
    """Five illustrative events for the Shift Brief figures (1.7.0), the same
    five the change proposal of 16 November 2026 was built on. Applied to the
    synthesised geometry only; every other object is left exactly as seeded.
    The detector in src/psirens/events.py finds them, and nothing else."""
    def days(a: datetime, b: datetime) -> float:
        return (a - b).total_seconds() / 86400.0

    def at(sat: str):
        return [(s, datetime.fromisoformat(s["epoch"])) for s in out[sat]["samples"]]

    # SHIYAN 12 02: E-W burn 30 h ago, now drifting 0.60 deg/day west.
    burn = head - timedelta(hours=30)
    for s, t in at("50322"):
        if t >= burn:
            s["sub_lon_deg"] = round(s["sub_lon_deg"] - 0.60 * days(t, burn), 4)
    # COSMOS 2589: N-S burn 58 h ago, inclination up 0.030 deg.
    burn = head - timedelta(hours=58)
    for s, t in at("64467"):
        if t >= burn:
            s["inclination_deg"] = round(s["inclination_deg"] + 0.030, 4)
    # TJS-17: drifting east at 0.25 deg/day until 40 h ago, then on station.
    burn = head - timedelta(hours=40)
    for s, t in at("63524"):
        if t < burn:
            s["sub_lon_deg"] = round(s["sub_lon_deg"] - 0.25 * days(burn, t), 4)
    # TIANLIAN-3 01: a large burn 7 h ago, 1.0 deg/day east: early warning only.
    burn = head - timedelta(hours=7)
    for s, t in at("100172"):
        if t >= burn:
            s["sub_lon_deg"] = round(s["sub_lon_deg"] + 1.0 * days(t, burn), 4)
    # TJS-24: no fix for the last 19 h.
    out["69235"]["samples"] = [s for s, t in at("69235") if t < head - timedelta(hours=19)]


def build(snapshot: str, end: datetime, age_hours: float, events: bool = True) -> dict:
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
    if events and all(k in out for k in ("50322", "64467", "63524", "100172", "69235")):
        _illustrate_events(out, head)
    for rec in out.values():   # provenance as 1.7.4 stores it: a table and an index
        rec["provenance"] = [{"source": "ILLUSTRATIVE", "data_mode": "REAL",
                              "classification_marking": rec["classification_marking"]}]
        for s in rec["samples"]:
            s["p"] = 0
    if "100172" in out:
        _illustrate_providers(out["100172"], head)
    return {"objects": out}


def _illustrate_providers(rec: dict, head: datetime) -> None:
    """Two more providers for TIANLIAN-3 01, the inspector figure's object
    (1.7.5): one agrees, one puts it 25 deg away. Without this every seeded
    object has one provider and the figure cannot show "set aside". Each
    extra element set is issued an hour before the track provider's, with its
    mean anomaly wound back by one hour of motion, so propagated to the common
    instant the agreeing one lands within a hundredth of a degree and the
    outlier 25 degrees off."""
    base = rec["elset"]
    wind = 360.0 * float(base["mean_motion_rev_per_day"]) / 24.0
    rec["elset_candidates"] = {"ILLUSTRATIVE": base}
    for source, shift in (("ILLUSTRATIVE-B", 0.01), ("ILLUSTRATIVE-C", 25.0)):
        el = {**base, "source": source,
              "epoch": (head - timedelta(hours=1)).isoformat(),
              "mean_anomaly_deg": round((base["mean_anomaly_deg"] - wind + shift) % 360.0, 4)}
        rec["elset_candidates"][source] = el
        rec["provenance"].append({"source": source, "data_mode": "REAL",
                                  "classification_marking": rec["classification_marking"]})
        idx = len(rec["provenance"]) - 1
        rec["samples"] += [{**smp, "p": idx,
                            "epoch": (datetime.fromisoformat(smp["epoch"]) - timedelta(hours=1)).isoformat(),
                            "sub_lon_deg": round((smp["sub_lon_deg"] + shift + 180.0) % 360.0 - 180.0, 4)}
                           for smp in rec["samples"] if smp["p"] == 0]
    rec["samples"].sort(key=lambda smp: smp["epoch"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--age-hours", type=float, default=0.5,
                    help="age of the newest fix; above STALE_AFTER_HOURS the "
                         "staleness banner appears, which the guide needs a "
                         "real screenshot of")
    ap.add_argument("--no-events", action="store_true",
                    help="leave out the five illustrative Shift Brief events")
    args = ap.parse_args(argv)

    os.makedirs(args.data_dir, exist_ok=True)
    end = datetime.now(timezone.utc).replace(microsecond=0)
    store = build(args.snapshot, end, args.age_hours, events=not args.no_events)
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
