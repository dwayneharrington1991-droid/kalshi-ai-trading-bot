#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

target=${1:?exact repair commit required}
expected_current=${2:?expected current commit required}
repo=/home/opc/kalshi-ai-trading-bot
config=/etc/kalshi-signals/deployment.env

block() { echo "LIVE_REPAIR_BLOCKED: $*" >&2; exit 1; }
[[ $(id -un) == opc ]] || block "must run as opc"
[[ -r "$config" ]] || block "deployment configuration is unreadable"
# shellcheck source=/dev/null
source "$config"
: "${DEPLOY_ROOT:?}" "${SERVICE_NAME:?}" "${DATA_ROOT:?}"
[[ "$SERVICE_NAME" == kalshi-signals.service ]] || block "unexpected service"
[[ -L "$DEPLOY_ROOT/current" ]] || block "versioned current pointer missing"

old_release=$(readlink -f "$DEPLOY_ROOT/current")
[[ -f "$old_release/.deployed-commit" ]] || block "current release identity missing"
[[ $(cat "$old_release/.deployed-commit") == "$expected_current" ]] ||
  block "running release is not expected commit $expected_current"
[[ -x "$old_release/.venv/bin/python3" ]] || block "current Python environment missing"

exec 8>"$DEPLOY_ROOT/learning.lock"
flock -n 8 || block "AI evaluation or learning refresh is active; no service change made"

git -C "$repo" cat-file -e "$target^{commit}" || block "repair commit unavailable"
release="$DEPLOY_ROOT/releases/$target"
staging="$DEPLOY_ROOT/releases/.live-repair-$target-$$"
rm_staging() { rm -rf "$staging"; }
trap rm_staging EXIT

if [[ ! -d "$release" ]]; then
  mkdir -p "$staging"
  git -C "$repo" archive "$target" | tar -x -C "$staging"
  # This lightweight path is allowed only when dependencies are identical.
  cmp -s "$staging/pyproject.toml" "$old_release/pyproject.toml" ||
    block "dependency definition changed; guarded full deployment required"
  ln -s "$old_release/.venv" "$staging/.venv"
  printf '%s\n' "$target" > "$staging/.deployed-commit"
  "$old_release/.venv/bin/python3" -m py_compile \
    "$staging/src/signals_display/monitor.py" \
    "$staging/src/signals_display/server.py" \
    "$staging/src/signals_display/prediction_engine.py"
  mv "$staging" "$release"
fi

rollback() {
  trap - ERR
  ln -s "$old_release" "$DEPLOY_ROOT/current.rollback"
  mv -Tf "$DEPLOY_ROOT/current.rollback" "$DEPLOY_ROOT/current"
  sudo -n systemctl restart "$SERVICE_NAME" || true
  echo "LIVE_REPAIR_ROLLED_BACK commit=$expected_current failed_target=$target" >&2
}
trap rollback ERR

ln -s "$release" "$DEPLOY_ROOT/current.next"
mv -Tf "$DEPLOY_ROOT/current.next" "$DEPLOY_ROOT/current"
sudo -n systemctl restart "$SERVICE_NAME"

healthy=0
for attempt in $(seq 1 90); do
  if "$release/.venv/bin/python3" "$release/deploy/oracle/health_check.py" \
      --commit "$target" --data-root "$DATA_ROOT" --require-models 0 \
      >/tmp/kalshi-live-repair-health.json 2>/dev/null; then
    healthy=1
    break
  fi
  sleep 2
done
if (( healthy != 1 )); then
  echo "LIVE_REPAIR_FAILED: dashboard health verification timed out" >&2
  false
fi

fresh=0
for attempt in $(seq 1 90); do
  if "$release/.venv/bin/python3" - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/state?view=main", timeout=5) as response:
    state = json.load(response)
preload = (state.get("feed_health") or {}).get("historical_preload") or {}
engine = state.get("prediction_engine") or {}
workers = ((state.get("feed_health") or {}).get("workers") or {}).get("healthy")
reason = str(engine.get("reason") or "")
if not (
    preload.get("status") == "READY"
    and preload.get("missing_bars") == 0
    and "Candle receipt stale/future-dated" not in reason
    and workers is True
):
    raise SystemExit(1)
print(json.dumps({
    "ticker": (state.get("market") or {}).get("ticker"),
    "historical_preload": preload,
    "prediction_status": engine.get("status"),
    "prediction_reason": engine.get("reason"),
    "signals_only": state.get("signals_only"),
}, sort_keys=True))
PY
  then
    fresh=1
    break
  fi
  sleep 2
done
if (( fresh != 1 )); then
  echo "LIVE_REPAIR_FAILED: fresh candle/prediction verification timed out" >&2
  false
fi

trap - ERR
echo "LIVE_REPAIR_VERIFIED commit=$target"
echo "AUTOMATED_TRADING=DISABLED"
