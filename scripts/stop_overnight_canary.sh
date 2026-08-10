#!/usr/bin/env bash
set -euo pipefail

PID_FILE="${KALSHI_CANARY_PID_FILE:-${TMPDIR:-/tmp}/kalshi-overnight-canary.pid}"
if [[ ! -f "$PID_FILE" ]]; then
  echo "No overnight canary PID file exists." >&2
  exit 0
fi
PID="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
if [[ ! "$PID" =~ ^[0-9]+$ ]]; then
  echo "BLOCKED: invalid overnight canary PID file" >&2
  exit 2
fi
is_running() {
  kill -0 "$PID" 2>/dev/null || return 1
  # A terminated child can remain as a zombie until its parent reaps it.
  # kill -0 still succeeds for zombies, but the canary is no longer executing.
  [[ "$(awk '{print $3}' "/proc/$PID/stat" 2>/dev/null || true)" != "Z" ]]
}

if is_running; then
  COMMAND="$(tr '\0' ' ' <"/proc/$PID/cmdline" 2>/dev/null || true)"
  [[ "$COMMAND" == *"kalshi-overnight-canary"* ]] || {
    echo "BLOCKED: recorded PID is not the overnight canary" >&2
    exit 2
  }
  kill -TERM "$PID"
  for _ in {1..50}; do
    is_running || break
    sleep 0.1
  done
  if is_running; then
    echo "BLOCKED: canary did not stop after SIGTERM" >&2
    exit 2
  fi
  echo "Stopped overnight canary PID=$PID" >&2
else
  echo "Overnight canary PID=$PID was not running." >&2
fi
rm -f -- "$PID_FILE"
