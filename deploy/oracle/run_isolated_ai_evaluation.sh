#!/usr/bin/env bash
set -Euo pipefail
umask 027

target=${1:?exact evaluation commit required}
repo=/home/opc/kalshi-ai-trading-bot
config=/etc/kalshi-signals/deployment.env

fail() {
  echo "EVALUATION_BLOCKED: $*" >&2
  exit 1
}

[[ $(id -un) == opc ]] || fail "must run as opc"
[[ -r "$config" ]] || fail "deployment configuration is unreadable"
# shellcheck source=/dev/null
source "$config"
: "${DEPLOY_ROOT:?}" "${JOURNAL_PATH:?}" "${MODEL_REGISTRY:?}" \
  "${OPENING_MODEL_ARTIFACT:?}"
[[ -L "$DEPLOY_ROOT/current" ]] || fail "versioned current release is missing"
current=$(readlink -f "$DEPLOY_ROOT/current")
[[ -x "$current/.venv/bin/python3" ]] || fail "current release Python is unavailable"
git -C "$repo" cat-file -e "$target^{commit}" || fail "target commit is unavailable"

# Never fall back to a multi-gigabyte full predictions scan. Index creation is
# deliberately excluded here because the live collector must remain running.
"$current/.venv/bin/python3" - "$JOURNAL_PATH" <<'PY' || exit $?
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
try:
    indexes = {row[1] for row in db.execute("PRAGMA index_list(predictions)")}
    if "predictions_ticker_mode" not in indexes:
        print("EVALUATION_BLOCKED: predictions_ticker_mode index is unavailable; refusing a full journal scan", file=sys.stderr)
        raise SystemExit(1)
finally:
    db.close()
print("evaluation_index_check=PASS index=predictions_ticker_mode")
PY

exec 8>"$DEPLOY_ROOT/learning.lock"
flock -w 30 8 || fail "another learning evaluation owns the lock"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
evaluation="$DEPLOY_ROOT/evaluations/$stamp-$target"
source_tree="$evaluation/source"
settlement_registry="$evaluation/settlement-registry"
opening_registry="$evaluation/opening-registry"
mkdir -p "$source_tree" "$settlement_registry" "$opening_registry"
git -C "$repo" archive "$target" | tar -x -C "$source_tree"

failure_report() {
  local path=$1 component=$2 rc=$3 elapsed=$4
  [[ -f "$path" ]] && return 0
  "$current/.venv/bin/python3" - "$path" "$component" "$rc" "$elapsed" <<'PY'
import json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
path, component, rc, elapsed = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
status = "EVALUATION_TIMEOUT" if rc == 124 else "EVALUATION_FAILED"
value = {
    "status": status, "component": component, "return_code": rc,
    "elapsed_seconds": elapsed, "generated_at": datetime.now(timezone.utc).isoformat(),
    "trained": False, "promotion_allowed": False, "active_model_changed": False,
    "reason": "Bounded evaluator timed out" if rc == 124 else "Evaluator exited unsuccessfully",
}
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".failure-", suffix=".json", dir=path.parent)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, sort_keys=True, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PY
}

if [[ -d "$MODEL_REGISTRY" ]]; then
  cp -a "$MODEL_REGISTRY"/. "$settlement_registry"/
fi
production_opening_registry="$(dirname "$OPENING_MODEL_ARTIFACT")/opening-model-registry"
if [[ -d "$production_opening_registry" ]]; then
  cp -a "$production_opening_registry"/. "$opening_registry"/
fi

settlement_rc=0
settlement_started=$SECONDS
echo "EVALUATION_STAGE=SETTLEMENT status=STARTED"
(
  cd "$source_tree"
  timeout --signal=TERM --kill-after=30s 900 \
    "$current/.venv/bin/python3" -m src.signals_display.learning \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/settlement.json" \
    --registry "$settlement_registry"
) || settlement_rc=$?
settlement_elapsed=$((SECONDS-settlement_started))
echo "EVALUATION_STAGE=SETTLEMENT status=FINISHED rc=$settlement_rc elapsed_seconds=$settlement_elapsed"
(( settlement_rc == 0 )) || failure_report "$evaluation/settlement.report.json" SETTLEMENT "$settlement_rc" "$settlement_elapsed"

