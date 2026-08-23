"""Native UDL TLE capture, validation, selection and release gating.

The line fixtures here are real: they are produced by `sgp4.exporter` (the
same exporter the fallback path uses) or built by mutating one of those and
recomputing the checksum, so every "valid" line in this file genuinely
checksums and every "invalid" one genuinely does not. Nothing is hand-typed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psirens.conjunction import tle_for, tle_lines
from psirens.sources import _elset_dict
from psirens.tle import (CLASS_COMMERCIAL, CLASS_GOVERNMENT, CLASS_UNKNOWN,
                         DEFAULT_PRIORITY, age_hours, candidates_of,
                         classify_source, copy_denied_reason, is_copyable,
                         parse_priority, satnum_of, select_elset,
                         validate_native)

EP = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _el(oid="41836", *, epoch=EP, source="LeoLabs", marking="U",
        line1="", line2="", rev_no=None):
    return _elset_dict(
        sat_no=oid, epoch=epoch, inclination_deg=0.05, eccentricity=0.0002,
        raan_deg=80.0, argp_deg=90.0, mean_anomaly_deg=100.0,
        mean_motion_rev_per_day=1.0027379, bstar=1e-5,
        classification=marking, source=source,
        line1=line1, line2=line2, rev_no=rev_no)


def _real_lines(oid="41836"):
    """A genuine exporter-produced, checksum-correct line pair."""
    lines = tle_lines(_el(oid))
    assert lines is not None
    return lines


def _recheck(line: str) -> str:
    """Recompute the mod-10 checksum so a mutated fixture stays a valid TLE."""
    total = sum(int(c) for c in line[:68] if c.isdigit())
    total += sum(1 for c in line[:68] if c == "-")
    return line[:68] + str(total % 10)


# -- validation ---------------------------------------------------------
def test_exporter_lines_validate_against_their_own_satno():
    l1, l2 = _real_lines("41836")
    assert validate_native(l1, l2, "41836") == (l1, l2)


def test_alpha5_line_validates_for_six_digit_catalogue():
    l1, l2 = _real_lines("100172")
    assert l1[2:7] == "A0172"
    assert satnum_of(l1) == 100172
    assert validate_native(l1, l2, "100172") == (l1, l2)


def test_native_line_for_another_object_is_rejected():
    """The cross-check that matters: a mispushed line must never be served
    under the wrong object on an HRR-scoped plot."""
    l1, l2 = _real_lines("41836")
    assert validate_native(l1, l2, "28924") is None


def test_broken_checksum_is_rejected():
    l1, l2 = _real_lines("41836")
    bad = l1[:68] + str((int(l1[68]) + 1) % 10)
    assert validate_native(bad, l2, "41836") is None


def test_lines_disagreeing_on_catalogue_are_rejected():
    l1, _ = _real_lines("41836")
    _, l2_other = _real_lines("28924")
    assert validate_native(l1, l2_other, "41836") is None


def test_wrong_shape_and_wrong_type_are_rejected():
    l1, l2 = _real_lines("41836")
    assert validate_native(l1[:40], l2, "41836") is None
    assert validate_native("2 " + l1[2:], l2, "41836") is None
    assert validate_native(None, l2, "41836") is None
    assert validate_native(12345, l2, "41836") is None


def test_trailing_whitespace_is_tolerated():
    l1, l2 = _real_lines("41836")
    assert validate_native(l1 + "   ", l2 + "\n", "41836") == (l1, l2)


def test_non_numeric_object_id_skips_the_satno_crosscheck():
    """A manual or demo object has no catalogue number to check against; the
    line is still shape- and checksum-validated."""
    l1, l2 = _real_lines("41836")
    assert validate_native(l1, l2, "MANUAL-A") == (l1, l2)


def test_mutated_line_with_recomputed_checksum_still_validates():
    """Guards the fixture helper itself: _recheck must produce valid lines."""
    l1, l2 = _real_lines("41836")
    rev = _recheck(l1[:63] + "  999")
    assert validate_native(rev, l2, "41836") is not None


# -- source classification ----------------------------------------------
def test_known_providers_classify_correctly():
    for name in ("LeoLabs", "EXO", "Cloudstone", "NorthStar", "KBR", "KRTL"):
        assert classify_source(name) == CLASS_COMMERCIAL
    for name in ("18 SPCS", "18th SPCS", "USSF", "JCO"):
        assert classify_source(name) == CLASS_GOVERNMENT


def test_unrecognised_source_is_unknown_not_promoted():
    assert classify_source("SOME-NEW-FEED") == CLASS_UNKNOWN
    assert classify_source("") == CLASS_UNKNOWN
    assert classify_source(None) == CLASS_UNKNOWN


# -- policy parsing -----------------------------------------------------
def test_policy_defaults_and_any():
    assert parse_priority(None) == DEFAULT_PRIORITY
    assert parse_priority("") == DEFAULT_PRIORITY
    assert parse_priority("nonsense") == DEFAULT_PRIORITY
    assert parse_priority("any") == ()


def test_policy_order_is_honoured():
    assert parse_priority("government,commercial") == (CLASS_GOVERNMENT,
                                                       CLASS_COMMERCIAL)


# -- selection ----------------------------------------------------------
def _multi_source_object():
    """One object with a fresher government fix and an older commercial one."""
    return {"elset_candidates": {
        "18 SPCS": _el(source="18 SPCS", epoch=EP + timedelta(hours=6)),
        "LeoLabs": _el(source="LeoLabs", epoch=EP),
    }}


def test_default_policy_prefers_newest_commercial_over_fresher_government():
    chosen = select_elset(_multi_source_object(), DEFAULT_PRIORITY)
    assert chosen["source"] == "LeoLabs"


def test_newest_within_the_winning_class_wins():
    obj = {"elset_candidates": {
        "LeoLabs": _el(source="LeoLabs", epoch=EP),
        "EXO": _el(source="EXO", epoch=EP + timedelta(hours=3)),
        "18 SPCS": _el(source="18 SPCS", epoch=EP + timedelta(days=1)),
    }}
    assert select_elset(obj, DEFAULT_PRIORITY)["source"] == "EXO"


def test_government_is_used_when_no_commercial_fix_exists():
    obj = {"elset_candidates": {"18 SPCS": _el(source="18 SPCS")}}
    assert select_elset(obj, DEFAULT_PRIORITY)["source"] == "18 SPCS"


def test_any_policy_collapses_to_newest_fix_wins():
    chosen = select_elset(_multi_source_object(), parse_priority("any"))
    assert chosen["source"] == "18 SPCS"


def test_reversed_policy_prefers_government():
    chosen = select_elset(_multi_source_object(),
                          parse_priority("government,commercial"))
    assert chosen["source"] == "18 SPCS"


def test_legacy_single_elset_record_still_selects():
    """A store written before per-source retention must keep working."""
    obj = {"elset": _el(source="LeoLabs")}
    assert candidates_of(obj) == [obj["elset"]]
    assert select_elset(obj, DEFAULT_PRIORITY)["source"] == "LeoLabs"


def test_object_with_no_elements_selects_nothing():
    assert select_elset({}, DEFAULT_PRIORITY) is None
    assert candidates_of({}) == []


# -- release gating -----------------------------------------------------
def test_plain_unclassified_is_copyable():
    assert is_copyable("U") is True
    assert is_copyable("UNCLASSIFIED") is True


def test_proprietary_commercial_marking_is_not_copyable():
    assert is_copyable("U//PR-EXO") is False
    assert "proprietary" in copy_denied_reason("U//PR-EXO")


def test_marking_above_unclassified_is_not_copyable():
    assert is_copyable("S//NF") is False
    assert "above unclassified" in copy_denied_reason("S//NF")


def test_absent_marking_fails_closed():
    assert is_copyable("") is False
    assert is_copyable(None) is False
    assert "no classification marking" in copy_denied_reason("")


def test_other_caveats_fail_closed():
    assert is_copyable("U//DS-JCO-NOTIF") is False
    assert "withheld" in copy_denied_reason("U//DS-JCO-NOTIF")


# -- end to end through tle_for -----------------------------------------
def test_native_line_is_served_verbatim_and_labelled_native():
    l1, l2 = _real_lines("41836")
    obj = {"elset_candidates": {"LeoLabs": _el(
        source="LeoLabs", line1=l1, line2=l2, rev_no=4211)}}
    el, lines, prov = tle_for(obj, DEFAULT_PRIORITY)
    assert lines == [l1, l2]          # verbatim, not re-exported
    assert prov["native"] is True
    assert prov["source"] == "LeoLabs"
    assert prov["source_class"] == CLASS_COMMERCIAL
    assert prov["rev_no"] == 4211
    assert prov["copyable"] is True
    assert el["source"] == "LeoLabs"


def test_missing_native_line_falls_back_to_reconstruction():
    obj = {"elset_candidates": {"LeoLabs": _el(source="LeoLabs")}}
    _, lines, prov = tle_for(obj, DEFAULT_PRIORITY)
    assert lines is not None
    assert prov["native"] is False
    assert prov["rev_no"] is None     # reconstruction cannot recover it


def test_invalid_stored_native_line_falls_back_rather_than_serving():
    """Defence in depth: a line that slipped into the store must not be
    served just because it is present."""
    l1, l2 = _real_lines("28924")
    obj = {"elset_candidates": {"EXO": _el(
        "41836", source="EXO", line1=l1, line2=l2)}}
    _, lines, prov = tle_for(obj, DEFAULT_PRIORITY)
    assert lines != [l1, l2]
    assert prov["native"] is False


def test_proprietary_native_line_is_shown_but_not_copyable():
    l1, l2 = _real_lines("41836")
    obj = {"elset_candidates": {"EXO": _el(
        source="EXO", marking="U//PR-EXO", line1=l1, line2=l2)}}
    _, lines, prov = tle_for(obj, DEFAULT_PRIORITY)
    assert lines == [l1, l2]          # displayed
    assert prov["copyable"] is False  # but not released
    assert prov["copy_denied_reason"]


def test_tle_for_on_an_empty_object_returns_nothing():
    assert tle_for({}, DEFAULT_PRIORITY) == (None, None, None)


# -- age ----------------------------------------------------------------
def test_age_hours_measures_from_epoch():
    assert age_hours(_el(epoch=EP), now=EP + timedelta(hours=5)) == 5.0
    assert age_hours({}) is None
    assert age_hours({"epoch": "not-a-date"}) is None
