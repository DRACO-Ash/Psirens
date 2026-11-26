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
LEDGER = "tests/test_ledger.py"
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
 # 1.7.8, the consensus drift rate.
 ("the consensus counts a provider that was set aside", "src/psirens/agreement.py",
  "        names = [p for p in a.positions if p not in a.set_aside]",
  "        names = list(a.positions)",
  T, "test_the_consensus_never_counts_a_provider_set_aside"),
 ("the element-set rate measured over half an orbit", "src/psirens/agreement.py",
  "    period_d = 1.0 / n",
  "    period_d = 0.5 / n",
  T, "test_one_full_orbit_cancels_the_eccentricity_libration"),
 ("no consensus published, as in 1.7.7 (every object read no usable baseline)", "src/psirens/main.py",
  "    if cons is None:\n        out[\"drift_basis\"]",
  "    if True:\n        out[\"drift_basis\"]",
  T, "test_a_rate_is_published_with_hours_of_history_not_days"),
 ("the screen reads the raw store, not the resolved view (F5, F6)", "src/psirens/main.py",
  "    data = request.app.state.resolver.load()\n    payload = conjunctions_for(",
  "    data = request.app.state.store.load()\n    payload = conjunctions_for(",
  T, "test_the_api_serves_the_agreement_and_never_a_projection"),
 # 1.7.9, the live /api/meta of 23 November 2026.
 ("the diagnosis ignores added (1177 added read as no new epochs)", "src/psirens/refresh.py",
  "        if res.added:\n",
  "        if False:\n",
  "tests/test_ingest_health.py", "test_the_diagnosis_never_says_no_new_epochs_when_epochs_arrived"),
 ("a window still full at the smallest slice is a log line only", "src/psirens/sources.py",
  "            self.last_stats.truncated += 1\n",
  "            pass\n",
  "tests/test_ingest_health.py", "test_a_window_still_full_at_the_smallest_slice_reaches_the_operator"),
 ("a halved response is not counted", "src/psirens/sources.py",
  "                self.last_stats.halved += 1\n",
  "                pass\n",
  "tests/test_ingest_health.py", "test_a_halved_response_is_counted"),
 ("the cycle drops the truncation count", "src/psirens/refresh.py",
  "        res.truncated += getattr(stats, \"truncated\", 0)\n",
  "        pass\n",
  "tests/test_ingest_health.py", "test_a_window_still_full_at_the_smallest_slice_reaches_the_operator"),
 # 1.7.10, request volume tuned at Ash's call.
 ("the 24 h default slice again (a live day fetched about five times)", "src/psirens/config.py",
  '        udl_slice_hours=_env_int("UDL_SLICE_HOURS", 1),',
  '        udl_slice_hours=_env_int("UDL_SLICE_HOURS", 24),',
  "tests/test_sources.py", "test_the_default_slice_fetches_a_live_day_once"),
 # 1.7.11, the platform Logging check on 1.7.10 (two Medium findings).
 ("request lines without user_agent.original (Logging check on 1.7.10)", "src/psirens/logjson.py",
  '            out[USER_AGENT_ATTR] = _header(value)\n',
  '            pass\n',
  "tests/test_logging.py", "test_every_request_line_carries_the_user_agent_path_and_method"),
 ("lines inside a request without url.path and method", "src/psirens/logjson.py",
  '        attrs["url.path"] = scope.get("path", "")\n',
  '',
  "tests/test_logging.py", "test_every_request_line_carries_the_user_agent_path_and_method"),
 ("the audit line without event.outcome (Logging check on 1.7.10)", "src/psirens/logjson.py",
  '                          OUTCOME_ATTR: outcome(status["code"]),\n',
  '',
  "tests/test_logging.py", "test_the_audit_line_states_the_outcome"),
 # 1.7.12, east-west burn timing (owner approval, 24 November 2026).
 ("the burn stamped at the 3-hour split again", "src/psirens/events.py",
  "    when = cross.when if cross is not None else split.when\n",
  "    when = split.when\n",
  "tests/test_events.py", "test_a_burn_is_timed_where_the_fits_cross"),
 ("the event id keyed on the exact epoch, not the hour", "src/psirens/events.py",
  "               max(when, split.when) + timedelta(hours=EW_MIN_AFTER_H), hour_key(when))",
  "               max(when, split.when) + timedelta(hours=EW_MIN_AFTER_H), None)",
  "tests/test_events.py", "test_the_event_id_holds_as_data_arrives"),
 ("the stated range back at nominal 2 sigma", "src/psirens/events.py",
  "CROSS_COVERAGE_K = 2.6",
  "CROSS_COVERAGE_K = 2.0",
  "tests/test_events.py", "test_the_stated_range_covers_95_percent_of_true_burn_times"),
 # 1.7.13, the ledger store.
 ("a merge edits samples a reader already holds", "src/psirens/store.py",
  "        samples.append(dict(s) if key in want else s)",
  "        samples.append(s)",
  LEDGER, "test_a_view_taken_before_a_change_is_never_altered"),
 ("health parses the whole store again", "src/psirens/store.py",
  "        return self._newest\n\n    def stats",
  "        return _newest(_read_json(self.path))\n\n    def stats",
  LEDGER, "test_health_reads_no_file"),
 ("a merge rewrites the whole store", "src/psirens/store.py",
  "            self._append(delta)\n",
  "            self._append(delta)\n            self._snapshot()\n",
  LEDGER, "test_a_merge_writes_only_what_changed"),
 ("a history.json written after the ledger is ignored at boot", "src/psirens/store.py",
  "        if sig is not None and (data is None or sig[0] / 1e9 > ledger_mtime):",
  "        if sig is not None and data is None:",
  LEDGER, "test_a_history_file_written_after_the_ledger_is_adopted"),
 ("a history.json written while running is ignored", "src/psirens/store.py",
  "        if sig is not None and sig != self._file_sig:",
  "        if False:",
  LEDGER, "test_a_history_file_written_after_the_ledger_is_adopted"),
 ("the store decoded at construction (twice the memory under --preload)",
  "src/psirens/store.py",
  "        self.boot_seconds: float | None = None\n",
  "        self.boot_seconds: float | None = None\n        self.ensure()\n",
  LEDGER, "test_the_first_start_adopts_the_legacy_file_once"),
 ("a refused delete breaks compaction", "src/psirens/store.py",
  "                except OSError:       # file is simply never read again",
  "                except FileNotFoundError:",
  LEDGER, "test_a_mount_that_refuses_deletes_still_works"),
 ("segments before the first snapshot are never replayed", "src/psirens/store.py",
  "        if segs and not snaps:",
  "        if False:",
  T, "test_the_api_serves_the_agreement_and_never_a_projection"),
 # 1.7.14, bandit B101: each guard put back to the 1.7.13 assert.
 ("_snapshot guarded by assert again", "src/psirens/store.py",
  'newer of the two."""\n        if self._data is None:\n            raise RuntimeError(NOT_LOADED)\n',
  'newer of the two."""\n        assert self._data is not None\n',
  LEDGER, "under_python_O and _snapshot"),
 ("ensure guarded by assert again", "src/psirens/store.py",
  "                self._check_external()\n            if self._data is None:\n"
  "                raise RuntimeError(NOT_LOADED)\n",
  "                self._check_external()\n            assert self._data is not None\n",
  LEDGER, "under_python_O and ensure"),
 ("_purge_locked guarded by assert again", "src/psirens/store.py",
  "    def _purge_locked(self) -> int:\n        if self._data is None:\n"
  "            raise RuntimeError(NOT_LOADED)\n",
  "    def _purge_locked(self) -> int:\n        assert self._data is not None\n",
  LEDGER, "under_python_O and _purge_locked"),
 # 51, the segment trigger (Ash, 26 Nov).
 ("a segment backlog never makes compaction due", "src/psirens/store.py",
  "            if 0 < self.compact_segments <= self._since_snap:\n",
  "            if False:\n",
  LEDGER, "segment_backlog"),
 ("a pull never compacts its backlog", "src/psirens/refresh.py",
  "        self.compact_soon()\n        return {",
  "        return {",
  LEDGER, "backfill_chunk"),
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
