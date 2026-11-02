#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly DEFAULT_TARGET_COMMIT="7fb41dd323a98571b6d93537386be97987013441"
readonly TARGET_COMMIT="${1:-$DEFAULT_TARGET_COMMIT}"
readonly CONFIG="/etc/kalshi-signals/deployment.env"
readonly LOCK_FILE="/home/opc/.kalshi-deploy/first-release.lock"

blocker() {
  printf 'DEPLOYMENT_BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ $(id -un) == opc ]] || blocker "run this script as opc"
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || blocker "target commit must be a full lowercase SHA"
[[ -r "$CONFIG" ]] || blocker "opc cannot read $CONFIG"
# shellcheck disable=SC1090
source "$CONFIG"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${BACKUP_ROOT:?}" \
  "${SERVICE_NAME:?}" "${PYTHON_BIN:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}"

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || blocker "another deployment owns $LOCK_FILE"

# Return success without another backup when the requested release is already healthy.
if [[ -f "$DEPLOY_ROOT/deployed-commit" ]] && \
   [[ $(cat "$DEPLOY_ROOT/deployed-commit") == "$TARGET_COMMIT" ]] && \
   sudo -n systemctl is-active --quiet "$SERVICE_NAME"; then
  current=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)
  if [[ -n "$current" && -x "$current/.venv/bin/python3" ]] && \
     "$current/.venv/bin/python3" "$current/deploy/oracle/health_check.py" \
       --commit "$TARGET_COMMIT" --data-root "$DATA_ROOT" \
       --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"; then
    echo "deployment=ALREADY_HEALTHY commit=$TARGET_COMMIT backup_skipped=true"
    exit 0
  fi
fi

# Fail before remote_deploy creates a backup directory or opens a database.
[[ -f "$DEPLOY_ROOT/production-approved" ]] || \
  blocker "missing reviewed approval marker: $DEPLOY_ROOT/production-approved"
baseline=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)
[[ -n "$baseline" && -d "$baseline" && -f "$baseline/.deployed-commit" && \
   -x "$baseline/.venv/bin/python3" ]] || \
  blocker "prepared rollback baseline is missing or incomplete; it will not be activated"
[[ -r "$DEPLOY_ROOT/account-monitoring.flags" ]] || \
  blocker "prepared baseline did not capture the existing read-only account mode"
[[ -d "$BACKUP_ROOT" && -w "$BACKUP_ROOT" ]] || \
  blocker "backup root is missing or not writable: $BACKUP_ROOT"
for required in "$JOURNAL_PATH" "$ACCOUNT_STORE_PATH"; do
  [[ -f "$required" && -r "$required" ]] || blocker "required database is missing or unreadable: $required"
done

# Do not compete with a previous activation or SQLite online backup.
if pgrep -u opc -f '[s]qlite_backup.py|[a]ctivate_baseline.sh|[r]emote_deploy.sh' >/dev/null; then
  blocker "a deployment or database backup is still running; wait for that process to finish"
fi

service_state=$(sudo -n systemctl show "$SERVICE_NAME" \
  --property=ExecStart,WorkingDirectory,MainPID --no-pager) || \
  blocker "cannot inspect $SERVICE_NAME with the reviewed sudo policy"
versioned_active=0
if [[ "$service_state" == *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]]; then
  versioned_active=1
fi
if sudo -n systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "original_service=ACTIVE will_quiesce_for_consistent_backup=true"
else
  echo "original_service=INACTIVE prepared_baseline_remains_rollback_material=true"
fi

git -C "$APP_ROOT" cat-file -e "$TARGET_COMMIT^{commit}" 2>/dev/null || \
  blocker "target commit is unavailable in $APP_ROOT; fetch the repair branch first"
incoming="$DEPLOY_ROOT/incoming"
mkdir -p "$incoming"
archive="$incoming/$TARGET_COMMIT.tar.gz"
temporary="$archive.tmp.$$"
tool_root=$(mktemp -d "$DEPLOY_ROOT/.first-release-tools.XXXXXX")
trap 'rm -f "$temporary"; rm -rf "$tool_root"' EXIT
# Use the reviewed deployment tools from the target Git object, never a locally
# modified Oracle working-tree copy.
git -C "$APP_ROOT" archive "$TARGET_COMMIT" deploy/oracle | tar -x -C "$tool_root"
tools="$tool_root/deploy/oracle"
for file in remote_deploy.sh preflight.sh health_check.py sqlite_backup.py \
  run_dashboard.sh kalshi-signals.override.conf.example release-policy.json; do
  [[ -r "$tools/$file" ]] || blocker "deployment tool missing from target commit: $file"
done
"$tools/preflight.sh" "$CONFIG" || blocker "preflight failed; no backup or service change was attempted"

git -C "$APP_ROOT" archive --format=tar.gz --output="$temporary" "$TARGET_COMMIT"
[[ -s "$temporary" ]] || blocker "git archive is empty"
mv "$temporary" "$archive"
archive_sha=$(sha256sum "$archive" | awk '{print $1}')
echo "release_packaged commit=$TARGET_COMMIT sha256=$archive_sha"
echo "first_release_start commit=$TARGET_COMMIT baseline_activated=false"

deploy_mode=()
(( versioned_active == 0 )) && deploy_mode+=(--first)
"$tools/remote_deploy.sh" "$TARGET_COMMIT" "$archive" "$archive_sha" "$CONFIG" "${deploy_mode[@]}"

current=$(readlink -f "$DEPLOY_ROOT/current")
[[ -f "$current/.deployed-commit" && $(cat "$current/.deployed-commit") == "$TARGET_COMMIT" ]] || \
  blocker "current release identity does not match $TARGET_COMMIT"
pid=$(sudo -n systemctl show "$SERVICE_NAME" --property=MainPID --value)
[[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] || blocker "service has no readable live process"
cwd=$(readlink -f "/proc/$pid/cwd")
command=$(tr '\0' ' ' < "/proc/$pid/cmdline")
[[ "$cwd" == "$current" \
   && ( "$command" == *"$current/.venv/bin/python3"* \
        || "$command" == *"$DEPLOY_ROOT/current/.venv/bin/python3"* ) && \
   "$command" == *"src.signals_display.server"* && "$command" == *"--mode public"* ]] || \
  blocker "running process is not the expected signals-only release"
"$current/.venv/bin/python3" "$current/deploy/oracle/health_check.py" \
  --commit "$TARGET_COMMIT" --data-root "$DATA_ROOT" \
  --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8765/healthz
printf '\ndeployment=VERIFIED commit=%s pid=%s cwd=%s signals_only=true\n' \
  "$TARGET_COMMIT" "$pid" "$cwd"
