#!/usr/bin/env bash
set -euo pipefail

config=${KALSHI_DEPLOY_CONFIG:-/etc/kalshi-signals/deployment.env}
# shellcheck disable=SC1090
source "$config"
: "${APP_ROOT:?}" "${DEPLOY_ROOT:?}" "${JOURNAL_PATH:?}" "${ACCOUNT_STORE_PATH:?}" \
  "${MODEL_ARTIFACT:?}" "${OPENING_MODEL_ARTIFACT:?}" "${MODEL_REGISTRY:?}"
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
if [[ ${ENABLE_ACCOUNT_READONLY:-0} == 1 ]]; then
  args+=(--account-readonly --private-dashboard --require-readonly-scope)
fi
cd "$release"
exec "$release/.venv/bin/python3" "${args[@]}"
