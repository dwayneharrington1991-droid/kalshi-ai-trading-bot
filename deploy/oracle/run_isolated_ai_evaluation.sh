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

exec 8>"$DEPLOY_ROOT/learning.lock"
flock -w 30 8 || fail "another learning evaluation owns the lock"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
evaluation="$DEPLOY_ROOT/evaluations/$stamp-$target"
source_tree="$evaluation/source"
settlement_registry="$evaluation/settlement-registry"
opening_registry="$evaluation/opening-registry"
mkdir -p "$source_tree" "$settlement_registry" "$opening_registry"
git -C "$repo" archive "$target" | tar -x -C "$source_tree"

if [[ -d "$MODEL_REGISTRY" ]]; then
  cp -a "$MODEL_REGISTRY"/. "$settlement_registry"/
fi
production_opening_registry="$(dirname "$OPENING_MODEL_ARTIFACT")/opening-model-registry"
if [[ -d "$production_opening_registry" ]]; then
  cp -a "$production_opening_registry"/. "$opening_registry"/
fi

settlement_rc=0
(
  cd "$source_tree"
  timeout --signal=TERM --kill-after=30s 900 \
    "$current/.venv/bin/python3" -m src.signals_display.learning \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/settlement.json" \
    --registry "$settlement_registry"
) || settlement_rc=$?

opening_rc=0
(
  cd "$source_tree"
  timeout --signal=TERM --kill-after=30s 900 \
    "$current/.venv/bin/python3" -m src.signals_display.opening_models \
    --journal "$JOURNAL_PATH" \
    --output "$evaluation/opening.json" \
    --registry "$opening_registry"
) || opening_rc=$?

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
    print(f"{name}_TEST_METRICS={(report.get('test') or {}).get('selected_candidate')}")
    print(f"{name}_QUALIFIES_FOR_ACTIVATION=false")

print("MODEL_ACTIVATION_PERFORMED=false")
print("AUTOMATED_TRADING=DISABLED")
PY

echo "SETTLEMENT_EVALUATION_RC=$settlement_rc"
echo "OPENING_EVALUATION_RC=$opening_rc"
echo "ISOLATED_EVALUATION=$evaluation"
if (( settlement_rc == 0 && opening_rc == 0 )); then
  echo "ISOLATED_EVALUATION_STATUS=COMPLETE"
else
  echo "ISOLATED_EVALUATION_STATUS=PARTIAL_OR_FAILED"
  exit 1
fi
