#!/bin/sh
# Serve the app offline, drive it in a real browser, assert the layout.
# Called by simulate-pipeline.sh; runnable on its own for debugging.
#
# Exit codes: 0 pass, 1 the layout assertions failed, 2 the probe could not run
# (no node or no playwright), which the caller decides how to treat.
set -eu
ROOT="${1:-$(pwd)}"
PORT="${LAYOUT_PROBE_PORT:-8137}"
command -v node >/dev/null 2>&1 || { echo "UNAVAILABLE: no node"; exit 2; }
NODE_PATH="${NODE_PATH:-$(npm root -g 2>/dev/null || echo '')}"
export NODE_PATH

DATA="$(mktemp -d)"
# Seed the high-interest list with the offline demo objects. Without it the
# watchlist is empty, the inspector never opens from a row, and the probe has
# no way in: the list is normally pulled from the notification feed, which is
# unreachable offline. This mirrors the local UI check in CLAUDE.md, and it
# changes display only: ingest filters on the list solely when UDL is enabled.
cat > "$DATA/hrr.json" <<'JSON'
{"marking":"U","source":"PROBE","origin":"LOCAL","regime":"GEO","count":5,
 "objects":{"41836":{"name":"SES-10","rank":2},
            "28924":{"name":"EUTELSAT 174A","rank":3},
            "43683":{"name":"BEIDOU-3 G1","rank":1},
            "41748":{"name":"USA 270","rank":1},
            "90210":{"name":"DRIFTER-1","rank":4}}}
JSON
# Demo mode: UDL_BASE_URL unset, so the offline synthetic belt populates the
# store. SCHEDULER_ENABLED=0 keeps the background loop out of the probe.
DATA_DIR="$DATA" SCHEDULER_ENABLED=0 PYTHONPATH=src \
  python3 -m uvicorn psirens.main:app --host 127.0.0.1 --port "$PORT" \
  >"$DATA/server.log" 2>&1 &
PID=$!
# Never pkill here: a process-kill inside a compound command has twice taken
# this project's own shell down with it. Kill the pid we started, only.
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
curl -fsS -X POST "http://127.0.0.1:$PORT/api/refresh" >/dev/null || {
  echo "FAIL: could not populate the demo store"; exit 1; }

node "$ROOT/tools/layout_probe.js" "http://127.0.0.1:$PORT"
