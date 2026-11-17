"""Prove each 1.7.5 guard by reinstating the behaviour it replaces.

Anchors follow the 1.7.6 shape of agreement.py (streams keyed mode, then
provider); the behaviours each case reinstates are unchanged.

Each case edits ONE line of the shipped source back to the 1.7.4 behaviour
(or to the bypass the guard exists to stop), runs the one test that guards
it, and restores the file byte for byte. A test that stays green against
the reverted line is VACUOUS and the run fails. Run from the repository root
inside the project venv: python3 audits/2026-11-17-tle-provenance/redcases_1_7_5.py
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
T = "tests/test_agreement.py"
CASES = [
 ("providers pooled into one track (1.7.4, F1)", "src/psirens/agreement.py",
  "        out.streams[mode] = here[primary]",
  "        out.streams[mode] = sorted((s for r in here.values() for s in r), key=lambda s: s[\"epoch\"])",
  T, "test_cosmos_2589_northstar_is_set_aside_and_the_drift_is_real"),
 ("no outlier ever set aside", "src/psirens/agreement.py",
  "    agreed = {p for p in positions if _has_majority(p, positions, agree_deg)}",
  "    agreed = set(positions)",
  T, "test_cosmos_2589_northstar_is_set_aside_and_the_drift_is_real"),
 ("a disputed object still publishes a rate", "src/psirens/agreement.py",
  "    if block.get(\"status\") == DISPUTED:",
  "    if block.get(\"status\") == \"never\":",
  T, "test_two_providers_that_disagree_publish_no_drift_and_no_event"),
 ("an unverifiable provider preferred by class", "src/psirens/agreement.py",
  "    return by_policy(checked, policy.priority) + by_policy(rest, policy.priority)",
  "    return by_policy(checked + rest, policy.priority)",
  T, "test_an_unverifiable_provider_is_listed_not_judged_and_not_preferred"),
 ("untagged history mixed into a tagged track", "src/psirens/agreement.py",
  "        out.streams[mode] = here[primary]",
  "        out.streams[mode] = sorted(here[primary] + untagged, key=lambda s: s[\"epoch\"])",
  T, "test_untagged_history_is_never_mixed_with_tagged_samples"),
 ("silence judged on the track provider alone", "src/psirens/agreement.py",
  "    out.heard[mode] = heard or [s[\"epoch\"] for s in out.streams.get(mode, [])]",
  "    out.heard[mode] = [s[\"epoch\"] for s in out.streams.get(mode, [])]",
  T, "test_quiet_is_judged_on_any_provider_not_only_the_track_provider"),
 ("no residual ceiling on the published rate (1.7.4, F3)", "src/psirens/main.py",
  "    poor = fit.residual_deg > ceiling",
  "    poor = False",
  T, "test_a_fit_that_misses_its_data_publishes_no_rate"),
 ("no residual ceiling in the E-W detector (1.7.4, F4)", "src/psirens/events.py",
  "    if max(fb.residual_deg, fa.residual_deg) > th.max_residual:",
  "    if False:",
  T, "test_a_poor_fit_cannot_confirm_a_burn"),
 ("no age-out (1.7.4, F12)", "src/psirens/tle.py",
  "ELSET_MAX_LAG_HOURS = 48.0",
  "ELSET_MAX_LAG_HOURS = 1e9",
  T, "test_an_element_set_far_older_than_the_newest_is_aged_out"),
 ("an event carries a projection again", "src/psirens/events.py",
  "        events.extend(_object_events(obj, ref, th))",
  "        events.extend({**e, \"projection\": None} for e in _object_events(obj, ref, th))",
  "tests/test_events.py", "test_no_event_carries_a_projection"),
 ("the screen reads the raw store, not the resolved view (F5, F6)", "src/psirens/main.py",
  "    data = request.app.state.resolver.load()\n    payload = conjunctions_for(",
  "    data = request.app.state.store.load()\n    payload = conjunctions_for(",
  T, "test_the_api_serves_the_agreement_and_never_a_projection"),
]


def main() -> int:
    bad = 0
    for label, rel, old, new, tfile, test in CASES:
        path = ROOT / rel
        src = path.read_text()
        if src.count(old) != 1:
            print(f"ANCHOR MISSING          {label}")
            bad += 1
            continue
        path.write_text(src.replace(old, new))
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-cov",
                                "-p", "no:cacheprovider", tfile, "-k", test],
                               cwd=ROOT, capture_output=True, text=True, check=False)
        finally:
            path.write_text(src)
        went_red = r.returncode != 0
        bad += not went_red
        print(("RED (guard holds)       " if went_red else "STILL GREEN (VACUOUS)   ") + label)
    print(f"\n{len(CASES) - bad} of {len(CASES)} guards proved by revert")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
