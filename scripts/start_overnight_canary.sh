#!/usr/bin/env bash
set -euo pipefail

APPROVAL="${1:-}"
REQUESTED_ENVIRONMENT="${2:-production}"
if [[ "$APPROVAL" != "I_APPROVE_OVERNIGHT_CANARY" ]]; then
  echo "BLOCKED: exact overnight-canary approval is required" >&2
  exit 2
fi
if [[ "$REQUESTED_ENVIRONMENT" != "production" ]]; then
  echo "BLOCKED: overnight canary is production-only" >&2
  exit 2
fi

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON="${CANARY_PYTHON:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo "BLOCKED: project Python is unavailable" >&2; exit 2; }

SAFETY_COMMIT="91fe26c75dc24e7aaab4c729cf4deb7312686a9c"
git merge-base --is-ancestor "$SAFETY_COMMIT" HEAD 2>/dev/null || {
  echo "BLOCKED: current branch does not contain the reconciliation safety repairs" >&2
  exit 2
}

export KALSHI_ENVIRONMENT="production"
export LIVE_TRADING_ENABLED="true"
export AUTHORITATIVE_LIVE_EXECUTION_ENABLED="true"
export ORDER_RECONCILIATION_ENABLED="true"
export RECONCILIATION_SHADOW_MODE="true"
export RECONCILIATION_STARTUP_REQUIRED="true"
export LIVE_ORDER_SUBMISSION_KILL_SWITCH="false"
export PRODUCTION_EXECUTION_ACKNOWLEDGEMENT="I_ACKNOWLEDGE_PRODUCTION_ORDER_RISK"
export OVERNIGHT_CANARY_ENABLED="true"
export OVERNIGHT_CANARY_MAX_TOTAL_RISK="20"
export OVERNIGHT_CANARY_MAX_MARKET_RISK="5"
export OVERNIGHT_CANARY_MAX_POSITIONS="5"
export OVERNIGHT_CANARY_MAX_REJECTIONS="3"
export OVERNIGHT_CANARY_MAX_DAILY_LOSS="5"
export OVERNIGHT_CANARY_MARKET_DATA_MAX_AGE_SECONDS="120"
export ALLOW_RISK_REDUCING_LIVE_EXITS="true"
export ALLOW_LIVE_ORDER_CANCELLATIONS="false"
export LEVERAGE_ENABLED="false"
export MARTINGALE_ENABLED="false"
export AUTO_POSITION_SIZE_INCREASE_ENABLED="false"
export DB_PATH="${DB_PATH:-trading_system.db}"

[[ "$OVERNIGHT_CANARY_MAX_TOTAL_RISK" == "20" \
   && "$OVERNIGHT_CANARY_MAX_MARKET_RISK" == "5" \
   && "$OVERNIGHT_CANARY_MAX_POSITIONS" == "5" \
   && "$OVERNIGHT_CANARY_MAX_REJECTIONS" == "3" \
   && "$OVERNIGHT_CANARY_MAX_DAILY_LOSS" == "5" \
   && "$LIVE_ORDER_SUBMISSION_KILL_SWITCH" == "false" \
   && "$PRODUCTION_EXECUTION_ACKNOWLEDGEMENT" == "I_ACKNOWLEDGE_PRODUCTION_ORDER_RISK" ]] || {
  echo "BLOCKED: canary gates or risk limits are inconsistent" >&2
  exit 2
}

"$PYTHON" - <<'PY' || exit 2
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
api = bool(os.getenv("KALSHI_API_KEY", "").strip())
key = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
if not api or not key or not Path(key).is_file():
    raise SystemExit("BLOCKED: credentials are incomplete")
print("Credentials: PRESENT", file=__import__("sys").stderr)
PY

VALIDATION_OUT="$(mktemp)"
VALIDATION_ERR="$(mktemp)"
cleanup() { rm -f -- "$VALIDATION_OUT" "$VALIDATION_ERR"; }
trap cleanup EXIT INT TERM
export READ_ONLY_ACCOUNT_VALIDATION="true"
export AUTHORITATIVE_LIVE_EXECUTION_ENABLED="false"
export LIVE_ORDER_SUBMISSION_KILL_SWITCH="true"
export LIVE_TRADING_ENABLED="false"
if ! "$PYTHON" scripts/read_only_validate.py >"$VALIDATION_OUT" 2>"$VALIDATION_ERR"; then
  echo "BLOCKED: read-only authentication or reconciliation validation failed" >&2
  exit 2
