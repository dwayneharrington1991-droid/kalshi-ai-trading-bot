#!/usr/bin/env bash
set -euo pipefail

config=${KALSHI_DEPLOY_CONFIG:-/etc/kalshi-signals/deployment.env}
# shellcheck disable=SC1090
source "$config"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}" \
  "${MODEL_ARTIFACT:?}" "${OPENING_MODEL_ARTIFACT:?}" "${MODEL_REGISTRY:?}"
account_mode_file="$DEPLOY_ROOT/account-monitoring.flags"
[[ -r "$account_mode_file" ]] || {
  echo "missing baseline-captured account monitoring mode" >&2; exit 1;
}
# This non-secret file is created from the existing effective systemd command
# during baseline preparation. Deployments never choose a new account mode.
# shellcheck disable=SC1090
source "$account_mode_file"
[[ ${ENABLE_ACCOUNT_READONLY:-} == 0 || ${ENABLE_ACCOUNT_READONLY:-} == 1 ]] || {
  echo "invalid baseline-captured account monitoring mode" >&2; exit 1;
}
release="$DEPLOY_ROOT/current"
commit=$(cat "$release/.deployed-commit")
args=(
  -m src.signals_display.server --mode public --host 127.0.0.1 --port 8765
  --journal "$JOURNAL_PATH" --account-store "$ACCOUNT_STORE_PATH"
  --model-artifact "$MODEL_ARTIFACT"
  --opening-model-artifact "$OPENING_MODEL_ARTIFACT"
  --model-registry "$MODEL_REGISTRY"
  --processing-ms 100 --build-commit "$commit"
)
if [[ $ENABLE_ACCOUNT_READONLY == 1 ]]; then
  args+=(--account-readonly --private-dashboard --require-readonly-scope)
fi
cd "$release"
exec "$release/.venv/bin/python3" "${args[@]}"
