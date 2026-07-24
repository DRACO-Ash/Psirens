#!/bin/sh
# Reproduce the platform's test stage against the ACTUAL upload artefact,
# including its added CI file and GITLAB_CI=true, and verify coverage.xml.
# sh-portable (the platform runs under BusyBox sh, not bash).
set -eu
VERSION="${1:-1.0.0}"
sh package-appstore.sh "$VERSION"
SIM="$(mktemp -d)"
unzip -q "psirens-appstore-${VERSION}.zip" -d "$SIM"
printf 'stages: [test]\n' > "$SIM/.gitlab-ci.yml"   # platform commits its own
cd "$SIM"
python -m venv .venv
. .venv/bin/activate
pip install --quiet -r requirements.txt -r requirements-dev.txt
GITLAB_CI=true python -m pytest -q
test -s coverage.xml || { echo "FAIL: coverage.xml missing/empty"; exit 1; }
echo "SIMULATION GREEN: tests passed and coverage.xml present at $SIM/coverage.xml"