opening_rc=0
opening_started=$SECONDS
echo "EVALUATION_STAGE=OPENING status=STARTED"
(
  cd "$source_tree"
  timeout --signal=TERM --kill-after=30s 900 \
    "$current/.venv/bin/python3" -m src.signals_display.opening_models \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/opening.json" \
    --registry "$opening_registry"
) || opening_rc=$?
opening_elapsed=$((SECONDS-opening_started))
echo "EVALUATION_STAGE=OPENING status=FINISHED rc=$opening_rc elapsed_seconds=$opening_elapsed"
(( opening_rc == 0 )) || failure_report "$evaluation/opening.report.json" OPENING "$opening_rc" "$opening_elapsed"

scalp_rc=0
scalp_started=$SECONDS
echo "EVALUATION_STAGE=SCALP status=STARTED"
(
  cd "$source_tree"
  timeout --signal=TERM --kill-after=30s 900 \
    "$current/.venv/bin/python3" -m src.signals_display.scalp_learning \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/scalp.report.json" \
    --minimum-update-hours 1
) || scalp_rc=$?
scalp_elapsed=$((SECONDS-scalp_started))
echo "EVALUATION_STAGE=SCALP status=FINISHED rc=$scalp_rc elapsed_seconds=$scalp_elapsed"
(( scalp_rc == 0 )) || failure_report "$evaluation/scalp.report.json" SCALP "$scalp_rc" "$scalp_elapsed"

"$current/.venv/bin/python3" - \
  "$evaluation/opening.report.json" \
  "$evaluation/settlement.report.json" <<'PY'
import json
import sys
from pathlib import Path

for name, filename in (("OPENING", sys.argv[1]), ("SETTLEMENT", sys.argv[2])):
    path = Path(filename)
    if not path.is_file():
        print(f"{name}_STATUS=UNAVAILABLE")
        print(f"{name}_QUALIFIES_FOR_ACTIVATION=false")
        continue
    report = json.loads(path.read_text())
    print(f"{name}_STATUS={report.get('status', 'UNAVAILABLE')}")
    print(f"{name}_SPLIT={report.get('split_contracts')}")
    print(f"{name}_ELIGIBLE_CONTRACTS={report.get('available_eligible_contracts', report.get('opening_contracts'))}")
    print(f"{name}_PREVIOUSLY_EXPOSED={report.get('previously_exposed_contracts')}")
    print(f"{name}_UNSEEN_FORWARD={report.get('unseen_forward_contracts')}")
    print(f"{name}_REQUIRED_UNSEEN={report.get('required_unseen_forward_contracts', 30)}")
    print(f"{name}_ADDITIONAL_NEEDED={report.get('additional_unseen_contracts_needed')}")
    print(f"{name}_LAST_EXPOSED_CONTRACT={report.get('last_exposed_contract')}")
    if name == "OPENING":
        print(f"{name}_DATASET_COVERAGE={report.get('dataset_coverage')}")
    else:
        print(f"{name}_RECORDED_OFFICIAL={report.get('recorded_official_contracts')}")
        print(f"{name}_EXCLUDED_OFFICIAL={report.get('excluded_official_contracts')}")
        print(f"{name}_AFTER_COST_TEST={report.get('after_cost_test')}")
    print(f"{name}_TEST_METRICS={(report.get('test') or {}).get('selected_candidate')}")
    print(f"{name}_QUALIFIES_FOR_ACTIVATION=false")

print("MODEL_ACTIVATION_PERFORMED=false")
print("AUTOMATED_TRADING=DISABLED")
PY

"$current/.venv/bin/python3" - "$evaluation/scalp.report.json" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    print("SCALP_STATUS=UNAVAILABLE")
    print("SCALP_QUALIFIES_FOR_ACTIVATION=false")
else:
    report = json.loads(path.read_text())
    print(f"SCALP_STATUS={report.get('status', 'UNAVAILABLE')}")
    print(f"SCALP_CONTRACTS={report.get('contracts')}")
    print(f"SCALP_SPLIT={report.get('split_contracts')}")
    print(f"SCALP_TEST_METRICS={report.get('test')}")
    print("SCALP_QUALIFIES_FOR_ACTIVATION=false")
PY

echo "SETTLEMENT_EVALUATION_RC=$settlement_rc"
echo "OPENING_EVALUATION_RC=$opening_rc"
echo "SCALP_EVALUATION_RC=$scalp_rc"
echo "ISOLATED_EVALUATION=$evaluation"
if (( settlement_rc == 0 && opening_rc == 0 && scalp_rc == 0 )); then
  echo "ISOLATED_EVALUATION_STATUS=COMPLETE"
else
  echo "ISOLATED_EVALUATION_STATUS=PARTIAL_OR_FAILED"
  exit 1
fi
