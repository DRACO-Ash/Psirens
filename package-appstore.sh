#!/bin/sh
# Produce the flat App Store upload: Dockerfile, lockfiles, src, tests, config
# at the ROOT (no wrapping folder). Runs sh-portable, no bash features.
set -eu
VERSION="${1:-1.0.0}"
OUT="psirens-appstore-${VERSION}.zip"
rm -f "$OUT"
zip -r "$OUT" \
  Dockerfile .dockerignore requirements.txt requirements-dev.txt \
  pyproject.toml sonar-project.properties README.md \
  src tests \
  -x '*/__pycache__/*' '*.pyc' >/dev/null
echo "wrote $OUT"
unzip -l "$OUT" | sed -n '1,40p'
