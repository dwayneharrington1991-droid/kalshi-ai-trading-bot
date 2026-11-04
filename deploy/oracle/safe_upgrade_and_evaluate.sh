#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
# shellcheck disable=SC2154

target=${1:?target commit required}
expected_current=${2:?expected current commit required}
repo=/home/opc/kalshi-ai-trading-bot
config=/etc/kalshi-signals/deployment.env

block() {
  echo "DEPLOYMENT_BLOCKED: $*" >&2
  exit 1
}

[[ $(id -un) == opc ]] || block "must run as opc"
[[ -r "$config" ]] || block "deployment configuration is unreadable"

# shellcheck source=/dev/null
source "$config"

: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${DATA_ROOT:?}" "${BACKUP_ROOT:?}" \
  "${SERVICE_NAME:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}" \
  "${MODEL_ROOT:?}" "${MODEL_ARTIFACT:?}" "${OPENING_MODEL_ARTIFACT:?}" \
  "${MODEL_REGISTRY:?}" "${PYTHON_BIN:?}"

[[ "$APP_ROOT" == "$repo" ]] || block "unexpected APP_ROOT"
[[ "$SERVICE_NAME" == kalshi-signals.service ]] || block "unexpected service"
[[ -L "$DEPLOY_ROOT/current" ]] || block "versioned current pointer is missing"

expected_release=$(readlink -f "$DEPLOY_ROOT/current")
[[ -f "$expected_release/.deployed-commit" ]] ||
  block "current release identity is missing"
[[ $(cat "$expected_release/.deployed-commit") == "$expected_current" ]] ||
  block "current release is not $expected_current"

sudo -n systemctl is-active --quiet "$SERVICE_NAME" ||
  block "current service is inactive"

service_state=$(sudo -n systemctl show "$SERVICE_NAME" \
  --property=ExecStart,WorkingDirectory,MainPID --no-pager)

[[ "$service_state" == *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]] ||
  block "service is not using the atomic release launcher"

exec 8>"$DEPLOY_ROOT/learning-refresh.lock"
flock -w 30 8 || block "learning evaluation currently owns its lock"

git -C "$repo" fetch --no-tags origin codex/oracle-opening-hold-repair
git -C "$repo" cat-file -e "$target^{commit}" ||
  block "target commit is unavailable"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
metadata="$BACKUP_ROOT/predeploy-metadata-$stamp-$target"
mkdir -p "$metadata"

readlink -f "$DEPLOY_ROOT/current" > "$metadata/current-release-pointer.txt"
cp -a "$expected_release/.deployed-commit" \
  "$metadata/current-release-commit.txt"
git -C "$repo" status --short > "$metadata/oracle-worktree-status.txt"
git -C "$repo" diff --binary > "$metadata/oracle-worktree.patch"

if [[ -f "$repo/src/signals_display/monitor.py" ]]; then
  cp -a "$repo/src/signals_display/monitor.py" "$metadata/monitor.py.oracle"
fi

sudo -n systemctl cat "$SERVICE_NAME" |
  cat > "$metadata/systemd-service.txt"

if [[ -d "$MODEL_ROOT" ]]; then
  find "$MODEL_ROOT" -type f -print0 |
    sort -z |
    xargs -0 -r sha256sum > "$metadata/model-metadata.sha256"
fi

tools=$(mktemp -d "$DEPLOY_ROOT/.deploy-tools.XXXXXX")
validation=$(mktemp -d "$DEPLOY_ROOT/.validation.XXXXXX")

cleanup() {
  rm -rf "$tools" "$validation"
}
trap cleanup EXIT

git -C "$repo" archive "$target" deploy/oracle | tar -x -C "$tools"

for required in preflight.sh launch_first_release.sh remote_deploy.sh \
  health_check.py sqlite_backup.py run_dashboard.sh \
  kalshi-signals.override.conf.example release-policy.json; do
  [[ -r "$tools/deploy/oracle/$required" ]] ||
    block "target deployment tool missing: $required"
done

"$tools/deploy/oracle/preflight.sh" "$config" ||
  block "preflight failed before service modification"

git -C "$repo" archive "$target" | tar -x -C "$validation"

grep -Fq 'if len(future) < 30:' \
  "$validation/src/signals_display/learning.py" ||
  block "30-contract unseen-forward gate is missing"

if [[ -f "$validation/backups/pre_intelligence_20260731_212815.tar.gz" ]]; then
  if tar -tzf \
      "$validation/backups/pre_intelligence_20260731_212815.tar.gz" |
      grep -Eqi '(pem|private.?key|credentials|\.env$|id_rsa|id_ed25519)'; then
    block "target archive contains a sensitive-looking path"
  fi

  if tar -xOzf \
      "$validation/backups/pre_intelligence_20260731_212815.tar.gz" |
      grep -aqE 'BEGIN .*PRIVATE KEY'; then
    block "target archive contains private-key material"
  fi
fi

"$PYTHON_BIN" -m venv "$validation/.venv"
PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "$validation/.venv/bin/python3" -m pip install "${validation}[dev,dashboard]"

