#!/usr/bin/env bash
set -euo pipefail
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
  "${SERVICE_NAME:?}" "${PYTHON_BIN:?}"
[[ -f "$DEPLOY_ROOT/production-approved" ]] || {
  echo "first-deployment approval marker missing; run preflight/setup first" >&2; exit 1;
}
actual_sha=$(sha256sum "$archive" | awk '{print $1}')
[[ "$actual_sha" == "$expected_sha" ]] || { echo "release checksum mismatch" >&2; exit 1; }
release="$DEPLOY_ROOT/releases/$commit"
mkdir -p "$DEPLOY_ROOT/releases" "$DEPLOY_ROOT/incoming" "$BACKUP_ROOT"
if [[ ! -d "$release" ]]; then
  mkdir "$release"
  tar -xzf "$archive" -C "$release"
fi
policy=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["database_change"])' \
  "$release/deploy/oracle/release-policy.json")
rollback=$("$PYTHON_BIN" -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["automatic_code_rollback"]).lower())' \
  "$release/deploy/oracle/release-policy.json")
[[ "$policy" == none || "$policy" == backward-compatible ]] || {
  echo "database policy requires explicit migration review" >&2; exit 1;
}
"$PYTHON_BIN" -m venv "$release/.venv"
"$release/.venv/bin/python3" -m pip install --disable-pip-version-check "$release"
printf '%s\n' "$commit" > "$release/.deployed-commit"

stamp=$(date -u +%Y%m%dT%H%M%SZ)-$commit
backup="$BACKUP_ROOT/$stamp"
mkdir "$backup"
bytes=0
for db in "$DATA_ROOT"/*.db; do [[ -f "$db" ]] && bytes=$((bytes + $(stat -c %s "$db"))); done
free=$(df --output=avail -B1 "$BACKUP_ROOT" | tail -1)
(( free > bytes + bytes / 5 )) || { echo "insufficient backup space" >&2; exit 1; }
for db in "$DATA_ROOT"/*.db; do
  [[ -f "$db" ]] || continue
  "$release/.venv/bin/python3" "$release/deploy/oracle/sqlite_backup.py" \
    "$db" "$backup/$(basename "$db")"
done
cp "$release/deploy/oracle/release-policy.json" "$backup/"

previous=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)
ln -s "$release" "$DEPLOY_ROOT/current.next"
mv -Tf "$DEPLOY_ROOT/current.next" "$DEPLOY_ROOT/current"
if ! sudo -n systemctl restart "$SERVICE_NAME"; then failed=1; else failed=0; fi
for _ in $(seq 1 30); do
  if sudo -n systemctl is-active --quiet "$SERVICE_NAME" && \
     "$release/.venv/bin/python3" "$release/deploy/oracle/health_check.py" \
       --commit "$commit" --data-root "$DATA_ROOT" \
       --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"; then
    printf '%s\n' "$commit" > "$DEPLOY_ROOT/deployed-commit"
    echo "deployment=HEALTHY commit=$commit backup=$backup"
    exit 0
  fi
  sleep 3
done
failed=1
if [[ "$failed" == 1 && "$rollback" == true && "$policy" == none && -n "$previous" && -d "$previous" ]]; then
  ln -s "$previous" "$DEPLOY_ROOT/current.rollback"
  mv -Tf "$DEPLOY_ROOT/current.rollback" "$DEPLOY_ROOT/current"
  sudo -n systemctl restart "$SERVICE_NAME"
  previous_commit=$(cat "$previous/.deployed-commit")
  "$previous/.venv/bin/python3" "$previous/deploy/oracle/health_check.py" \
    --commit "$previous_commit" --data-root "$DATA_ROOT" \
    --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"
  echo "deployment=ROLLED_BACK failed_commit=$commit restored_commit=$previous_commit" >&2
  exit 1
fi
echo "deployment failed; automatic rollback withheld by database policy or missing baseline" >&2
exit 1

