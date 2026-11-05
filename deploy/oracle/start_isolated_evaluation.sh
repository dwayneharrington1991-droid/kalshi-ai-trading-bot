#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

target=${1:?exact deployed commit required}
config=/etc/kalshi-signals/deployment.env

fail() {
  echo "EVALUATION_LAUNCH_BLOCKED: $*" >&2
  exit 1
}

[[ $(id -un) == opc ]] || fail "must run as opc"
[[ -r "$config" ]] || fail "deployment configuration is unreadable"
# shellcheck source=/dev/null
source "$config"
: "${DEPLOY_ROOT:?}"

[[ -L "$DEPLOY_ROOT/current" ]] || fail "versioned current release is missing"
current=$(readlink -f "$DEPLOY_ROOT/current")
[[ -f "$current/.deployed-commit" ]] || fail "release identity is missing"
[[ $(cat "$current/.deployed-commit") == "$target" ]] ||
  fail "running release does not match requested evaluation commit"

evaluator="$current/deploy/oracle/run_isolated_ai_evaluation.sh"
[[ -x "$evaluator" ]] || fail "isolated evaluator is unavailable"

mkdir -p "$DEPLOY_ROOT/evaluation-logs"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log="$DEPLOY_ROOT/evaluation-logs/$stamp-$target.log"
lock="$DEPLOY_ROOT/background-evaluation.lock"

if ! flock -n "$lock" true; then
  echo "AI_EVALUATION_BACKGROUND=ALREADY_RUNNING"
  exit 0
fi

# Close every inherited SSH descriptor. The model comparison continues on
# Oracle without delaying dashboard access or depending on the laptop.
nohup flock -n "$lock" "$evaluator" "$target" \
  >"$log" 2>&1 </dev/null &
pid=$!
sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  wait "$pid" || rc=$?
  echo "EVALUATION_LAUNCH_BLOCKED: evaluator exited immediately rc=${rc:-0}" >&2
  tail -20 "$log" >&2 || true
  exit 1
fi

echo "AI_EVALUATION_BACKGROUND=STARTED"
echo "AI_EVALUATION_PID=$pid"
echo "AI_EVALUATION_LOG=$log"
echo "DASHBOARD_NOT_BLOCKED=true"
echo "MODEL_PROMOTION_PERFORMED=false"
echo "AUTOMATED_TRADING=DISABLED"
