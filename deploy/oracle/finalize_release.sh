#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

target=${1:?exact release commit required}
repo=/home/opc/kalshi-ai-trading-bot
config=/etc/kalshi-signals/deployment.env

fail() { echo "FINALIZE_BLOCKED: $*" >&2; exit 1; }

[[ $(id -un) == opc ]] || fail "must run as opc"
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || fail "target must be a full commit SHA"
[[ -r "$config" ]] || fail "deployment configuration is unreadable"
# shellcheck source=/dev/null
source "$config"
: "${DEPLOY_ROOT:?}" "${SERVICE_NAME:?}" "${JOURNAL_PATH:?}"

git -C "$repo" fetch --no-tags origin codex/oracle-opening-hold-repair
[[ $(git -C "$repo" rev-parse FETCH_HEAD) == "$target" ]] ||
  fail "fetched branch does not match approved target"
[[ -L "$DEPLOY_ROOT/current" ]] || fail "current release pointer is missing"
old=$(readlink -f "$DEPLOY_ROOT/current")
[[ -f "$old/.deployed-commit" ]] || fail "rollback identity is missing"
rollback=$(cat "$old/.deployed-commit")
[[ "$rollback" =~ ^[0-9a-f]{40}$ ]] || fail "rollback commit is invalid"
[[ -f "$JOURNAL_PATH" ]] || fail "historical journal is missing"

mkdir -p /home/opc/.kalshi-deploy
apply=/home/opc/.kalshi-deploy/apply-$target.sh
trap 'rm -f "$apply"' EXIT
git -C "$repo" show "$target:deploy/oracle/apply_live_pipeline_repair.sh" >"$apply"
chmod 0700 "$apply"
bash -n "$apply"
bash "$apply" "$target" "$rollback" || fail "deployment failed or rolled back"

rollback_final_verification() {
  local reason=$1
  echo "FINALIZE_FAILED: $reason; restoring $rollback" >&2
  if bash "$apply" "$rollback" "$target"; then
    echo "FINALIZE_ROLLED_BACK commit=$rollback failed_target=$target" >&2
  else
    echo "FINALIZE_ROLLBACK_FAILED target=$rollback; inspect $SERVICE_NAME immediately" >&2
  fi
  exit 1
}

current=$(readlink -f "$DEPLOY_ROOT/current")
[[ $(cat "$current/.deployed-commit") == "$target" ]] ||
  rollback_final_verification "production commit does not match target"
sudo -n systemctl is-active --quiet "$SERVICE_NAME" ||
  rollback_final_verification "service is not active"

health=$(curl -fsS --max-time 10 http://127.0.0.1:8765/healthz) ||
  rollback_final_verification "health endpoint failed"
# /readyz intentionally returns HTTP 503 during an upstream feed outage. Read
# and validate its JSON without treating that external condition as a release
# regression; apply_live_pipeline_repair.sh already compares pre/post readiness.
ready=$(curl -sS --max-time 10 http://127.0.0.1:8765/readyz) ||
  rollback_final_verification "readiness endpoint was unreachable"
chart='{}'
for _attempt in $(seq 1 30); do
  chart=$(curl -fsS --max-time 8 \
    'http://127.0.0.1:8765/api/signal-chart?interval=1m&limit=180') || chart='{}'
  if "$current/.venv/bin/python3" - "$chart" <<'PY'
import json, sys
try:
    value = json.loads(sys.argv[1])
    raise SystemExit(0 if value.get("status") == "READY" and value.get("bars") else 1)
except Exception:
    raise SystemExit(1)
PY
  then break; fi
  sleep 2
done

if ! "$current/.venv/bin/python3" - "$target" "$health" "$ready" "$chart" <<'PY'
import json, sys
target = sys.argv[1]
health, ready, chart = map(json.loads, sys.argv[2:])
assert health.get("status") == "HEALTHY", health
assert health.get("build_commit") == target, health
assert health.get("signals_only") is True, health
assert (health.get("workers") or {}).get("healthy") is True, health
assert ready.get("build_commit") == target, ready
assert ready.get("signals_only") is True, ready
assert chart.get("status") == "READY" and chart.get("bars"), chart
assert chart.get("non_repainting") is True, chart
print("DEPLOYMENT=SUCCESS")
print("PRODUCTION_COMMIT=" + target)
print("DASHBOARD=HEALTHY")
print("READINESS=" + str(ready.get("status")))
print("CONTRACT=" + str(ready.get("contract")))
print("CHART=READY")
print("CHART_BARS=" + str(len(chart["bars"])))
print("CHART_SOURCE=" + str(chart.get("source")))
print("AUTOMATED_TRADING=DISABLED")
PY
then
  rollback_final_verification "final dashboard or chart verification failed"
fi

# A failed background evaluation must not roll back a healthy dashboard.
"$current/deploy/oracle/start_isolated_evaluation.sh" "$target"

mkdir -p "$DEPLOY_ROOT/live-observations"
observation="$DEPLOY_ROOT/live-observations/$(date -u +%Y%m%dT%H%M%SZ)-$target.json"
nohup flock -n "$DEPLOY_ROOT/live-observation.lock" \
  "$current/.venv/bin/python3" "$current/deploy/oracle/observe_live_rollover.py" \
  --target "$target" --duration 1200 --interval 5 --output "$observation" \
  >"$observation.log" 2>&1 </dev/null &
echo "LIVE_ROLLOVER_OBSERVER=STARTED"
echo "LIVE_ROLLOVER_RESULT=$observation"
echo "DASHBOARD_AVAILABLE=true"
echo "AUTOMATED_TRADING=DISABLED"
