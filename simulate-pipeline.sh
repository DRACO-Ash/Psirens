#!/bin/sh
# Reproduce the App Store python template test stage EXACTLY as the platform
# runs it: install ONLY requirements.txt (never requirements-dev.txt), then run
# the platform's exact pytest command. A divergence here once let a green local
# run fail the platform with `pytest: command not found`; this must not drift.
set -eu
SIM="$(mktemp -d)"
cp -r Dockerfile requirements.txt pyproject.toml sonar-project.properties src tests "$SIM"/
cd "$SIM"
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt            # platform step 1 (this file only)
pytest --cov --cov-report=xml:coverage.xml  # platform step 2 (exact command)
test -s coverage.xml || { echo "FAIL: coverage.xml missing/empty"; exit 1; }
# SonarQube python:S3776 proxy: no function may exceed cognitive complexity 15.
# Plus python:S107: no function may exceed 13 parameters. Both are flagged
# server-side only; check them here so a green local run cannot pass a smell
# the gate will reject (a real 1.4.0 miss on parse_hrr, and a real 1.5.0 miss
# on _elset_dict at 18 parameters, which cost a full upload cycle).
pip install -q cognitive_complexity >/dev/null 2>&1
python3 - <<'PY' || { echo "FAIL: cognitive complexity >15 or parameters >13 (see above)"; exit 1; }
import ast, glob, sys
from cognitive_complexity.api import get_cognitive_complexity
MAX_CC, MAX_PARAMS = 15, 13
over = []
for f in sorted(glob.glob("src/psirens/*.py")):
    for n in ast.walk(ast.parse(open(f).read())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cc = get_cognitive_complexity(n)
            if cc > MAX_CC:
                over.append(f"{f}:{n.lineno} {n.name} cc={cc} (max {MAX_CC}, S3776)")
            a = n.args
            params = (len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
                      + (1 if a.vararg else 0) + (1 if a.kwarg else 0))
            if params > MAX_PARAMS:
                over.append(f"{f}:{n.lineno} {n.name} params={params} (max {MAX_PARAMS}, S107)")
if over:
    print("\n".join(over)); sys.exit(1)
print(f"cognitive complexity OK (<={MAX_CC}) and parameter counts OK (<={MAX_PARAMS})")
PY
# SonarQube JS "prefer globalThis over window" (S6643) proxy: eslint-plugin-sonarjs
# does not carry this rule, so grep the served SPA for window.* member access.
if grep -nE "\bwindow\." src/psirens/static/index.html; then
  echo "FAIL: use globalThis instead of window.* in the SPA (SonarQube S6643)"; exit 1
fi
# SonarQube Web:S6819 proxy: prefer native elements over ARIA landmark roles
# (region->section, banner->header, navigation->nav, main->main, etc.). The
# eslint proxy cannot parse HTML a11y, so grep the SPA for these roles.
if grep -nE 'role="(region|banner|navigation|main|contentinfo|complementary|form)"' src/psirens/static/index.html; then
  echo "FAIL: use the native element instead of the ARIA role above (SonarQube S6819)"; exit 1
fi
# SonarQube "prefer dataset over getAttribute" proxy: a data-* attribute read
# via getAttribute should use element.dataset.* instead.
if grep -nE 'getAttribute\("data-' src/psirens/static/index.html; then
  echo "FAIL: use element.dataset.* instead of getAttribute(\"data-...\") above (SonarQube prefer-dataset)"; exit 1
fi
echo "SIMULATION GREEN (matches platform): tests passed, coverage.xml at $SIM/coverage.xml"