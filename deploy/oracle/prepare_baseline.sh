#!/usr/bin/env bash
set -Eeuo pipefail
umask 027
config=${1:-/etc/kalshi-signals/deployment.env}
[[ -r "$config" ]] || { echo "missing deployment config: $config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${SERVICE_NAME:?}" "${PYTHON_BIN:?}"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit("baseline requires the configured Python 3.12 interpreter")
PY
service_state=$(sudo -n systemctl show "$SERVICE_NAME" \
  --property=WorkingDirectory,ExecStart,MainPID --no-pager)
[[ "$service_state" != *"$DEPLOY_ROOT/current/deploy/oracle/run_dashboard.sh"* ]] || {
  echo "service already uses versioned releases; baseline preparation refused" >&2; exit 1;
}
detected_account=0
[[ "$service_state" == *"--account-readonly"* ]] && detected_account=1
[[ "$service_state" != *"--mode trading"* ]] || {
  echo "signals-only check failed" >&2; exit 1;
}

mkdir -p "$DEPLOY_ROOT/releases"
snapshot=$(mktemp "$DEPLOY_ROOT/.baseline-source.XXXXXX.tar")
trap 'rm -f "$snapshot"' EXIT
tar -C "$APP_ROOT" \
  --exclude='./.git' --exclude='./.venv' --exclude='./.venv.*' \
  --exclude='./data' --exclude='./.deploy' --exclude='./.env*' \
  --exclude='*.pem' --exclude='*.key' --exclude='*.db' --exclude='*.db-*' \
  -cf "$snapshot" .
identity=$(sha256sum "$snapshot" | awk '{print substr($1,1,40)}')
release="$DEPLOY_ROOT/releases/$identity"
[[ ! -e "$release" ]] || { echo "baseline release already exists: $release" >&2; exit 1; }
staging="$DEPLOY_ROOT/releases/.baseline-$identity-$$"
mkdir "$staging"
tar -xf "$snapshot" -C "$staging"

# Use the reviewed deployment controls while preserving the exact current
# application source, including the locally modified monitor.py.
tools=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$staging/deploy/oracle"
for name in run_dashboard.sh health_check.py sqlite_backup.py release-policy.json; do
  cp "$tools/$name" "$staging/deploy/oracle/$name"
done
printf '%s\n' "$identity" > "$staging/.deployed-commit"
"$PYTHON_BIN" -m venv "$staging/.venv"
"$staging/.venv/bin/python3" -m pip install --disable-pip-version-check -e "$staging[dev]"

required_tests=(tests/test_signals_monitor.py tests/test_signals_prediction_engine.py tests/test_signals_browser.py)
for test_file in "${required_tests[@]}"; do
  [[ -f "$staging/$test_file" ]] || { echo "baseline test missing: $test_file" >&2; exit 1; }
done
(cd "$staging" && "$staging/.venv/bin/python3" -m pytest -q "${required_tests[@]}")
mv "$staging" "$release"

flags_tmp="$DEPLOY_ROOT/account-monitoring.flags.next"
printf 'ENABLE_ACCOUNT_READONLY=%s\n' "$detected_account" > "$flags_tmp"
chmod 600 "$flags_tmp"
mv -f "$flags_tmp" "$DEPLOY_ROOT/account-monitoring.flags"
ln -s "$release" "$DEPLOY_ROOT/current.next"
mv -Tf "$DEPLOY_ROOT/current.next" "$DEPLOY_ROOT/current"

echo "baseline=PREPARED identity=$identity release=$release"
echo "account_readonly_preserved=$detected_account"
echo "service_modified=false service_restarted=false"
echo "production_approval_marker_created=false"
echo "NEXT: review this baseline and run preflight before separately switching systemd."
