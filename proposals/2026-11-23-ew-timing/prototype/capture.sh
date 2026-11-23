#!/bin/sh
# Serve the app offline exactly as tools/layout_check.sh does, compute the
# mockup values from it, and capture figures A and B. Run from the repo root.
set -eu
HERE=proposals/2026-11-23-ew-timing
PORT=8791; DATA="$(mktemp -d)"
sed -n '/^cat > "\$DATA\/hrr.json"/,/^JSON$/p' tools/layout_check.sh | sed '1d;$d' > "$DATA/hrr.json"
PYTHONPATH="src:$HERE/prototype" python3 "$HERE/prototype/seed_example.py" "$DATA" "$HERE/prototype/seed.json"
python3 - "$DATA/hrr.json" <<'PY'
import json, sys
h = json.load(open(sys.argv[1])); h["objects"]["EXAMPLE-1"] = {"name": "BURN-EXAMPLE", "country": "CHN", "rank": 1}
h["count"] = len(h["objects"]); json.dump(h, open(sys.argv[1], "w"))
PY
# An unreachable UDL keeps the demo belt from merging over the seed (CLAUDE.md, local UI check).
UDL_BASE_URL=http://127.0.0.1:1/ DATA_DIR="$DATA" SCHEDULER_ENABLED=0 PYTHONPATH=src python3 -m uvicorn psirens.main:app \
  --host 127.0.0.1 --port "$PORT" >"$DATA/server.log" 2>&1 &
PID=$!; trap 'kill $PID 2>/dev/null || true; rm -rf "$DATA"' EXIT
i=0; until curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 60 ] && exit 1; sleep 0.5; done
PYTHONPATH="src:$HERE/prototype" python3 "$HERE/prototype/mock_values.py" "http://127.0.0.1:$PORT" "$HERE/prototype/mock.json"
NODE_PATH="$(npm root -g)" node "$HERE/prototype/capture.js" "http://127.0.0.1:$PORT" "$HERE/prototype/mock.json" "$HERE/figures"
