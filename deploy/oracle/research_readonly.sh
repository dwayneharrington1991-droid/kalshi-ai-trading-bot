#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only opening research on Oracle. Run from an extracted copy of the code.
# It never stops, starts or signals any process, never writes to a journal,
# artifact or registry, and never changes ports 8765/8767/8768. It reports the
# model the running preview actually uses and runs opening_research against
# the preview journal opened read-only, at low CPU priority.

readonly CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
readonly PYTHON=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
readonly OUT_DIR=/home/opc/kalshi-research/$(date -u +%Y%m%dT%H%M%SZ)
readonly PORT=8768

say() { printf 'RESEARCH: %s\n' "$*"; }
block() { printf 'RESEARCH_BLOCKED: %s\n' "$*" >&2; exit 1; }

argv_value() {
  local pid=$1 flag=$2 index
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline"
  for ((index=0; index<${#argv[@]}-1; index++)); do
    if [[ ${argv[index]} == "$flag" ]]; then
      printf '%s\n' "${argv[index+1]}"
      return 0
    fi
  done
  return 0
}

[[ $(id -un) == opc ]] || block "must run as opc"
[[ -x $PYTHON ]] || block "Python interpreter missing: $PYTHON"
listing=$(sudo -n ss -H -ltnp "sport = :$PORT" 2>/dev/null || ss -H -ltnp "sport = :$PORT" 2>/dev/null || true)
pid=$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$listing" | sort -u | head -1)
[[ -n $pid ]] || block "no process is listening on preview port $PORT"
journal=$(argv_value "$pid" --journal)
artifact=$(argv_value "$pid" --opening-model-artifact)
[[ -n $journal && -f $journal ]] || block "could not identify the preview journal from the running process"
install -d -m 0700 "$OUT_DIR"

say "preview pid=$pid journal=$journal"
say "opening artifact=${artifact:-NONE (statistical fallback only)}"
PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 "$PYTHON" -P -c '
import hashlib, json, sys
from urllib.request import ProxyHandler, build_opener
path = sys.argv[1]
summary = {"artifact_path": path or None}
if path:
    from src.signals_display.opening_models import load_artifact
    data = json.load(open(path))
    loaded, reason = load_artifact(path)
    report = data.get("report", {})
    selective = report.get("selective_signal_evaluation") or {}
    summary.update(
        sha256=hashlib.sha256(open(path, "rb").read()).hexdigest(),
        loads_with_this_code=loaded is not None, load_reason=reason,
        model_id=data.get("model_id"), model_kind=(data.get("model") or {}).get("kind"),
        decision_threshold=(data.get("decision_policy") or {}).get("threshold"),
        labels_available_through=data.get("labels_available_through"),
        opening_contracts=report.get("opening_contracts"), split_contracts=report.get("split_contracts"),
        validation=report.get("validation", {}).get("log_loss"),
        test_selected=(report.get("test") or {}).get("selected_candidate", {}),
        test_baseline=(report.get("test") or {}).get("opening_statistical_baseline", {}),
        selective_test_at_threshold=selective.get("test"),
    )
    for key in ("test_selected", "test_baseline"):
        summary[key] = {k: summary[key].get(k) for k in ("samples", "accuracy", "brier", "log_loss")}
    test = summary.get("selective_test_at_threshold") or {}
    summary["selective_test_at_threshold"] = {k: test.get(k) for k in (
        "independent_signals", "eligible_markets", "actionable_market_coverage",
        "actionable_directional_accuracy", "accuracy_interval_95")}
try:
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8768/api/state?view=main", timeout=8) as r:
        state = json.load(r)
    initial = (state.get("prediction_engine") or {}).get("initial") or {}
    summary["live_opening_now"] = {k: initial.get(k) for k in ("ticker", "model", "up", "direction")}
    summary["live_opening_now"]["confidence_status"] = (initial.get("confidence") or {}).get("status")
    summary["live_opening_now"]["decision_threshold"] = (initial.get("decision_policy") or {}).get("threshold")
except Exception as exc:
    summary["live_opening_now"] = "unavailable: " + type(exc).__name__
print(json.dumps(summary, indent=1, default=str))
' "$artifact" | tee "$OUT_DIR/running_model.json"

say "running read-only opening research (low priority; can take several minutes)"
cd "$OUT_DIR"
PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 nice -n 15 timeout 3h \
  "$PYTHON" -P -m src.signals_display.opening_research \
  --journal "$journal" --output "$OUT_DIR/opening_research.report.json"
say "REPORT_DIR=$OUT_DIR"
printf 'RESEARCH_COMPLETE journal_unchanged=READ_ONLY services_unchanged=YES trading=DISABLED\n'
