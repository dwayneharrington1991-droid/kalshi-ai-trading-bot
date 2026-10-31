#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
commit=${1:?commit required}
archive=${2:?archive required}
expected_sha=${3:?archive sha required}
config=${4:-/etc/kalshi-signals/deployment.env}
first_mode=${5:-}
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit" >&2; exit 1; }
[[ -r "$config" ]] || { echo "missing deployment config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${BACKUP_ROOT:?}" \
  "${SERVICE_NAME:?}" "${PYTHON_BIN:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}"
[[ "$JOURNAL_PATH" == "$DATA_ROOT"/* && "$ACCOUNT_STORE_PATH" == "$DATA_ROOT"/* ]] || {
  echo "configured databases must be under DATA_ROOT" >&2; exit 1;
}
for required in "$JOURNAL_PATH" "$ACCOUNT_STORE_PATH"; do
  [[ -f "$required" && -r "$required" ]] || { echo "required database missing: $required" >&2; exit 1; }
done
[[ -f "$DEPLOY_ROOT/production-approved" ]] || {
  echo "first-deployment approval marker missing; reviewed baseline setup required" >&2; exit 1;
}
previous=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)
[[ -n "$previous" && -d "$previous" && -f "$previous/.deployed-commit" \
   && -x "$previous/.venv/bin/python3" ]] || {
  echo "verified versioned rollback baseline missing; deployment refused" >&2; exit 1;
}
service_state=$(sudo -n systemctl show "$SERVICE_NAME" --property=WorkingDirectory,ExecStart --no-pager)
versioned_active=0
[[ "$service_state" == *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]] && versioned_active=1
if (( versioned_active == 0 )) && [[ "$first_mode" != --first ]]; then
  echo "versioned systemd launcher is not active; use reviewed first-deployment mode" >&2; exit 1
fi
account_mode_file="$DEPLOY_ROOT/account-monitoring.flags"
[[ -r "$account_mode_file" ]] || { echo "baseline-captured account mode missing" >&2; exit 1; }
# shellcheck disable=SC1090
source "$account_mode_file"
[[ ${ENABLE_ACCOUNT_READONLY:-} == 0 || ${ENABLE_ACCOUNT_READONLY:-} == 1 ]] || {
  echo "invalid baseline-captured account mode" >&2; exit 1;
}
actual_sha=$(sha256sum "$archive" | awk '{print $1}')
[[ "$actual_sha" == "$expected_sha" ]] || { echo "release checksum mismatch" >&2; exit 1; }
mkdir -p "$DEPLOY_ROOT/releases" "$DEPLOY_ROOT/incoming" "$BACKUP_ROOT"

dropin_dir="/etc/systemd/system/$SERVICE_NAME.d"
dropin="$dropin_dir/zz-codex-versioned-release.conf"
legacy_dropin="$dropin_dir/99-codex-versioned-release.conf"
legacy_disabled="$dropin_dir/99-codex-versioned-release.conf.disabled-by-codex"
legacy_moved=0
first_recovery_needed=0
versioned_recovery_needed=0
restore_first_on_error() {
  code=${1:-1}; trap - ERR EXIT; set +e
  ln -s "$previous" "$DEPLOY_ROOT/current.failed-first-rollback"
  mv -Tf "$DEPLOY_ROOT/current.failed-first-rollback" "$DEPLOY_ROOT/current"
  sudo rm -f "$dropin"
  if (( legacy_moved == 1 )); then sudo mv "$legacy_disabled" "$legacy_dropin"; fi
  sudo systemctl daemon-reload
  sudo systemctl restart "$SERVICE_NAME"
  echo "first deployment failed; original systemd configuration restored" >&2
  exit "$code"
}
recover_first_on_exit() {
  code=$?
  if (( first_recovery_needed == 1 && code != 0 )); then
    restore_first_on_error "$code"
  fi
}
recover_versioned_on_exit() {
  code=$?
  if (( versioned_recovery_needed == 1 && code != 0 )); then
    trap - EXIT
    set +e
    ln -s "$previous" "$DEPLOY_ROOT/current.failed-update-rollback"
    mv -Tf "$DEPLOY_ROOT/current.failed-update-rollback" "$DEPLOY_ROOT/current"
    sudo systemctl restart "$SERVICE_NAME"
    echo "versioned deployment failed; previous release restarted" >&2
    exit "$code"
  fi
}
if (( versioned_active == 0 )); then
  first_recovery_needed=1
  trap recover_first_on_exit EXIT
else
  versioned_recovery_needed=1
  trap recover_versioned_on_exit EXIT
fi
# Every deployment needs a consistent backup. Stop the managed writer, then
# terminate only orphaned copies of this exact signals server/journal/port.
echo "dashboard_quiesce_start service=$SERVICE_NAME" >&2
sudo -n systemctl stop "$SERVICE_NAME"
for pid in $(pgrep -u opc -f '[s]rc\.signals_display\.server' || true); do
  command=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  if [[ "$command" == *"--journal $JOURNAL_PATH"* && "$command" == *"--port 8765"* ]]; then
    echo "stopping_orphan_dashboard pid=$pid" >&2
    kill -TERM "$pid"
  fi
done
for _ in $(seq 1 15); do
  pgrep -u opc -f '[s]rc\.signals_display\.server.*--port 8765' >/dev/null || break
  sleep 1
done
for pid in $(pgrep -u opc -f '[s]rc\.signals_display\.server.*--port 8765' || true); do
  command=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  if [[ "$command" == *"--journal $JOURNAL_PATH"* ]]; then
    echo "forcing_orphan_dashboard_exit pid=$pid" >&2
    kill -KILL "$pid"
  fi
done
holders=$(fuser "$JOURNAL_PATH" 2>/dev/null || true)
[[ -z "${holders//[[:space:]]/}" ]] || {
  echo "database still held after managed writer shutdown; pids=$holders" >&2
  exit 1
}
echo "dashboard_quiesce_complete database_writers=0" >&2
data_device=$(stat -c %d "$DATA_ROOT")
backup_device=$(stat -c %d "$BACKUP_ROOT")
same_disk=0
if [[ "$data_device" == "$backup_device" ]]; then
  [[ ${ALLOW_SAME_DISK_BACKUP:-0} == 1 ]] || {
    echo "same-disk backup refused without explicit ALLOW_SAME_DISK_BACKUP=1" >&2; exit 1;
  }
  same_disk=1
  echo "WARNING: SAME_DISK_BACKUP_APPROVED_TEMPORARILY" >&2
  echo "WARNING: backups protect deployment rollback but not physical disk failure" >&2
fi
mapfile -d '' databases < <(find "$DATA_ROOT" -type f -name '*.db' -print0 | sort -z)
(( ${#databases[@]} > 0 )) || { echo "no databases discovered under DATA_ROOT" >&2; exit 1; }
for required in "$JOURNAL_PATH" "$ACCOUNT_STORE_PATH"; do
  found=0
  for db in "${databases[@]}"; do [[ "$db" == "$required" ]] && found=1; done
  (( found == 1 )) || { echo "required database not discovered: $required" >&2; exit 1; }
done
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$commit
backup="$BACKUP_ROOT/$stamp"
mkdir "$backup" "$backup/databases"
if (( same_disk == 1 )); then
  cat > "$backup/BACKUP_DURABILITY_WARNING.txt" <<'EOF'
SAME_DISK_BACKUP_APPROVED_TEMPORARILY
This backup can support application rollback, but it cannot protect against
filesystem corruption, volume loss, or physical disk failure. Copy verified
backup artifacts to independent storage as soon as practical.
EOF
fi
bytes=0
for db in "${databases[@]}"; do bytes=$((bytes + $(stat -c %s "$db"))); done
for model in "${MODEL_ROOT:-}" "${MODEL_ARTIFACT:-}" "${OPENING_MODEL_ARTIFACT:-}" "${MODEL_REGISTRY:-}"; do
  [[ -n "$model" && -e "$model" ]] && bytes=$((bytes + $(du -sb "$model" | awk '{print $1}'))) || true
done
free=$(df --output=avail -B1 "$BACKUP_ROOT" | tail -1 | tr -d ' ')
required_free=$((bytes + bytes / 4 + 1073741824))
(( free > required_free )) || { echo "insufficient backup space" >&2; exit 1; }

# Preserve dirty Oracle worktree material for reconciliation. Never checkout/reset/clean it.
git -C "$APP_ROOT" status --short > "$backup/oracle-worktree-status.txt" || true
git -C "$APP_ROOT" diff --binary > "$backup/oracle-worktree.patch" || true
if [[ -f "$APP_ROOT/src/signals_display/monitor.py" ]]; then
  cp -a "$APP_ROOT/src/signals_display/monitor.py" "$backup/monitor.py.oracle"
fi
backup_tool=$(cd "$(dirname "$0")" && pwd)/sqlite_backup.py
[[ -r "$backup_tool" ]] || { echo "verified SQLite backup tool missing" >&2; exit 1; }
for db in "${databases[@]}"; do
  relative=${db#"$DATA_ROOT"/}
  destination="$backup/databases/$relative"
  mkdir -p "$(dirname "$destination")"
  echo "backup_start database=$relative bytes=$(stat -c %s "$db")" >&2
  "$PYTHON_BIN" "$backup_tool" "$db" "$destination" \
    --max-seconds "${SQLITE_BACKUP_MAX_SECONDS:-900}" \
    --stall-seconds "${SQLITE_BACKUP_STALL_SECONDS:-120}" \
    --max-restarts "${SQLITE_BACKUP_MAX_RESTARTS:-3}" \
    --busy-timeout-ms "${SQLITE_BACKUP_BUSY_TIMEOUT_MS:-30000}"
  (cd "$(dirname "$destination")" && sha256sum -c "$(basename "$destination").sha256")
  echo "backup_complete database=$relative" >&2
done
for required in "$JOURNAL_PATH" "$ACCOUNT_STORE_PATH"; do
  relative=${required#"$DATA_ROOT"/}
  [[ -f "$backup/databases/$relative" && -f "$backup/databases/$relative.sha256" ]] || {
    echo "verified required backup missing: $relative" >&2; exit 1;
  }
done
model_items=()
for model in "${MODEL_ROOT:-}" "${MODEL_ARTIFACT:-}" "${OPENING_MODEL_ARTIFACT:-}" "${MODEL_REGISTRY:-}"; do
  if [[ -n "$model" && -e "$model" ]]; then
    [[ "$model" == "$APP_ROOT"/* ]] || { echo "model path outside APP_ROOT" >&2; exit 1; }
    model_items+=("${model#"$APP_ROOT"/}")
  fi
done
if (( ${#model_items[@]} > 0 )); then
  tar -C "$APP_ROOT" -czf "$backup/model-artifacts.tar.gz" "${model_items[@]}"
  (cd "$backup" && sha256sum model-artifacts.tar.gz > model-artifacts.tar.gz.sha256 \
    && sha256sum -c model-artifacts.tar.gz.sha256)
fi

# Only after every required backup is verified may release files/dependencies be created.
release="$DEPLOY_ROOT/releases/$commit"
if [[ ! -d "$release" ]]; then
  staging="$DEPLOY_ROOT/releases/.staging-$commit-$$"
  mkdir "$staging"
  tar -xzf "$archive" -C "$staging"
  printf '%s\n' "$commit" > "$staging/.deployed-commit"
  mv "$staging" "$release"
fi
[[ $(cat "$release/.deployed-commit") == "$commit" ]] || { echo "release identity mismatch" >&2; exit 1; }
policy=$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1]))["database_change"])' "$release/deploy/oracle/release-policy.json")
rollback=$($PYTHON_BIN -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["automatic_code_rollback"]).lower())' "$release/deploy/oracle/release-policy.json")
[[ "$policy" == none || "$policy" == backward-compatible ]] || { echo "database migration review required" >&2; exit 1; }
if [[ -f "$APP_ROOT/src/signals_display/monitor.py" ]]; then
  diff -u "$APP_ROOT/src/signals_display/monitor.py" "$release/src/signals_display/monitor.py" \
    > "$backup/monitor-comparison.diff" || true
fi
[[ -x "$release/.venv/bin/python3" ]] || "$PYTHON_BIN" -m venv "$release/.venv"
"$release/.venv/bin/python3" -m pip install --disable-pip-version-check "$release"
cp "$release/deploy/oracle/release-policy.json" "$backup/"

if (( versioned_active == 0 )); then
  sudo test -f "$dropin" && { echo "winning versioned drop-in already exists" >&2; exit 1; }
  if sudo test -f "$legacy_dropin"; then
    sudo test -f "$legacy_disabled" && { echo "legacy override preservation conflict" >&2; exit 1; }
    sudo mv "$legacy_dropin" "$legacy_disabled"
    legacy_moved=1
  fi
  tools=$(cd "$(dirname "$0")" && pwd)
  sudo install -d -m 0755 "$dropin_dir"
  sudo install -m 0644 "$tools/kalshi-signals.override.conf.example" "$dropin"
  sudo systemctl daemon-reload
  effective=$(sudo systemctl show "$SERVICE_NAME" --property=ExecStart --value)
  [[ "$effective" == *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]] || {
    echo "winning systemd override is ineffective" >&2; exit 1;
  }
fi

ln -s "$release" "$DEPLOY_ROOT/current.next"
mv -Tf "$DEPLOY_ROOT/current.next" "$DEPLOY_ROOT/current"
if sudo -n systemctl restart "$SERVICE_NAME"; then
  for attempt in $(seq 1 60); do
    (( attempt == 1 || attempt % 5 == 0 )) && \
      echo "deployment_health_wait attempt=$attempt max=60 expected_commit=$commit" >&2
    if sudo -n systemctl is-active --quiet "$SERVICE_NAME" && \
       "$release/.venv/bin/python3" "$release/deploy/oracle/health_check.py" \
         --commit "$commit" --data-root "$DATA_ROOT" --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"; then
      pid=$(sudo -n systemctl show "$SERVICE_NAME" --property=MainPID --value)
      cwd=$(readlink -f "/proc/$pid/cwd")
      command=$(tr '\0' ' ' < "/proc/$pid/cmdline")
      [[ "$cwd" == "$release" \
         && ( "$command" == *"$release/.venv/bin/python3"* \
              || "$command" == *"$DEPLOY_ROOT/current/.venv/bin/python3"* ) \
         && "$command" == *"src.signals_display.server"* \
         && "$command" == *"--mode public"* ]] || break
      printf '%s\n' "$commit" > "$DEPLOY_ROOT/deployed-commit"
      first_recovery_needed=0
      versioned_recovery_needed=0
      trap - ERR EXIT
      echo "deployment=HEALTHY commit=$commit backup=$backup"
      exit 0
    fi
    sleep 3
  done
fi
if [[ "$rollback" == true && "$policy" == none ]]; then
  ln -s "$previous" "$DEPLOY_ROOT/current.rollback"
  mv -Tf "$DEPLOY_ROOT/current.rollback" "$DEPLOY_ROOT/current"
  if (( versioned_active == 0 )); then
    first_recovery_needed=0
    trap - ERR EXIT
    sudo rm -f "$dropin"
    if (( legacy_moved == 1 )); then sudo mv "$legacy_disabled" "$legacy_dropin"; fi
    sudo systemctl daemon-reload
    sudo systemctl restart "$SERVICE_NAME"
    sudo systemctl is-active --quiet "$SERVICE_NAME"
    echo "deployment=ROLLED_BACK_TO_ORIGINAL_SYSTEMD failed_commit=$commit" >&2
    exit 1
  fi
  sudo -n systemctl restart "$SERVICE_NAME"
  previous_commit=$(cat "$previous/.deployed-commit")
  "$previous/.venv/bin/python3" "$previous/deploy/oracle/health_check.py" \
    --commit "$previous_commit" --data-root "$DATA_ROOT" --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"
  echo "deployment=ROLLED_BACK failed_commit=$commit restored_commit=$previous_commit" >&2
  versioned_recovery_needed=0
  trap - EXIT
  exit 1
fi
echo "deployment failed; automatic rollback withheld by release policy" >&2
exit 1
