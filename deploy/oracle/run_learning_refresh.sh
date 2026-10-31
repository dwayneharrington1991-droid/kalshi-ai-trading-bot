#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

config=${KALSHI_DEPLOY_CONFIG:-/etc/kalshi-signals/deployment.env}
# shellcheck disable=SC1090
source "$config"
: "${DEPLOY_ROOT:?}" "${JOURNAL_PATH:?}" "${MODEL_ARTIFACT:?}" \
  "${OPENING_MODEL_ARTIFACT:?}" "${MODEL_REGISTRY:?}"
release=$(readlink -f "$DEPLOY_ROOT/current")
[[ -x "$release/.venv/bin/python3" && -f "$release/.deployed-commit" ]] || {
  echo "learning_refresh_failed reason=versioned_release_unavailable" >&2; exit 1;
}
[[ -f "$JOURNAL_PATH" && -r "$JOURNAL_PATH" ]] || {
  echo "learning_refresh_failed reason=journal_unavailable" >&2; exit 1;
}

exec 9>"$DEPLOY_ROOT/learning-refresh.lock"
flock -n 9 || {
  echo "learning_refresh_skipped reason=another_refresh_is_running" >&2; exit 0;
}
opening_registry="$(dirname "$OPENING_MODEL_ARTIFACT")/opening-model-registry"
echo "learning_refresh_start commit=$(cat "$release/.deployed-commit")"

# Both jobs open the signal journal read-only. General scheduled evaluation may
# archive a candidate but cannot activate it. The opening job writes only a
# statistically gated, opening-targeted artifact and its audit report.
timeout --signal=TERM --kill-after=30s 600 \
  "$release/.venv/bin/python3" -m src.signals_display.learning \
  --journal "$JOURNAL_PATH" --output "$MODEL_ARTIFACT" \
  --registry "$MODEL_REGISTRY" --scheduled --minimum-update-hours 24
timeout --signal=TERM --kill-after=30s 600 \
  "$release/.venv/bin/python3" -m src.signals_display.opening_models \
  --journal "$JOURNAL_PATH" --output "$OPENING_MODEL_ARTIFACT" \
  --registry "$opening_registry"
echo "learning_refresh_complete commit=$(cat "$release/.deployed-commit")"
