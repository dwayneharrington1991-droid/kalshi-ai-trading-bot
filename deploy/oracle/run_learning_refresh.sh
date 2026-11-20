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

# Use the same lock domain as deployment and isolated evaluation.  The
# service-specific lock additionally prevents two timer invocations from
# entering the refresh together.  Locks are always acquired in this order;
# no other script acquires them in reverse order.
exec 8>"$DEPLOY_ROOT/learning.lock"
flock -n 8 || {
  echo "learning_refresh_skipped reason=deployment_or_evaluation_is_running" >&2; exit 0;
}
exec 9>"$DEPLOY_ROOT/learning-refresh.lock"
flock -n 9 || {
  echo "learning_refresh_skipped reason=another_refresh_is_running" >&2; exit 0;
}
opening_registry="$(dirname "$OPENING_MODEL_ARTIFACT")/opening-model-registry"
commit=$(cat "$release/.deployed-commit")
status_dir="$DEPLOY_ROOT/learning-status"
mkdir -p "$status_dir"
status_path="$status_dir/latest.json"
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
refresh_started=$SECONDS
echo "learning_refresh_start commit=$commit started_at=$started_at"

# All three jobs open the signal journal read-only. General scheduled evaluation may
# archive a candidate but cannot activate it. The opening job writes only a
# statistically gated, opening-targeted artifact and its audit report.
settlement_rc=0
settlement_started=$SECONDS
echo "learning_stage=settlement status=started"
timeout --signal=TERM --kill-after=30s 600 \
  "$release/.venv/bin/python3" -m src.signals_display.learning \
  --journal "$JOURNAL_PATH" --output "$MODEL_ARTIFACT" \
  --registry "$MODEL_REGISTRY" --scheduled --minimum-update-hours 1 || settlement_rc=$?
settlement_elapsed=$((SECONDS-settlement_started))
echo "learning_stage=settlement status=finished rc=$settlement_rc elapsed_seconds=$settlement_elapsed"
opening_rc=0
opening_started=$SECONDS
echo "learning_stage=opening status=started"
timeout --signal=TERM --kill-after=30s 600 \
  "$release/.venv/bin/python3" -m src.signals_display.opening_models \
  --journal "$JOURNAL_PATH" --output "$OPENING_MODEL_ARTIFACT" \
  --registry "$opening_registry" || opening_rc=$?
opening_elapsed=$((SECONDS-opening_started))
echo "learning_stage=opening status=finished rc=$opening_rc elapsed_seconds=$opening_elapsed"
scalp_rc=0
scalp_started=$SECONDS
echo "learning_stage=scalp status=started"
timeout --signal=TERM --kill-after=30s 600 \
  "$release/.venv/bin/python3" -m src.signals_display.scalp_learning \
  --journal "$JOURNAL_PATH" \
  --output "$MODEL_REGISTRY/latest-scalp-evaluation.json" \
  --minimum-update-hours 1 || scalp_rc=$?
scalp_elapsed=$((SECONDS-scalp_started))
echo "learning_stage=scalp status=finished rc=$scalp_rc elapsed_seconds=$scalp_elapsed"
total_elapsed=$((SECONDS-refresh_started))
finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
overall=COMPLETE
(( settlement_rc == 0 && opening_rc == 0 && scalp_rc == 0 )) || overall=PARTIAL_OR_FAILED
temporary="$status_path.tmp.$$"
"$release/.venv/bin/python3" - "$temporary" "$commit" "$started_at" "$finished_at" \
  "$overall" "$total_elapsed" "$settlement_rc" "$settlement_elapsed" \
  "$opening_rc" "$opening_elapsed" "$scalp_rc" "$scalp_elapsed" <<'PY_STATUS'
import json, os, sys
path = sys.argv[1]
names = ("settlement", "opening", "scalp")
values = sys.argv[7:]
stages = {
    name: {
        "return_code": int(values[index * 2]),
        "elapsed_seconds": int(values[index * 2 + 1]),
    }
    for index, name in enumerate(names)
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "commit": sys.argv[2], "started_at": sys.argv[3],
        "finished_at": sys.argv[4], "status": sys.argv[5],
        "elapsed_seconds": int(sys.argv[6]), "stages": stages,
        "model_promotion_performed": False,
        "automated_trading": "DISABLED",
    }, handle, sort_keys=True, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY_STATUS
chmod 0600 "$temporary"
mv -f "$temporary" "$status_path"
echo "learning_refresh_results settlement_rc=$settlement_rc opening_rc=$opening_rc scalp_rc=$scalp_rc"
if (( settlement_rc != 0 || opening_rc != 0 || scalp_rc != 0 )); then
  echo "learning_refresh_partial_or_failed" >&2
  exit 1
fi
echo "learning_refresh_complete commit=$commit elapsed_seconds=$total_elapsed status_file=$status_path"
