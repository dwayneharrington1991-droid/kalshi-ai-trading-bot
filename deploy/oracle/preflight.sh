#!/usr/bin/env bash
set -euo pipefail
config=${1:-/etc/kalshi-signals/deployment.env}
[[ -r "$config" ]] || { echo "missing deployment config: $config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${BACKUP_ROOT:?}" "${SERVICE_NAME:?}"
echo "repository_commit=$(git -C "$APP_ROOT" rev-parse HEAD 2>/dev/null || echo unavailable)"
if [[ -n "$(git -C "$APP_ROOT" status --porcelain 2>/dev/null || true)" ]]; then
  echo "repository_worktree=DIRTY"
else
  echo "repository_worktree=CLEAN_OR_NOT_A_REPOSITORY"
fi
echo "service_active=$(sudo -n systemctl is-active "$SERVICE_NAME" || true)"
sudo -n systemctl show "$SERVICE_NAME" \
  --property=FragmentPath,DropInPaths,WorkingDirectory,ExecStart,MainPID,NRestarts \
  --no-pager
for path in "$DATA_ROOT"/signals_monitor.db "$DATA_ROOT"/signals_account_monitor.db \
            "$DATA_ROOT"/signals_model.json "$DATA_ROOT"/signals_opening_model.json; do
  [[ -e "$path" ]] && stat -c 'persistent=%n bytes=%s owner=%U:%G mode=%a' "$path" || true
done
df -h "$APP_ROOT" "$BACKUP_ROOT"
test -d "$BACKUP_ROOT" && test -w "$BACKUP_ROOT"
echo "preflight=PASS service_not_modified=true"

