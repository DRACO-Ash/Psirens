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
# SPA lint gates.
# The grep below is the one that matters for the rule that failed the 1.5.2
# upload ("prefer throw over a returned rejected promise"). MEASURED, not
# assumed: eslint-plugin-sonarjs does NOT carry that rule, even with all 279
# of its rules enabled, so running eslint would NOT have caught it. Only a
# real sonar-scanner or this grep will.
# eslint still runs below because it covers a different, wider set of JS
# smells, and it carries its own canary so it can never report a silent pass.
if grep -nE "Promise\.reject" src/psirens/static/index.html; then
  echo "FAIL: prefer 'throw error' over 'return Promise.reject(error)' above (SonarJS)"; exit 1
fi
SPA_LINT_CACHE="${SPA_LINT_CACHE:-$HOME/.cache/psirens-spa-lint}"
if command -v node >/dev/null 2>&1; then
  mkdir -p "$SPA_LINT_CACHE"
  if [ ! -x "$SPA_LINT_CACHE/node_modules/.bin/eslint" ]; then
    (cd "$SPA_LINT_CACHE" && npm init -y >/dev/null 2>&1 \
      && npm install --no-audit --no-fund --loglevel=error eslint@9 eslint-plugin-sonarjs >/dev/null 2>&1) || true
  fi
  if [ -x "$SPA_LINT_CACHE/node_modules/.bin/eslint" ]; then
    # eslint resolves its plugin and its base path from the working directory,
    # so the config, the node_modules and the extracted script must all sit in
    # $SIM. Linting from elsewhere makes eslint silently ignore the file and
    # report success, which is worse than having no gate at all.
    ln -sfn "$SPA_LINT_CACHE/node_modules" node_modules
    cat > eslint.config.mjs <<'ESLINTCFG'
import sonarjs from 'eslint-plugin-sonarjs';
export default [
  { files: ['**/*.js'],
    languageOptions: { ecmaVersion: 2023, sourceType: 'script' },
    plugins: { sonarjs },
    rules: { ...sonarjs.configs.recommended.rules, 'no-negated-condition': 'error' } },
];
ESLINTCFG
    python3 - <<'EXTRACT'
import re
src = open("src/psirens/static/index.html").read()
blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
assert blocks, "no inline script found in the SPA"
open("_spa_lint.js", "w").write("\n".join(blocks))
# A deliberate violation of a rule the PLUGIN genuinely carries
# (sonarjs/no-invariant-returns). If it is not flagged, eslint is not really
# running here and the step must not report a pass. Verified by hand that
# this snippet trips the rule and that a clean file does not.
open("_spa_lint_canary.js", "w").write(
    "function canary(flag) {\n"
    "  if (flag) { return 'same'; }\n"
    "  return 'same';\n"
    "}\n")
EXTRACT
    node --check _spa_lint.js || { echo "FAIL: SPA script is not valid JavaScript"; exit 1; }
    # Prove the gate bites before trusting it to pass.
    if ./node_modules/.bin/eslint --config eslint.config.mjs _spa_lint_canary.js >/dev/null 2>&1; then
      echo "FAIL: SPA lint canary was NOT flagged; the linter is not running"; exit 1
    fi
    if ./node_modules/.bin/eslint --config eslint.config.mjs _spa_lint.js; then
      echo "SPA lint OK (eslint + eslint-plugin-sonarjs bit the canary, no findings in the SPA)"
    else
      echo "FAIL: SonarJS findings in the SPA (see above)"; exit 1
    fi
    rm -f _spa_lint.js _spa_lint_canary.js eslint.config.mjs node_modules
  else
    echo "WARNING: SPA lint SKIPPED (could not install eslint-plugin-sonarjs)."
    echo "         Run it by hand before upload; two gate failures came from here."
  fi
else
  echo "WARNING: SPA lint SKIPPED (no node on this machine)."
  echo "         Run it by hand before upload; two gate failures came from here."
fi
echo "SIMULATION GREEN (matches platform): tests passed, coverage.xml at $SIM/coverage.xml"