#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
config=${1:-/etc/kalshi-signals/deployment.env}
[[ -r "$config" ]] || { echo "missing deployment config: $config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${BACKUP_ROOT:?}" \
  "${SERVICE_NAME:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}"
[[ "$JOURNAL_PATH" == "$DATA_ROOT"/* && "$ACCOUNT_STORE_PATH" == "$DATA_ROOT"/* ]] || {
  echo "configured databases must be under DATA_ROOT" >&2; exit 1;
}
for required in "$JOURNAL_PATH" "$ACCOUNT_STORE_PATH"; do
  [[ -f "$required" && -r "$required" ]] || {
    echo "required database missing or unreadable: $required" >&2; exit 1;
  }
done
[[ -d "$BACKUP_ROOT" && -w "$BACKUP_ROOT" ]] || {
  echo "backup root must already exist and be writable: $BACKUP_ROOT" >&2; exit 1;
}
data_device=$(stat -c %d "$DATA_ROOT")
backup_device=$(stat -c %d "$BACKUP_ROOT")
if [[ "$data_device" == "$backup_device" ]]; then
  [[ ${ALLOW_SAME_DISK_BACKUP:-0} == 1 ]] || {
    echo "data and backup paths share a disk; explicit ALLOW_SAME_DISK_BACKUP=1 is required" >&2
    exit 1
  }
  echo "WARNING: SAME_DISK_BACKUP_APPROVED_TEMPORARILY"
  echo "WARNING: /home/opc/kalshi-backups protects deployment rollback only; it does not protect against disk failure."
  echo "backup_durability=SAME_DISK_TEMPORARY"
else
  echo "backup_durability=SEPARATE_FILESYSTEM"
fi
echo "repository_commit=$(git -C "$APP_ROOT" rev-parse HEAD 2>/dev/null || echo unavailable)"
echo "repository_worktree=$(if [[ -n "$(git -C "$APP_ROOT" status --porcelain 2>/dev/null || true)" ]]; then echo DIRTY_PRESERVED; else echo CLEAN; fi)"
git -C "$APP_ROOT" status --short 2>/dev/null || true
echo "service_active=$(sudo -n systemctl is-active "$SERVICE_NAME" || true)"
service_user=$(sudo -n systemctl show "$SERVICE_NAME" --property=User --value)
[[ "$service_user" == opc ]] || {
  echo "service must run as opc to use the reviewed permissions; found: ${service_user:-root/default}" >&2
  exit 1
}
service_state=$(sudo -n systemctl show "$SERVICE_NAME" \
  --property=User,Group,FragmentPath,DropInPaths,WorkingDirectory,ExecStart,MainPID,NRestarts --no-pager)
printf '%s\n' "$service_state"
account_mode_file="$DEPLOY_ROOT/account-monitoring.flags"
if [[ -r "$account_mode_file" ]]; then
  # shellcheck disable=SC1090
  source "$account_mode_file"
  [[ ${ENABLE_ACCOUNT_READONLY:-} == 0 || ${ENABLE_ACCOUNT_READONLY:-} == 1 ]] || {
    echo "invalid baseline-captured account monitoring mode" >&2; exit 1;
  }
  if [[ "$service_state" != *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]]; then
    detected=0
    [[ "$service_state" == *"--account-readonly"* ]] && detected=1
    [[ "$detected" == "$ENABLE_ACCOUNT_READONLY" ]] || {
      echo "captured account mode does not match existing service" >&2; exit 1;
    }
  fi
  echo "account_monitoring_mode=BASELINE_PRESERVED_${ENABLE_ACCOUNT_READONLY}"
else
  echo "baseline_required=true reason=account_monitoring_mode_not_captured"
fi
if [[ -L "$DEPLOY_ROOT/current" ]]; then
  current=$(readlink -f "$DEPLOY_ROOT/current")
  [[ -f "$current/.deployed-commit" && -x "$current/.venv/bin/python3" ]] || {
    echo "current rollback baseline is incomplete" >&2; exit 1;
  }
  if [[ "$service_state" == *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]]; then
    echo "versioned_service=ACTIVE"
  else
    echo "versioned_service=BASELINE_PREPARED_ACTIVATION_REQUIRED"
  fi
else
  echo "baseline_required=true reason=no_versioned_current_release"
fi
while IFS= read -r -d '' path; do
  stat -c 'persistent_database=%n bytes=%s owner=%U:%G mode=%a' "$path"
done < <(find "$DATA_ROOT" -type f -name '*.db' -print0 | sort -z)
for path in "${MODEL_ROOT:-}" "${MODEL_ARTIFACT:-}" "${OPENING_MODEL_ARTIFACT:-}" "${MODEL_REGISTRY:-}"; do
  [[ -n "$path" && -e "$path" ]] && stat -c 'persistent_model=%n bytes=%s owner=%U:%G mode=%a' "$path" || true
done
df -h "$APP_ROOT" "$BACKUP_ROOT"
echo "preflight=PASS service_not_modified=true worktree_not_modified=true"