fi
grep -Eq '^BALANCE=-?[0-9]+([.][0-9]+)?$' "$VALIDATION_OUT" || {
  echo "BLOCKED: exchange balance could not be verified" >&2
  exit 2
}
grep -q '^RECONCILIATION_HEALTH=HEALTHY$' "$VALIDATION_OUT" || {
  echo "BLOCKED: read-only reconciliation is unhealthy" >&2
  exit 2
}
grep -q '^CRITICAL_ALERTS=0$' "$VALIDATION_OUT" || {
  echo "BLOCKED: read-only reconciliation has critical alerts" >&2
  exit 2
}

# Refresh after account validation so the strict 30-second checkpoint gate
# measures current state rather than time spent scanning the account.
if ! "$PYTHON" scripts/run_persistent_read_only_reconciliation.py \
    >"$VALIDATION_OUT" 2>"$VALIDATION_ERR"; then
  echo "BLOCKED: persistent read-only reconciliation refresh failed" >&2
  exit 2
fi
grep -q '^PERSISTENT_RECONCILIATION=COMPLETE$' "$VALIDATION_OUT" || {
  echo "BLOCKED: persistent read-only reconciliation did not complete" >&2
  exit 2
}

"$PYTHON" - <<'PY' || exit 2
import os, sqlite3
from datetime import datetime, timezone
path = os.environ["DB_PATH"]
try:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        critical = db.execute("""
            SELECT COUNT(*) FROM reconciliation_alerts
            WHERE resolved_at IS NULL AND severity='critical'
        """).fetchone()[0]
        run = db.execute("""
            SELECT status, completed_at FROM reconciliation_runs
            WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1
        """).fetchone()
except sqlite3.Error:
    raise SystemExit("BLOCKED: persistent reconciliation state is unavailable")
if critical:
    raise SystemExit("BLOCKED: persistent ledger has critical reconciliation alerts")
if not run or run[0] not in {"completed", "completed_with_mismatches"}:
    raise SystemExit("BLOCKED: latest persistent reconciliation failed")
completed = datetime.fromisoformat(str(run[1]).replace("Z", "+00:00"))
if completed.tzinfo is None:
    completed = completed.replace(tzinfo=timezone.utc)
if (datetime.now(timezone.utc) - completed).total_seconds() > 30:
    raise SystemExit("BLOCKED: persistent reconciliation is stale")
print("Persistent reconciliation: HEALTHY", file=__import__("sys").stderr)
PY

# Re-enable the intentionally acknowledged live gates only after every preflight passes.
export AUTHORITATIVE_LIVE_EXECUTION_ENABLED="true"
export LIVE_ORDER_SUBMISSION_KILL_SWITCH="false"
export LIVE_TRADING_ENABLED="true"

LOG_DIR="$REPO_ROOT/logs"
mkdir -p -- "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_PATH="$LOG_DIR/overnight-canary-$STAMP.log"
ERROR_PATH="$LOG_DIR/overnight-canary-$STAMP.error.log"
PID_FILE="${KALSHI_CANARY_PID_FILE:-${TMPDIR:-/tmp}/kalshi-overnight-canary.pid}"
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
  if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "BLOCKED: an overnight canary is already running" >&2
    exit 2
  fi
  rm -f -- "$PID_FILE"
fi
(
  cd "$REPO_ROOT"
  exec -a kalshi-overnight-canary "$PYTHON" cli.py run --live
) >"$LOG_PATH" 2>"$ERROR_PATH" &
CANARY_PID=$!
printf '%s\n' "$CANARY_PID" >"$PID_FILE"
echo "Overnight canary started. PID=$CANARY_PID" >&2
echo "Log=$LOG_PATH" >&2
echo "Stop with: ./scripts/stop_overnight_canary.sh" >&2
