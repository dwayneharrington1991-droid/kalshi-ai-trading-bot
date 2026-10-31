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
  "$PYTHON_BIN" "$tools/sqlite_backup.py" "$db" "$destination"
  (cd "$(dirname "$destination")" && sha256sum -c "$(basename "$destination").sha256")
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
dropin="$dropin_dir/99-codex-versioned-release.conf"
existed=0
if sudo test -f "$dropin"; then existed=1; sudo cat "$dropin" > "$backup/previous-dropin.conf"; fi
rollback() {
  code=$?; trap - ERR; set +e
  if (( existed == 1 )); then sudo install -m 0644 "$backup/previous-dropin.conf" "$dropin"; else sudo rm -f "$dropin"; fi
  sudo systemctl daemon-reload; sudo systemctl restart "$SERVICE_NAME"
  echo "baseline activation rolled back; databases were not changed" >&2
  exit "$code"
}
trap rollback ERR
sudo install -d -m 0755 "$dropin_dir"
sudo install -m 0644 "$tools/kalshi-signals.override.conf.example" "$dropin"
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE_NAME"
for _ in $(seq 1 30); do
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
