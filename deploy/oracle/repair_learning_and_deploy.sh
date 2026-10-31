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

# Restore scheduling only after the repaired dashboard and exact commit passed
# the launcher's process and /healthz verification.
sudo -n systemctl start "$timer"
sudo -n systemctl is-active --quiet "$timer" || {
  echo "DEPLOYMENT_BLOCKED: dashboard is healthy but $timer could not be restored" >&2; exit 1;
}
echo "learning_timer=ACTIVE dashboard_commit=$target deployment=VERIFIED"
