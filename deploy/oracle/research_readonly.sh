#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only opening research on Oracle. Run from an extracted copy of the code.
# It never stops, starts or signals any process, never writes to a journal,
# artifact or registry, and never changes ports 8765/8767/8768. It reports the
# model the running preview actually uses and runs opening_research against
# the preview journal opened read-only, at low CPU priority.

readonly CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
readonly PYTHON=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
# One persistent run folder: its manifest pins the as-of time so a rerun
# resumes on exactly the same data, and its checkpoints keep completed folds.
# Everything written lives under /home/opc/kalshi-research only.
readonly RUN_ROOT=${RESEARCH_RUN_ROOT:-/home/opc/kalshi-research/phase4-run}
readonly OUT_DIR=$RUN_ROOT/reports-$(date -u +%Y%m%dT%H%M%SZ)
readonly RESEARCH_TIMEOUT=${RESEARCH_TIMEOUT:-6h}
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

listener_pid() {
  local listing
  listing=$(sudo -n ss -H -ltnp "sport = :$1" 2>/dev/null || ss -H -ltnp "sport = :$1" 2>/dev/null || true)
  sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$listing" | sort -u | head -1
}

# Journal path of a running signals process, resolved against its working dir.
journal_of() {
  local pid=$1 path
  path=$(argv_value "$pid" --journal)
  [[ -n $path ]] || return 0
  [[ $path == /* ]] || path="$(readlink -f "/proc/$pid/cwd")/$path"
  [[ -f $path ]] && readlink -f "$path"
  return 0
}

[[ $(id -un) == opc ]] || block "must run as opc"
[[ -x $PYTHON ]] || block "Python interpreter missing: $PYTHON"
pid=$(listener_pid "$PORT")
[[ -n $pid ]] || block "no process is listening on preview port $PORT"
artifact=$(argv_value "$pid" --opening-model-artifact)
preview_journal=$(journal_of "$pid")
[[ -n $preview_journal ]] || block "could not identify the port $PORT journal"
install -d -m 0700 "$RUN_ROOT" "$RUN_ROOT/checkpoints" "$OUT_DIR"

# Every running signals bot's journal is a candidate source of genuinely
# recorded frozen openings. Each is opened read-only only to count eligible
# openings; the largest is researched. Nothing is written to any journal.
declare -a journals=()
declare -A journal_port=()
for port in 8768 8765 8767; do
  other=$(listener_pid "$port")
  [[ -n $other ]] || continue
  found=$(journal_of "$other")
  if [[ -n $found && " ${journals[*]} " != *" $found "* ]]; then
    journals+=("$found")
    journal_port["$found"]=$port
    say "port $port journal=$found"
  fi
done
(( ${#journals[@]} > 0 )) || block "could not identify any signals journal from the running processes"
largest_journal=$(PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 "$PYTHON" -P -c '
import json, sqlite3, sys
from contextlib import closing
from pathlib import Path
from src.signals_display.opening_models import dataset_coverage
best, counts = None, {}
for path in sys.argv[1:]:
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            coverage = dataset_coverage(db)
        counts[path] = {"official_settled": coverage["official_settled_contracts"],
                        "eligible_frozen_openings": coverage["eligible_frozen_openings"],
                        "top_exclusions": dict(sorted(coverage["exclusion_reasons"].items(), key=lambda kv: -kv[1])[:4])}
        if best is None or coverage["eligible_frozen_openings"] > counts[best]["eligible_frozen_openings"]:
            best = path
    except (sqlite3.Error, OSError, KeyError, ValueError) as exc:
        counts[path] = {"error": type(exc).__name__}
print(json.dumps(counts, indent=1), file=sys.stderr)
print(best or "")
' "${journals[@]}" 2> >(tee "$OUT_DIR/journal_coverage.json" >&2))
[[ -n $largest_journal ]] || block "no readable journal with opening records"
# Model diagnostics and Report B must use the same journal as the running
# preview artifact.  A larger production/legacy journal may be useful for its
# own Report A, but pairing it with the 8768 model would mix deployments.
journal=$preview_journal
[[ -r $journal ]] || block "port $PORT journal is not readable: $journal"
say "researching the verified port $PORT journal: $journal"
say "largest historical journal (Report A only if different): $largest_journal"
say "preview pid=$pid"
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
    # The inputs the trained model actually uses: the component preprocessors.
    model = data.get("model") or {}
    components = model.get("components") or [model]
    used = sorted({name for part in components
                   for name in ((part.get("preprocessor") or {}).get("names") or [])})
    summary["model_input_features"] = used
    summary["model_input_feature_count"] = len(used)
    summary["model_components"] = [
        {"kind": part.get("kind"), "feature_set": part.get("feature_set"),
         "features": len((part.get("preprocessor") or {}).get("names") or [])}
        for part in components
    ]
    summary["mixture_weights"] = model.get("mixture_weights")
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

current_model=""
if [[ -n $artifact && -f $artifact ]]; then
  current_model=$(PYTHONNOUSERSITE=1 "$PYTHON" -P -c 'import json,sys; m=json.load(open(sys.argv[1])).get("model_id"); print("opening-candidate/" + m if m else "")' "$artifact" 2>/dev/null || true)
fi
say "running model label for score/model-separated comparisons: ${current_model:-NONE}"

manifest=$RUN_ROOT/manifest.json
asof=$(PYTHONNOUSERSITE=1 "$PYTHON" -P - "$manifest" "$journal" <<'PY'
import json, os, sys
from datetime import datetime, timezone
path, journal = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
if data.get("journal") != journal or not data.get("asof"):
    data = {"journal": journal, "asof": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat()}
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, path)
    print("NEW " + data["asof"])
else:
    print("RESUME " + data["asof"])
PY
)
say "run $asof (manifest $manifest; checkpoints $RUN_ROOT/checkpoints)"
asof=${asof#* }
run_py() { PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 nice -n 15 "$PYTHON" -P "$@"; }
cd "$OUT_DIR"
say "Report A: settled audit of displayed calls (port ${journal_port[$journal]} journal)"
run_py -m src.signals_display.opening_research --journal "$journal" --asof "$asof" \
  --output "$OUT_DIR/opening_research.report.json" --audit-only \
  ${current_model:+--current-model "$current_model"} | tee "$OUT_DIR/summary_reports_A_B.txt"
# Report A per running service: the dashboard counter on each port counts
# that port's own journal, so each is audited separately and labelled.
for each in "${journals[@]}"; do
  [[ $each == "$journal" ]] && continue
  port_dir="$OUT_DIR/port-${journal_port[$each]}"
  install -d -m 0700 "$port_dir"
  say "Report A for port ${journal_port[$each]} journal (dashboard reconciliation)"
  run_py -m src.signals_display.opening_research --journal "$each" --asof "$asof" \
    --output "$port_dir/opening_research.report.json" --audit-only \
    | sed "s/^/[port ${journal_port[$each]}] /" | tee -a "$OUT_DIR/summary_reports_A_B.txt" \
    || say "Report A for port ${journal_port[$each]} unavailable; continuing"
done
if [[ -n $artifact && -f $artifact ]]; then
  say "Model diagnostics: deployed artifact components, inputs and contributions (artifact read only)"
  run_py -m src.signals_display.artifact_diagnostics --journal "$journal" --artifact "$artifact" --asof "$asof" \
    --output "$OUT_DIR/model_diagnostics.json" | tee -a "$OUT_DIR/summary_reports_A_B.txt" \
    || say "model diagnostics unavailable; continuing"
fi
say "running model label for same-contract comparison: ${current_model:-NONE}"
say "Report B: offline model research (resumable; limit $RESEARCH_TIMEOUT; rerun the same command to continue)"
set +e
PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 nice -n 15 timeout "$RESEARCH_TIMEOUT" \
  "$PYTHON" -P -m src.signals_display.opening_research --journal "$journal" --asof "$asof" \
  --checkpoint-dir "$RUN_ROOT/checkpoints" --output "$OUT_DIR/opening_research.report.json" \
  ${current_model:+--current-model "$current_model"} \
  --intracontract --retrospective | tee -a "$OUT_DIR/summary_reports_A_B.txt"
status=${PIPESTATUS[0]}
set -e
if (( status == 124 )); then
  say "REPORT_DIR=$OUT_DIR"
  printf 'RESEARCH_INCOMPLETE time limit reached; completed folds are checkpointed. Rerun the same command to resume. journal_unchanged=READ_ONLY services_unchanged=YES trading=DISABLED\n'
  exit 124
fi
(( status == 0 )) || block "research exited with status $status (completed folds are checkpointed)"
say "REPORT_DIR=$OUT_DIR"
printf 'RESEARCH_COMPLETE journal_unchanged=READ_ONLY services_unchanged=YES trading=DISABLED\n'
