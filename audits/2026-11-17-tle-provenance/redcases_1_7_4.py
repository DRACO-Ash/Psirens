"""Prove each 1.7.4 guard by reintroducing the 1.7.3 behaviour it replaces."""
import pathlib, subprocess, sys
ROOT = pathlib.Path("/home/user/Psirens")
CASES = [
 ("store keyed on epoch alone (1.7.3)", "src/psirens/store.py",
  "        key = (source, clean[\"epoch\"])", "        key = (\"\", clean[\"epoch\"])",
  "test_two_providers_at_one_instant_stay_two_observations"),
 ("missing elements zero-filled (1.7.3)", "src/psirens/sources.py",
  "        if val is None or val == \"\":\n            return None", "        if val is None or val == \"\":\n            val = 0.0",
  "test_a_row_missing_any_element_is_rejected_not_zero_filled"),
 ("missing marking becomes U (1.7.3)", "src/psirens/sources.py",
  "    marking = str(row.get(\"classificationMarking\") or \"\").strip()", "    marking = str(row.get(\"classificationMarking\") or \"U\").strip()",
  "test_a_row_without_provenance_is_rejected"),
 ("missing mode becomes REAL (1.7.3)", "src/psirens/sources.py",
  "        mode = DataMode(str(row.get(\"dataMode\") or \"\").strip().upper())", "        mode = DataMode(str(row.get(\"dataMode\") or \"REAL\").strip().upper())",
  "test_a_row_without_provenance_is_rejected"),
 ("unmarked TLE labelled U (1.7.3)", "src/psirens/tle.py",
  "    marking = str(el.get(\"classification\") or \"\").strip() or None", "    marking = el.get(\"classification\") or \"U\"",
  "test_an_unmarked_element_set_is_never_called_unclassified"),
 ("demo beside UDL when DEMO_MODE (1.7.3)", "src/psirens/main.py",
  "    if not cfg.udl_enabled:\n        sources.append(DemoElsetSource())", "    if cfg.demo_mode or not cfg.udl_enabled:\n        sources.append(DemoElsetSource())",
  "test_the_demo_belt_is_never_built_beside_live_udl"),
 ("no purge on a live start", "src/psirens/main.py",
  "        store.purge_synthetic()   # nothing synthetic survives into a live store", "        pass",
  "test_a_live_start_purges_anything_synthetic"),
 ("record-level mode labels every sample (1.7.3)", "src/psirens/main.py",
  "            mode = DataMode(prov[\"data_mode\"]) if prov else legacy", "            mode = legacy",
  "test_the_real_view_serves_only_samples_tagged_real"),
 ("manual set filed under the typed source (1.7.3)", "src/psirens/sources.py",
  "        tags = {\"source\": MANUAL_PROVIDER, \"data_mode\": i[\"data_mode\"],", "        tags = {\"source\": i.get(\"source\") or MANUAL_PROVIDER, \"data_mode\": i[\"data_mode\"],",
  "test_a_manual_set_is_filed_under_the_operator_not_a_provider"),
 ("legacy sample adopted without an exact match", "src/psirens/store.py",
  "    if legacy is None or not _same_values(legacy, clean):", "    if legacy is None:",
  "test_a_legacy_sample_is_tagged_only_when_a_re_pull_matches_it_exactly"),
]
bad = 0
for label, rel, old, new, test in CASES:
    path = ROOT / rel; src = path.read_text()
    assert src.count(old) == 1, (label, "anchor not found")
    path.write_text(src.replace(old, new))
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
                            "tests/test_provenance.py", "-k", test], cwd=ROOT, capture_output=True, text=True)
    finally:
        path.write_text(src)
    went_red = r.returncode != 0
    bad += not went_red
    print(("RED (guard holds)  " if went_red else "STILL GREEN (VACUOUS) ") + label)
print(f"\n{len(CASES) - bad} of {len(CASES)} guards proved by revert")
sys.exit(1 if bad else 0)