(
  cd "$validation"
  RUN_LIVE_TESTS=0 .venv/bin/python3 -m pytest -q \
    tests/test_oracle_deployment.py \
    tests/test_live_execution_disabled.py \
    tests/test_signals_opening_models.py \
    tests/test_signals_ensemble.py \
    tests/test_dashboard_launcher.py
)

"$expected_release/.venv/bin/python3" \
  "$expected_release/deploy/oracle/health_check.py" \
  --commit "$expected_current" \
  --data-root "$DATA_ROOT" \
  --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"

rollback_expected() {
  code=$?
  trap - ERR
  set +e

  current_now=$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null)

  if [[ "$current_now" != "$expected_release" ]] ||
     ! sudo -n systemctl is-active --quiet "$SERVICE_NAME" ||
     ! "$expected_release/.venv/bin/python3" \
       "$expected_release/deploy/oracle/health_check.py" \
       --commit "$expected_current" \
       --data-root "$DATA_ROOT" \
       --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}" \
       >/dev/null 2>&1; then

    ln -s "$expected_release" "$DEPLOY_ROOT/current.wrapper-rollback"
    mv -Tf "$DEPLOY_ROOT/current.wrapper-rollback" "$DEPLOY_ROOT/current"
    sudo -n systemctl restart "$SERVICE_NAME"

    "$expected_release/.venv/bin/python3" \
      "$expected_release/deploy/oracle/health_check.py" \
      --commit "$expected_current" \
      --data-root "$DATA_ROOT" \
      --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"
  fi

  echo "DEPLOYMENT_ROLLED_BACK commit=$expected_current failed_target=$target" >&2
  exit "$code"
}

trap rollback_expected ERR

"$tools/deploy/oracle/launch_first_release.sh" "$target"

new_current=$(readlink -f "$DEPLOY_ROOT/current")
[[ -f "$new_current/.deployed-commit" ]]
[[ $(cat "$new_current/.deployed-commit") == "$target" ]]

"$new_current/.venv/bin/python3" \
  "$new_current/deploy/oracle/health_check.py" \
  --commit "$target" \
  --data-root "$DATA_ROOT" \
  --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"

curl --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:8765/healthz
echo

# Production is now verified. Isolated evaluation failures below cannot
# roll back healthy production code.
trap - ERR

evaluation="$DEPLOY_ROOT/evaluations/$stamp-$target"

set +e
(
  set -Eeuo pipefail

  settlement_registry="$evaluation/settlement-registry"
  opening_registry="$evaluation/opening-registry"
  mkdir -p "$settlement_registry" "$opening_registry"

  if [[ -d "$MODEL_REGISTRY" ]]; then
    cp -a "$MODEL_REGISTRY"/. "$settlement_registry"/
  fi

  production_opening_registry="$(dirname "$OPENING_MODEL_ARTIFACT")/opening-model-registry"
  if [[ -d "$production_opening_registry" ]]; then
    cp -a "$production_opening_registry"/. "$opening_registry"/
  fi

  timeout --signal=TERM --kill-after=30s 600 \
    "$new_current/.venv/bin/python3" \
    -m src.signals_display.learning \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/settlement.json" \
    --registry "$settlement_registry"

  timeout --signal=TERM --kill-after=30s 600 \
    "$new_current/.venv/bin/python3" \
    -m src.signals_display.opening_models \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/opening.json" \
    --registry "$opening_registry"

  "$new_current/.venv/bin/python3" - \
    "$evaluation/opening.report.json" \
    "$evaluation/settlement.report.json" <<'PY'
import json
import sys
from pathlib import Path

for name, filename in (
    ("OPENING", sys.argv[1]),
    ("SETTLEMENT", sys.argv[2]),
):
    path = Path(filename)

    if not path.is_file():
        print(f"{name}_STATUS=UNAVAILABLE")
        print(f"{name}_QUALIFIES_FOR_ACTIVATION=false")
        continue

    report = json.loads(path.read_text())
    print(f"{name}_STATUS={report.get('status', 'UNAVAILABLE')}")
    print(f"{name}_SPLIT={report.get('split_contracts')}")
    print(
        f"{name}_TEST_METRICS="
        f"{(report.get('test') or {}).get('selected_candidate')}"
    )
    print(f"{name}_QUALIFIES_FOR_ACTIVATION=false")

print("MODEL_ACTIVATION_PERFORMED=false")
print("AUTOMATED_TRADING=DISABLED")
PY
)

evaluation_rc=$?
set -e

if (( evaluation_rc == 0 )); then
  echo "ISOLATED_EVALUATION_STATUS=COMPLETE path=$evaluation"
else
  echo "ISOLATED_EVALUATION_STATUS=FAILED rc=$evaluation_rc production_rollback=false" >&2
fi

echo "DEPLOYMENT_COMPLETE commit=$target"
echo "METADATA_BACKUP=$metadata"
echo "ISOLATED_EVALUATION=$evaluation"
echo "MODEL_ACTIVATION_PERFORMED=false"
echo "AUTOMATED_TRADING=DISABLED"
