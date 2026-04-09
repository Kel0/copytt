#!/usr/bin/env bash
set -euo pipefail

# Start the bot and the web UI as siblings; if either dies, kill the other
# and exit non-zero so the container restarts under any orchestrator.

python copytrader.py &
BOT_PID=$!

python webapp.py &
WEB_PID=$!

term() {
  echo "shutting down..."
  kill -TERM "$BOT_PID" "$WEB_PID" 2>/dev/null || true
  wait "$BOT_PID" "$WEB_PID" 2>/dev/null || true
  exit 0
}
trap term INT TERM

# Exit as soon as either process exits.
wait -n
EXIT=$?
echo "child exited ($EXIT), tearing down"
kill -TERM "$BOT_PID" "$WEB_PID" 2>/dev/null || true
wait 2>/dev/null || true
exit "$EXIT"
