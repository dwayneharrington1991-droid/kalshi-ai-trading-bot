#!/usr/bin/env bash
# Scheduled learning cycle for 8768: retrain -> shadow qualify -> controlled promotion.
#   run_lifecycle_8768.sh <commit-sha> <journal> <live-artifact>
# Safe to repeat: one run at a time (flock), lowest CPU and I/O priority, hard time limit,
# and it refuses to start inside a 15-minute opening window. Read-only against the live
# bot and the journal; writes only inside <journal dir>/lifecycle and the status file.
# Promotion itself is blocked unless a verified activator is configured (none is by default).
set -uo pipefail
SHA=${1:-}; J=${2:-}; LIVE=${3:-}
[[ $SHA =~ ^[0-9a-f]{40}$ && -n $J && -f $J && -f $LIVE ]] || { echo "usage: $0 <40-hex sha> <journal> <live-artifact>"; exit 2; }
REPO=${LEARN_REPO:-/home/opc/kalshi-ai-trading-bot}
PY=${LEARN_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
STATE=$(dirname "$J")/lifecycle
mkdir -p "$STATE" && chmod 700 "$STATE"
exec 9>"$STATE/cycle.lock" || exit 4
flock -n 9 || { echo "LIFECYCLE: another cycle is running; nothing done"; exit 0; }
WORK=$(mktemp -d) || exit 4
trap 'rm -rf "$WORK"' EXIT
git -C "$REPO" archive "$SHA" src | tar -x -C "$WORK" || { echo "LIFECYCLE: could not extract code"; exit 4; }
# Refuse to start inside a 15-minute opening window (no model work near a market opening).
if ! ( cd "$WORK" && "$PY" -c 'import sys; from datetime import datetime,timezone; from src.signals_display.model_lifecycle import opening_window_safe as s; sys.exit(0 if s(datetime.now(timezone.utc)) else 3)' ); then
  echo "LIFECYCLE: inside a 15-minute opening window; nothing done (retried next schedule)"; exit 0
fi
( cd "$WORK" && nice -n 19 ionice -c3 timeout 1500 "$PY" -m src.signals_display.lifecycle_runner \
    --journal "$J" --state-dir "$STATE" --commit "$SHA" --live-artifact "$LIVE" ) > "$WORK/run.log" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then echo "LIFECYCLE: cycle failed (exit $rc); live model unchanged; last good status kept"; tail -n 5 "$WORK/run.log"; exit 5; fi
echo "LIFECYCLE: cycle complete; live model unchanged unless a verified promotion succeeded"
