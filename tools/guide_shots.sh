#!/bin/sh
# Serve the app over a representative seeded store and capture the Operator
# Guide's figures. Not part of the pipeline: run it when the interface changes
# and the guide's figures need to catch up.
#
# Exit codes: 0 captured, 1 capture failed, 2 cannot run (no node/playwright).
set -eu
ROOT="${1:-$(pwd)}"
PORT="${GUIDE_SHOT_PORT:-8139}"
command -v node >/dev/null 2>&1 || { echo "UNAVAILABLE: no node"; exit 2; }
NODE_PATH="${NODE_PATH:-$(npm root -g 2>/dev/null || echo '')}"
export NODE_PATH

AGE="${GUIDE_SHOT_AGE_HOURS:-0.5}"
MODE="${GUIDE_SHOT_MODE:-full}"
DATA="$(mktemp -d)"
python3 "$ROOT/tools/guide_seed.py" --data-dir "$DATA" \
  --snapshot "$ROOT/src/psirens/static/hrr-geo.json" --age-hours "$AGE"

# UDL_BASE_URL points at a dead port ON PURPOSE: it keeps udl_enabled true, so
# the HRR path behaves as in production, while guaranteeing no live pull can
# merge over the seeded store. SCHEDULER_ENABLED=0 keeps the loop out entirely.
DATA_DIR="$DATA" SCHEDULER_ENABLED=0 UDL_BASE_URL="http://127.0.0.1:1/" \
  PYTHONPATH="$ROOT/src" \
  python3 -m uvicorn psirens.main:app --host 127.0.0.1 --port "$PORT" \
  >"$DATA/server.log" 2>&1 &
PID=$!
cleanup() { kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; rm -rf "$DATA"; }
trap cleanup EXIT

i=0
while [ "$i" -lt 60 ]; do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  i=$((i + 1)); sleep 0.5
done
if [ "$i" -ge 60 ]; then
  echo "FAIL: the app did not become healthy on port $PORT"; tail -20 "$DATA/server.log"; exit 1
fi

node "$ROOT/tools/guide_shots.js" "http://127.0.0.1:$PORT" \
  "$ROOT/src/psirens/static" "$MODE"
