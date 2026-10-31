#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
config=${1:-/etc/kalshi-signals/deployment.env}
[[ -r "$config" ]] || { echo "missing deployment config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${BACKUP_ROOT:?}" "${SERVICE_NAME:?}" \
  "${PYTHON_BIN:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}"
current=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)
[[ -n "$current" && -x "$current/.venv/bin/python3" && -f "$current/.deployed-commit" ]] || {
  echo "prepared baseline is missing" >&2; exit 1;
}
[[ -r "$DEPLOY_ROOT/account-monitoring.flags" ]] || { echo "captured account mode missing" >&2; exit 1; }
mkdir -p "$BACKUP_ROOT"
same_disk=0
if [[ $(stat -c %d "$DATA_ROOT") == $(stat -c %d "$BACKUP_ROOT") ]]; then
  [[ ${ALLOW_SAME_DISK_BACKUP:-0} == 1 ]] || { echo "same-disk backup not explicitly approved" >&2; exit 1; }
  echo "WARNING: SAME_DISK_BACKUP_APPROVED_TEMPORARILY; disk failure is not covered" >&2
  same_disk=1
fi
mapfile -d '' databases < <(find "$DATA_ROOT" -type f -name '*.db' -print0 | sort -z)
for required in "$JOURNAL_PATH" "$ACCOUNT_STORE_PATH"; do
  found=0; for db in "${databases[@]}"; do [[ "$db" == "$required" ]] && found=1; done
  (( found == 1 )) || { echo "required database not discovered: $required" >&2; exit 1; }
done
bytes=0; for db in "${databases[@]}"; do bytes=$((bytes + $(stat -c %s "$db"))); done
[[ -n ${MODEL_ROOT:-} && -e ${MODEL_ROOT:-} ]] && bytes=$((bytes + $(du -sb "$MODEL_ROOT" | awk '{print $1}'))) || true
free=$(df --output=avail -B1 "$BACKUP_ROOT" | tail -1 | tr -d ' ')
(( free > bytes + bytes / 4 + 1073741824 )) || { echo "insufficient backup space" >&2; exit 1; }
commit=$(cat "$current/.deployed-commit")
backup="$BACKUP_ROOT/baseline-activation-$(date -u +%Y%m%dT%H%M%SZ)-$commit"
mkdir "$backup" "$backup/databases"
if (( same_disk == 1 )); then
  printf '%s\n' 'SAME_DISK_BACKUP_APPROVED_TEMPORARILY' \
    'This backup does not protect against disk failure.' > "$backup/BACKUP_DURABILITY_WARNING.txt"
fi
tools=$(cd "$(dirname "$0")" && pwd)
for db in "${databases[@]}"; do
  relative=${db#"$DATA_ROOT"/}; destination="$backup/databases/$relative"
  mkdir -p "$(dirname "$destination")"
  echo "backup_start database=$relative bytes=$(stat -c %s "$db")" >&2
  "$PYTHON_BIN" "$tools/sqlite_backup.py" "$db" "$destination"
  (cd "$(dirname "$destination")" && sha256sum -c "$(basename "$destination").sha256")
  echo "backup_complete database=$relative" >&2
done
if [[ -n ${MODEL_ROOT:-} && -e ${MODEL_ROOT:-} ]]; then
  [[ "$MODEL_ROOT" == /home/opc/kalshi-ai-trading-bot/* ]] || { echo "model root outside application" >&2; exit 1; }
  tar -C /home/opc/kalshi-ai-trading-bot -czf "$backup/model-artifacts.tar.gz" \
    "${MODEL_ROOT#/home/opc/kalshi-ai-trading-bot/}"
  (cd "$backup" && sha256sum model-artifacts.tar.gz > model-artifacts.tar.gz.sha256 \
    && sha256sum -c model-artifacts.tar.gz.sha256)
fi
sudo -n systemctl cat "$SERVICE_NAME" > "$backup/systemd-before.txt"
dropin_dir="/etc/systemd/system/$SERVICE_NAME.d"
dropin="$dropin_dir/zz-codex-versioned-release.conf"
if sudo test -f "$dropin"; then
  echo "target baseline drop-in already exists; refusing to overwrite it" >&2; exit 1
fi
rollback() {
  code=$?; trap - ERR; set +e
  sudo rm -f "$dropin"
  sudo systemctl daemon-reload; sudo systemctl restart "$SERVICE_NAME"
  echo "baseline activation rolled back; databases were not changed" >&2
  exit "$code"
}
trap rollback ERR
sudo install -d -m 0755 "$dropin_dir"
sudo install -m 0644 "$tools/kalshi-signals.override.conf.example" "$dropin"
sudo systemctl daemon-reload
effective=$(sudo systemctl show "$SERVICE_NAME" --property=ExecStart --value)
[[ "$effective" == *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]] || {
  echo "versioned launcher did not win systemd drop-in precedence" >&2; false
}
sudo systemctl restart "$SERVICE_NAME"
for attempt in $(seq 1 90); do
  (( attempt == 1 || attempt % 5 == 0 )) && \
    echo "health_wait attempt=$attempt max=90 expected_commit=$commit" >&2
  if sudo systemctl is-active --quiet "$SERVICE_NAME" && \
     "$current/.venv/bin/python3" "$current/deploy/oracle/health_check.py" \
       --commit "$commit" --data-root "$DATA_ROOT" --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"; then
    trap - ERR
    echo "baseline_activation=HEALTHY backup=$backup production_approval_marker_created=false"
    exit 0
  fi
  sleep 2
done
false
