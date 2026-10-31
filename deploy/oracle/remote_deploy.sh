#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
commit=${1:?commit required}
archive=${2:?archive required}
expected_sha=${3:?archive sha required}
config=${4:-/etc/kalshi-signals/deployment.env}
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
actual_sha=$(sha256sum "$archive" | awk '{print $1}')
[[ "$actual_sha" == "$expected_sha" ]] || { echo "release checksum mismatch" >&2; exit 1; }
mkdir -p "$DEPLOY_ROOT/releases" "$DEPLOY_ROOT/incoming" "$BACKUP_ROOT"
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
  "$PYTHON_BIN" "$backup_tool" "$db" "$destination"
  (cd "$(dirname "$destination")" && sha256sum -c "$(basename "$destination").sha256")
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

ln -s "$release" "$DEPLOY_ROOT/current.next"
mv -Tf "$DEPLOY_ROOT/current.next" "$DEPLOY_ROOT/current"
if sudo -n systemctl restart "$SERVICE_NAME"; then
  for _ in $(seq 1 30); do
    if sudo -n systemctl is-active --quiet "$SERVICE_NAME" && \
       "$release/.venv/bin/python3" "$release/deploy/oracle/health_check.py" \
         --commit "$commit" --data-root "$DATA_ROOT" --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"; then
      pid=$(sudo -n systemctl show "$SERVICE_NAME" --property=MainPID --value)
      cwd=$(readlink -f "/proc/$pid/cwd")
      command=$(tr '\0' ' ' < "/proc/$pid/cmdline")
      [[ "$cwd" == "$release" && "$command" == *"$release/.venv/bin/python3"* ]] || break
      printf '%s\n' "$commit" > "$DEPLOY_ROOT/deployed-commit"
      echo "deployment=HEALTHY commit=$commit backup=$backup"
      exit 0
    fi
    sleep 3
  done
fi
if [[ "$rollback" == true && "$policy" == none ]]; then
  ln -s "$previous" "$DEPLOY_ROOT/current.rollback"
  mv -Tf "$DEPLOY_ROOT/current.rollback" "$DEPLOY_ROOT/current"
  sudo -n systemctl restart "$SERVICE_NAME"
  previous_commit=$(cat "$previous/.deployed-commit")
  "$previous/.venv/bin/python3" "$previous/deploy/oracle/health_check.py" \
    --commit "$previous_commit" --data-root "$DATA_ROOT" --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"
  echo "deployment=ROLLED_BACK failed_commit=$commit restored_commit=$previous_commit" >&2
  exit 1
fi
echo "deployment failed; automatic rollback withheld by release policy" >&2
exit 1
