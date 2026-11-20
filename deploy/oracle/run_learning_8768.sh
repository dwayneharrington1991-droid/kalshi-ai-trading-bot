#!/usr/bin/env bash
# Scheduled, read-only learning run for 8768 (safe to repeat; never overlaps itself).
#   run_learning_8768.sh <commit-sha> [journal]
# Lock: one run at a time (flock). Priority: lowest CPU and I/O. The journal is
# opened read-only by the runner. Status is written atomically beside the
# journal; if the journal is missing or the run fails, the previous status is kept.
# Never touches the live bot, ports, the model, Telegram or trading.
set -uo pipefail
SHA=${1:-}
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || { echo "usage: $0 <40-hex commit sha> [journal]"; exit 2; }
REPO=${LEARN_REPO:-/home/opc/kalshi-ai-trading-bot}
PY=${LEARN_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
J=${2:-${LEARN_JOURNAL:-}}
[[ -n $J ]] || { echo "LEARNING: no journal path given"; exit 2; }
[[ -f $J ]] || { echo "LEARNING: journal not found; previous status kept"; exit 3; }
STATUS=$J.learning-status.json
LOCK=$J.learning.lock
WORK=$(mktemp -d) || exit 4
trap 'rm -rf "$WORK"' EXIT
exec 9>"$LOCK" || exit 4
flock -n 9 || { echo "LEARNING: another run holds the lock; nothing done"; exit 0; }
git -C "$REPO" archive "$SHA" src | tar -x -C "$WORK" || { echo "LEARNING: could not extract code"; exit 4; }
( cd "$WORK" && nice -n 19 ionice -c3 timeout 1200 "$PY" -m src.signals_display.learning_runner \
    --journal "$J" --status-file "$STATUS.next" ) >"$WORK/run.log" 2>&1 || { echo "LEARNING: run failed; previous status kept"; tail -n 5 "$WORK/run.log"; rm -f "$STATUS.next"; exit 5; }
mv -f "$STATUS.next" "$STATUS" && chmod 600 "$STATUS"
echo "LEARNING: status published ($STATUS); live model and trading unchanged"
