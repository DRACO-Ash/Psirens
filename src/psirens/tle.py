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

NOTHING IS FILLED IN (1.7.4, owner rule of 17 November 2026: "It should not
INVENT anything from ANY TLE"). A record with no marking is NOT "U": it has no
marking, and it is withheld. No TLE is reconstructed: where the provider issued
no line, none is shown. An element set is propagated only when its own record
says which theory it belongs to and that theory is SGP4, and it carries its own
B*; anything else is refused with the reason, never approximated.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import NamedTuple

_log = logging.getLogger("psirens.tle")


class ElsetProvenance(NamedTuple):
    """Where an element set came from, and the provider's own line pair.

    Grouped into one value rather than passed as loose arguments: these fields
    travel together, they are meaningless apart, and spreading them across a
    builder's signature pushed it past the SonarQube parameter cap (S107).
    Every field defaults to absent so a source that supplies none (demo,
    manual) constructs an empty provenance rather than inventing values.
    """

    source: str = ""
    line1: str = ""
    line2: str = ""
    rev_no: int | None = None
    mean_motion_dot: float | None = None
    mean_motion_ddot: float | None = None
    ephem_type: int | None = None
    data_mode: str = ""


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
    # Owner decision, 17 November 2026: SAFRAN is commercial. It was on neither
    # list, so it ranked "unknown", below every other provider, which is how
    # the COSMOS 2589 comparison's newest agreeing fix lost to NorthStar's.
    "SAFRAN",
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


# Every element `conjunction.build_satrec` needs. An element set missing any
# of them cannot be propagated, and none of them is ever defaulted.
_REQUIRED_ELSET_KEYS = (
    "epoch", "mean_motion_rev_per_day", "eccentricity", "argp_deg",
    "inclination_deg", "mean_anomaly_deg", "raan_deg", "bstar",
)

# Ephemeris type 0 (and 2, its older spelling) is the SGP4 theory this
# application propagates with. Anything else, SGP4-XP's type 4 among them,
# needs a different propagator, and approximating it with SGP4 would put an
# orbit on screen that the provider never issued.
_SGP4_TYPES = (0, 2)
_EPHEM_COL = 62   # TLE line 1, column 63 (1-based)


def ephem_type_of(el: dict) -> int | None:
    """The ephemeris type the RECORD states: its own field first, then column
    63 of its own validated native line 1. None when neither states one; the
    absence is reported, never filled in."""
    raw = el.get("ephem_type")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    line1 = str(el.get("line1") or "")
    if len(line1) > _EPHEM_COL and line1[_EPHEM_COL].isdigit():
        return int(line1[_EPHEM_COL])
    return None


def _elements_present(el: dict) -> bool:
    try:
        for key in _REQUIRED_ELSET_KEYS[1:]:
            float(el[key])  # raises on absent, None, or non-numeric
        _parse_iso(el["epoch"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def propagation_refusal(el: object) -> str | None:
    """None when this element set may be propagated, else the reason it may
    not. Every reason is a fact about the record, stated for the operator."""
    if not isinstance(el, dict) or not _elements_present(el):
        return "element set incomplete: an element or B* was not supplied"
    etype = ephem_type_of(el)
    if etype is None:
        return "ephemeris type not stated by the provider"
    if etype not in _SGP4_TYPES:
        return f"ephemeris type {etype} is not SGP4 and is not propagated here"
    return None


def is_propagatable(el: object) -> bool:
    """Whether an element set can be turned into a Satrec without anything
    being assumed about it."""
    return propagation_refusal(el) is None


def _is_usable(el: object) -> bool:
    """A candidate worth considering for display: it has an epoch and the
    provider's own classification marking. An unmarked record is withheld."""
    return (isinstance(el, dict) and bool(el.get("epoch"))
            and bool(str(el.get("classification") or "").strip()))


# A provider's element set more than this many hours older than the newest
# one held for the same object and mode is AGED OUT (1.7.5): it is neither
# selected nor compared. Relative to the newest held, not to the clock, so a
# feed outage ages nothing out and never blanks every TLE at once. Until 1.7.5
# a provider that stopped reporting kept winning selection by class for the
# whole 90-day retention window (audit finding F12).
ELSET_MAX_LAG_HOURS = 48.0


def mode_of(el: dict) -> str:
    """The data mode an element set states. A candidate stored before 1.7.4
    carries none; it was filed under the REAL key, so it is read as REAL."""
    return str(el.get("data_mode") or "REAL")


def _parse_iso(raw: object) -> datetime:
    """An ISO epoch, a trailing Z read as UTC, a naive value as UTC. Raises
    ValueError on anything unparseable. The single parser in this module."""
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def epoch_of(el: dict) -> datetime | None:
    try:
        return _parse_iso(el.get("epoch"))
    except (TypeError, ValueError):
        return None


def usable_candidates(obj: dict) -> list[dict]:
    """Every usable retained element set for an object, newest first, before
    any ageing. Falls back to the single legacy `elset` for records written
    before per-source retention existed."""
    by_source = obj.get("elset_candidates")
    items: list[dict] = []
    if isinstance(by_source, dict):
        items = [el for el in by_source.values() if _is_usable(el)]
    if not items and _is_usable(obj.get("elset")):
        items = [obj["elset"]]
    return sorted(items, key=_epoch_key, reverse=True)


def split_aged(items: list[dict],
               max_lag_hours: float | None = ELSET_MAX_LAG_HOURS) -> tuple[list[dict], list[dict]]:
    """(fresh, aged) element sets, judged PER MODE against the newest of that
    mode, so a newer SIMULATED set can never age a REAL one out."""
    if max_lag_hours is None:
        return list(items), []
    newest: dict[str, datetime] = {}
    for el in items:
        when = epoch_of(el)
        mode = mode_of(el)
        if when is not None and (mode not in newest or when > newest[mode]):
            newest[mode] = when
    fresh, aged = [], []
    for el in items:
        when, top = epoch_of(el), newest.get(mode_of(el))
        lag = None if when is None or top is None else (top - when).total_seconds() / 3600.0
        (aged if lag is None or lag > max_lag_hours else fresh).append(el)
    return fresh, aged


def candidates_of(obj: dict,
                  max_lag_hours: float | None = ELSET_MAX_LAG_HOURS) -> list[dict]:
    """Every usable, current element set for an object, newest first.

    Propagation is decided separately (`propagation_refusal`), so an element
    set that cannot be screened can still have its provider's own line
    displayed, with the reason stated. Aged-out sets are dropped here.
    """
    return split_aged(usable_candidates(obj), max_lag_hours)[0]


def _is_real(el: dict) -> bool:
    return mode_of(el) == "REAL"


def policy_rank(source: str | None, priority: tuple[str, ...]) -> int:
    """Position of a provider's class in the selection policy; lower wins.
    An empty policy (`any`) ranks every class equal, so newest wins."""
    cls = classify_source(source)
    return priority.index(cls) if cls in priority else len(priority)


def by_policy(items: list[dict], priority: tuple[str, ...]) -> list[dict]:
    """Element sets in selection order: policy class, then newest first."""
    newest_first = sorted(items, key=_epoch_key, reverse=True)
    return sorted(newest_first, key=lambda el: policy_rank(el.get("source"), priority))


def select_elset(obj: dict, priority: tuple[str, ...] = DEFAULT_PRIORITY,
                 max_lag_hours: float | None = ELSET_MAX_LAG_HOURS) -> dict | None:
    """Pick the element set to publish for this object.

    REAL element sets first, whenever the object holds any (1.7.4): an
    EXERCISE or SIMULATED set filed under a real catalogue number must not
    become that object's TLE. Then the first class in `priority` that holds
    any candidate wins, newest epoch within it. An empty priority means no
    class ranking, so the newest candidate wins outright. A set more than
    `max_lag_hours` older than the newest of its mode is never chosen (1.7.5).

    The served path goes through `agreement.resolve`, which applies this same
    order AFTER setting aside a provider the others contradict.
    """
    ranked = candidates_of(obj, max_lag_hours)
    if not ranked:
        return None
    real = [el for el in ranked if _is_real(el)]
    ordered = by_policy(real or ranked, priority)
    return ordered[0]


def provenance_of(el: dict, *, native: bool) -> dict:
    """The label served alongside a line so an operator can see its origin.

    Every field here is read from the record. Nothing is inferred, and an
    absent value stays absent rather than being filled with a plausible one:
    until 1.7.4 an unmarked record was labelled "U" here and then offered for
    copy-out, which made the "no classification marking" refusal unreachable.
    """
    marking = str(el.get("classification") or "").strip() or None
    source = el.get("source") or ""
    copyable = is_copyable(marking)
    return {
        "source": source,
        "source_class": classify_source(source),
        "epoch": el.get("epoch"),
        "native": native,
        "classification_marking": marking,
        "rev_no": el.get("rev_no"),
        "ephem_type": ephem_type_of(el),
        "data_mode": el.get("data_mode"),
        "propagation_refused": propagation_refusal(el),
        "copyable": copyable,
        "copy_denied_reason": None if copyable else copy_denied_reason(marking),
    }


def age_hours(el: dict, now: datetime | None = None) -> float | None:
    """Hours between the element set's epoch and `now`, or None if unparseable."""
    raw = el.get("epoch")
    if not raw:
        return None
    try:
        dt = _parse_iso(raw)
    except ValueError:
        return None
    ref = now or datetime.now(timezone.utc)
    return round((ref - dt).total_seconds() / 3600.0, 2)
