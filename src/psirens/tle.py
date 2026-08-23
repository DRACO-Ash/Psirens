"""Native UDL two-line element sets: capture, validation, selection, release.

UDL carries native `line1`/`line2` on its elset records. Those lines are the
provider's own fix, checksummed and Alpha-5 correct as issued. Reconstruction
via `sgp4.exporter` (see `conjunction.py`) stays as the fallback for records
that carry no usable native line, but it is no longer the primary path: it
cannot recover the international designator or the rev number, so a
reconstructed line is strictly lossier than the one the provider pushed.

Three things happen here.

Validation. A native line is served only if it parses as a TLE, checksums
clean on both lines, and its embedded catalogue number matches the record's
own `satNo`. A provider that pushes a line belonging to a different object is
a data fault, not a display problem: the line is rejected and the record falls
back to reconstruction rather than mislabelling somebody else's orbit.

Selection. An object may hold a fix from several providers at once. The
selection policy is an ordered list of source classes (`TLE_SOURCE_PRIORITY`,
default `commercial,government,unknown`): the first class holding any
candidate wins, and within that class the newest epoch wins. Setting the
policy to `any` collapses this to plain newest-fix-wins across all sources.

Release. Commercial elsets carry proprietary caveats (`U//PR-...`). Whatever
is displayed, only a line whose classification marking is plain unclassified
and caveat-free is offered for copy-out; everything else is shown with the
copy control withheld and the reason stated.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

_log = logging.getLogger("psirens.tle")

# Standard TLE line length. Providers sometimes pad or trim trailing spaces,
# so lines are right-stripped before this is applied.
_TLE_LEN = 69

# Source classes, most specific match first. Provider names observed on the
# tenant's /udl/elset feed (CLAUDE.md, Open items): JCO pushes the HRR list
# only; elements arrive from Cloudstone, NorthStar, EXO, KBR, KRTL, LeoLabs
# and 18th SPCS. An unrecognised source is classed `unknown` and, under the
# default policy, ranks below both known classes rather than being quietly
# promoted into one of them.
CLASS_COMMERCIAL = "commercial"
CLASS_GOVERNMENT = "government"
CLASS_UNKNOWN = "unknown"

_GOVERNMENT_MARKERS = (
    # 18 SPCS was redesignated 18 SDS; the tenant feed carries both forms,
    # and 19 SDS appears alongside it. Match every spelling seen so a rename
    # upstream cannot silently reclassify a government fix as unknown.
    "18 SPCS", "18TH SPCS", "SPCS", "18 SDS", "18SDS", "19 SDS", "19SDS",
    "USSF", "SPACE FORCE", "AFSPC", "SPADOC", "SPACETRACK", "SPACE-TRACK",
    "JCO",
)
_COMMERCIAL_MARKERS = (
    "CLOUDSTONE", "NORTHSTAR", "NORTH STAR", "EXO", "KBR", "KRTL",
    "LEOLABS", "LEO LABS",
)

DEFAULT_PRIORITY = (CLASS_COMMERCIAL, CLASS_GOVERNMENT, CLASS_UNKNOWN)

# Policy value that disables class ranking entirely: newest fix wins outright.
POLICY_ANY = "any"


def classify_source(source: str | None) -> str:
    """Map a UDL `source` value onto a selection class.

    Government markers are tested first: several government feeds reach the
    tenant through a commercially-operated relay, and the operator of the
    pipe does not change who produced the fix.
    """
    name = (source or "").strip().upper()
    if not name:
        return CLASS_UNKNOWN
    for marker in _GOVERNMENT_MARKERS:
        if marker in name:
            return CLASS_GOVERNMENT
    for marker in _COMMERCIAL_MARKERS:
        if marker in name:
            return CLASS_COMMERCIAL
    return CLASS_UNKNOWN


def parse_priority(raw: str | None) -> tuple[str, ...]:
    """Parse `TLE_SOURCE_PRIORITY` into an ordered class tuple.

    Only the explicit literal `any` yields an empty tuple, meaning no class
    ranking: selection falls through to newest-epoch-wins across every
    candidate. An empty or unparseable value falls back to the default order
    rather than silently disabling the policy, which would change which
    provider an operator sees for no stated reason.
    """
    text = (raw or "").strip().lower()
    if text == POLICY_ANY:
        return ()
    if not text:
        return DEFAULT_PRIORITY
    valid = {CLASS_COMMERCIAL, CLASS_GOVERNMENT, CLASS_UNKNOWN}
    order = [p.strip() for p in text.split(",")]
    kept = tuple(p for p in order if p in valid)
    return kept or DEFAULT_PRIORITY


# --------------------------------------------------------------------------
# Native line validation
# --------------------------------------------------------------------------
def _checksum(line: str) -> int:
    """TLE mod-10 checksum over the first 68 columns: digits sum as their
    value, every minus sign counts one, everything else counts nothing."""
    total = 0
    for ch in line[:68]:
        if ch.isdigit():
            total += int(ch)
        elif ch == "-":
            total += 1
    return total % 10


def _checksum_ok(line: str) -> bool:
    tail = line[68:69]
    return tail.isdigit() and int(tail) == _checksum(line)


def _alpha5_to_int(field: str) -> int | None:
    """Decode a five-character catalogue field, Alpha-5 aware.

    Alpha-5 replaces the leading digit of a six-digit catalogue number with a
    letter: A=10 through Z=33, with I and O omitted because they read as 1
    and 0. A plain five-digit field decodes as itself.
    """
    text = field.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    head, rest = text[0].upper(), text[1:]
    if not rest.isdigit() or not head.isalpha() or head in ("I", "O"):
        return None
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # I and O removed
    if head not in alphabet:
        return None
    return (alphabet.index(head) + 10) * 10000 + int(rest)


def satnum_of(line1: str) -> int | None:
    """The catalogue number a TLE line claims, or None if unreadable."""
    return _alpha5_to_int(line1[2:7])


def _shape_ok(line1: str, line2: str) -> bool:
    if len(line1) != _TLE_LEN or len(line2) != _TLE_LEN:
        return False
    return line1.startswith("1 ") and line2.startswith("2 ")


def validate_native(line1: object, line2: object, sat_no: object) -> tuple[str, str] | None:
    """Return the native line pair if it is safe to serve, else None.

    Safe means: both lines are strings of the right shape, both checksums are
    correct, the two lines agree on a catalogue number, and that number is the
    one this record is filed under. The last test is the one that matters
    operationally: it is what stops a mispushed line being served under
    another object's name on an HRR-scoped plot.
    """
    if not isinstance(line1, str) or not isinstance(line2, str):
        return None
    l1, l2 = line1.rstrip(), line2.rstrip()
    if not _shape_ok(l1, l2):
        return None
    if not _checksum_ok(l1) or not _checksum_ok(l2):
        _log.warning("native TLE rejected for %s: checksum mismatch", sat_no)
        return None
    n1, n2 = satnum_of(l1), _alpha5_to_int(l2[2:7])
    if n1 is None or n1 != n2:
        _log.warning("native TLE rejected for %s: line1/line2 catalogue mismatch", sat_no)
        return None
    expected = _alpha5_to_int(str(sat_no or "").strip())
    if expected is not None and expected != n1:
        _log.warning(
            "native TLE rejected for %s: line claims catalogue %s", sat_no, n1)
        return None
    return l1, l2


# --------------------------------------------------------------------------
# Release gating
# --------------------------------------------------------------------------
def is_copyable(marking: str | None) -> bool:
    """Whether a line may be offered for copy-out.

    Fail closed. Only a plain unclassified marking with no caveat passes: a
    proprietary caveat (`U//PR-...`) carries redistribution terms this
    application has no basis to waive, and anything above unclassified is out
    of scope for a copy button regardless of caveat.
    """
    text = (marking or "").strip().upper()
    if not text:
        return False
    parts = [p.strip() for p in text.split("//") if p.strip()]
    if not parts or parts[0] not in ("U", "UNCLASSIFIED"):
        return False
    return len(parts) == 1


def copy_denied_reason(marking: str | None) -> str:
    """A short, honest explanation for a withheld copy control."""
    text = (marking or "").strip().upper()
    if not text:
        return "no classification marking on the record"
    if "PR-" in text or text.endswith("PR"):
        return "proprietary source: copy-out terms not confirmed"
    if text.split("//")[0] not in ("U", "UNCLASSIFIED"):
        return "marking above unclassified"
    return "caveated marking: copy-out withheld"


# --------------------------------------------------------------------------
# Candidate selection
# --------------------------------------------------------------------------
def _epoch_key(el: dict) -> str:
    return str(el.get("epoch") or "")


def candidates_of(obj: dict) -> list[dict]:
    """Every retained element set for an object, newest first.

    Falls back to the single legacy `elset` for records written before
    per-source retention existed, so an existing store keeps working
    unchanged rather than losing its TLEs on upgrade.
    """
    by_source = obj.get("elset_candidates")
    if isinstance(by_source, dict) and by_source:
        items = [el for el in by_source.values() if isinstance(el, dict)]
    else:
        legacy = obj.get("elset")
        items = [legacy] if isinstance(legacy, dict) else []
    return sorted(items, key=_epoch_key, reverse=True)


def select_elset(obj: dict, priority: tuple[str, ...] = DEFAULT_PRIORITY) -> dict | None:
    """Pick the element set to publish for this object.

    First class in `priority` that holds any candidate wins; newest epoch
    within it. An empty priority means no class ranking, so the newest
    candidate wins outright.
    """
    ranked = candidates_of(obj)
    if not ranked:
        return None
    for cls in priority:
        for el in ranked:
            if classify_source(el.get("source")) == cls:
                return el
    return ranked[0]


def provenance_of(el: dict, *, native: bool) -> dict:
    """The label served alongside a line so an operator can see its origin.

    Every field here is read from the record. Nothing is inferred, and an
    absent value stays absent rather than being filled with a plausible one.
    """
    marking = el.get("classification") or "U"
    source = el.get("source") or ""
    copyable = is_copyable(marking)
    return {
        "source": source,
        "source_class": classify_source(source),
        "epoch": el.get("epoch"),
        "native": native,
        "classification_marking": marking,
        "rev_no": el.get("rev_no"),
        "ephem_type": el.get("ephem_type"),
        "copyable": copyable,
        "copy_denied_reason": None if copyable else copy_denied_reason(marking),
    }


def age_hours(el: dict, now: datetime | None = None) -> float | None:
    """Hours between the element set's epoch and `now`, or None if unparseable."""
    raw = el.get("epoch")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return round((ref - dt).total_seconds() / 3600.0, 2)
