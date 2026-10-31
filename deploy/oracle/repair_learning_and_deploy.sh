#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

target=${1:?full target commit required}
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || { echo "DEPLOYMENT_BLOCKED: invalid target commit" >&2; exit 1; }
app=/home/opc/kalshi-ai-trading-bot
timer=kalshi-learning.timer
learning=kalshi-learning.service
config=/etc/kalshi-signals/deployment.env
[[ $(id -un) == opc ]] || { echo "DEPLOYMENT_BLOCKED: run as opc" >&2; exit 1; }
[[ -r "$config" ]] || { echo "DEPLOYMENT_BLOCKED: unreadable $config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${PYTHON_BIN:?}"
[[ -f "$DEPLOY_ROOT/production-approved" ]] || {
  echo "DEPLOYMENT_BLOCKED: missing reviewed approval marker: $DEPLOY_ROOT/production-approved" >&2; exit 1;
}
baseline=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)
[[ -n "$baseline" && -f "$baseline/.deployed-commit" && -x "$baseline/.venv/bin/python3" ]] || {
  echo "DEPLOYMENT_BLOCKED: prepared rollback baseline is missing or incomplete" >&2; exit 1;
}
[[ -r "$DEPLOY_ROOT/account-monitoring.flags" ]] || {
  echo "DEPLOYMENT_BLOCKED: prepared baseline did not capture read-only account mode" >&2; exit 1;
}

mkdir -p /home/opc/.kalshi-deploy
exec 8>/home/opc/.kalshi-deploy/learning-repair.lock
flock -n 8 || { echo "DEPLOYMENT_BLOCKED: another learning repair is running" >&2; exit 1; }

git -C "$app" cat-file -e "$target^{commit}" 2>/dev/null || {
  echo "DEPLOYMENT_BLOCKED: target commit is not available in the production repository" >&2; exit 1;
}
sudo -n systemctl show "$timer" --property=LoadState --value | grep -qx loaded || {
  echo "DEPLOYMENT_BLOCKED: $timer is not installed; no service name was invented" >&2; exit 1;
}
sudo -n systemctl show "$learning" --property=LoadState --value | grep -qx loaded || {
  echo "DEPLOYMENT_BLOCKED: $learning is not installed; no service name was invented" >&2; exit 1;
}

echo "learning_quiesce timer=$timer service=$learning"
sudo -n systemctl stop "$timer"
sudo -n systemctl stop "$learning"
sudo -n systemctl is-active --quiet "$timer" && {
  echo "DEPLOYMENT_BLOCKED: learning timer remained active" >&2; exit 1;
}

# Stop only the explicitly identified staging refresh, owned by opc.
mapfile -t refresh_pids < <(pgrep -u opc -f '[p]ython[^ ]* .*\/home\/opc\/refresh_learning_staging\.py($| )' || true)
for pid in "${refresh_pids[@]}"; do
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  [[ "$cmd" == *"/home/opc/refresh_learning_staging.py"* ]] || continue
  echo "stopping_stuck_refresh pid=$pid"
  kill -TERM "$pid"
done
for _ in $(seq 1 15); do
  remaining=0
  for pid in "${refresh_pids[@]}"; do kill -0 "$pid" 2>/dev/null && remaining=1; done
  (( remaining == 0 )) && break
  sleep 1
done
for pid in "${refresh_pids[@]}"; do
  if kill -0 "$pid" 2>/dev/null; then
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    [[ "$cmd" == *"/home/opc/refresh_learning_staging.py"* ]] || continue
    echo "forcing_stuck_refresh_exit pid=$pid"
    kill -KILL "$pid"
  fi
done

# Validate every live database read-only before packaging or deployment backup.
echo "source_integrity_start"
timeout 600 "$PYTHON_BIN" - "$DATA_ROOT" <<'PY'
import sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
databases = sorted(root.rglob("*.db"))
if not databases:
    raise SystemExit("no SQLite databases found")
for path in databases:
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        result = db.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"database integrity failed: {path}: {result}")
    print(f"source_integrity_ok database={path} bytes={path.stat().st_size}", flush=True)
PY
echo "source_integrity_complete"

temporary=$(mktemp /home/opc/.kalshi-deploy/launch-first-release.XXXXXX)
trap 'rm -f "$temporary"' EXIT
git -C "$app" show "$target:deploy/oracle/launch_first_release.sh" > "$temporary"
chmod 0700 "$temporary"
"$temporary" "$target"

# Replace the legacy staging refresh with the bounded versioned runner. Never
# restore a timer that still points at /home/opc/refresh_learning_staging.py.
learning_dropin_dir=/etc/systemd/system/kalshi-learning.service.d
learning_dropin="$learning_dropin_dir/zz-codex-bounded-learning.conf"
runner="$DEPLOY_ROOT/current/deploy/oracle/run_learning_refresh.sh"
override="$DEPLOY_ROOT/current/deploy/oracle/kalshi-learning.override.conf.example"
[[ -x "$runner" && -r "$override" ]] || {
  echo "DEPLOYMENT_BLOCKED: repaired release lacks bounded learning service files" >&2; exit 1;
}
sudo -n install -d -m 0755 "$learning_dropin_dir"
sudo -n install -m 0644 "$override" "$learning_dropin"
sudo -n systemctl daemon-reload
learning_exec=$(sudo -n systemctl show "$learning" --property=ExecStart --value)
[[ "$learning_exec" == *"$DEPLOY_ROOT/current/deploy/oracle/run_learning_refresh.sh"* ]] || {
  echo "DEPLOYMENT_BLOCKED: bounded learning override did not win systemd precedence" >&2; exit 1;
}
echo "learning_validation_start runner=$runner"
if ! timeout --signal=TERM --kill-after=30s 1320 sudo -n systemctl start "$learning"; then
  echo "DEPLOYMENT_BLOCKED: bounded learning refresh failed or exceeded 22 minutes; timer remains stopped" >&2
  exit 1
fi
learning_result=$(sudo -n systemctl show "$learning" --property=Result --value)
learning_status=$(sudo -n systemctl show "$learning" --property=ExecMainStatus --value)
[[ "$learning_result" == success && "$learning_status" == 0 ]] || {
  echo "DEPLOYMENT_BLOCKED: bounded learning refresh result=$learning_result status=$learning_status; timer remains stopped" >&2
  exit 1
}

# Restore scheduling only after both the repaired dashboard and one bounded
# learning refresh pass validation.
sudo -n systemctl start "$timer"
sudo -n systemctl is-active --quiet "$timer" || {
  echo "DEPLOYMENT_BLOCKED: dashboard is healthy but $timer could not be restored" >&2; exit 1;
}
echo "learning_service=VERIFIED learning_timer=ACTIVE dashboard_commit=$target deployment=VERIFIED"
