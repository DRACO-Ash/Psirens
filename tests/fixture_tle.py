"""Test-only TLE fixture generator.

The application no longer builds a TLE line (1.7.4: no line is shown that a
provider did not issue). Tests still need lines that genuinely checksum, so
this exports one from an element set with the exporter, here and only here.
"""
from __future__ import annotations

from sgp4.exporter import export_tle

from psirens.conjunction import build_satrec


def export_lines(el: dict) -> tuple[str, str] | None:
    """A checksummed two-line set for a test element set, or None."""
    try:
        sat = build_satrec(el)
    except (ValueError, OverflowError, KeyError, TypeError):
        return None
    sat.classification = "U"
    sat.intldesg = str(el.get("intl_desig", "") or "")[:8]
    sat.elnum = 1
    sat.revnum = 0
    sat.ephtype = 0
    return export_tle(sat)
