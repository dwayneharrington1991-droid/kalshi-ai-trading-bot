#!/usr/bin/env bash
# Score the pre-registered forward tests on Oracle while nobody is at the Windows PC.
# Same inputs as scripts/Run-ForwardEvalV2/V3/V4.ps1: the evaluator file is taken byte-for-byte from its FREEZE
# commit (git show), imports come from the currently deployed release, the journal is exported read-only by
# scripts/export_opening_research.py, and the live artifact is the production one. Research only: nothing is
# trained, tuned or changed. When a test reaches a decision the report is kept and one Telegram line is sent,
# once (a .decided marker prevents repeats). Never runs inside an opening window; one run at a time (flock).
set -uo pipefail
REPO=/home/opc/kalshi-ai-trading-bot
PY=$REPO/.venv/bin/python
OUT=/home/opc/dual-ai-alerts/forward
ART=/home/opc/.kalshi-deploy/preview-8768/models/qualified-20261116T210122Z.json
mkdir -p "$OUT" && chmod 700 "$OUT"
exec 9>"$OUT/run.lock"; flock -n 9 || { echo "another forward-test run is active"; exit 0; }
min=$(date -u +%M); sec=$(date -u +%S); m=$(( (10#$min % 15) * 60 + 10#$sec ))
if (( m < 150 || m > 810 )); then echo "inside an opening window; skipped"; exit 0; fi
BUILD=$(curl -s --max-time 10 http://127.0.0.1:8768/readyz | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("build_commit","")[:7])' 2>/dev/null)
SRC=/home/opc/kalshi-preview-repair-$BUILD
[[ -n $BUILD && -d $SRC/src ]] || { echo "deployed release not found ($BUILD)"; exit 3; }
now=$(date -u +%s)
# Daily read-only stability report (scripts/stability_report.py): first run of each UTC day, low priority.
REP=/home/opc/dual-ai-alerts/reports
mkdir -p "$REP"
if ! ls "$REP"/stability-$(date -u +%Y%m%d)*.json >/dev/null 2>&1; then
  nice -n 19 ionice -c3 timeout 300 python3 /home/opc/dual-ai-alerts/stability_report.py > "$REP/stability-$(date -u +%Y%m%dT%H%M%SZ).json" 2>"$REP/stability.err" || true
fi
dump=""
for spec in "v2 2d4d007 forward_eval_v2.py 2026-11-23T02:00:00Z" \
            "v3 30a5ee8 forward_eval_v3.py 2026-11-24T03:00:00Z" \
            "v4 2798cef forward_eval_v4.py 2026-11-24T05:30:00Z"; do
  set -- $spec; name=$1; freeze=$2; ev=$3; due=$(date -u -d "$4" +%s)
  [[ -f $OUT/$name.decided ]] && continue
  (( now >= due )) || continue
  W=$(mktemp -d)
  git -C "$REPO" show "$freeze:scripts/$ev" > "$W/$ev" || { echo "$name: frozen evaluator missing"; rm -rf "$W"; continue; }
  if [[ -z $dump ]]; then
    dump="$OUT/journal_dump-$(date -u +%Y%m%dT%H%M%SZ).json"
    ( cd "$SRC" && nice -n 19 ionice -c3 timeout 900 "$PY" scripts/export_opening_research.py ) > "$dump" || { echo "export failed"; rm -f "$dump"; rm -rf "$W"; exit 4; }
  fi
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  ( cd "$SRC" && PYTHONPATH="$SRC" nice -n 19 timeout 900 "$PY" "$W/$ev" --journal "$dump" --artifact "$ART" ) > "$OUT/$name-$stamp.json" 2> "$OUT/$name-$stamp.err"
  rm -rf "$W"
  decision=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("decision",""))' "$OUT/$name-$stamp.json" 2>/dev/null)
  echo "$name: $decision"
  if [[ -n $decision && $decision != "NOT ENOUGH FORWARD DATA" ]]; then
    cp "$OUT/$name-$stamp.json" "$OUT/$name.decided"
    PYTHONPATH=/home/opc/dual-ai-alerts/vendor "$PY" -c '
import sys
from src.signals_display import phone_alerts as p
cfg = p.load_config()
if "telegram" in p.channels(cfg):
    p.send("telegram", cfg, "8768 FORWARD TEST " + sys.argv[1] + ": " + sys.argv[2] + " (research only; no model changed)")
' "$name" "$decision" || true
  fi
done
# v5 clean-data retrain (docs/preregistration-v5-retrain-20261122.md): walk-forward on full-window openings,
# holdout from 2026-11-24 06:30 UTC, decision at >= 300 holdout openings. The script itself refuses to run
# earlier. Research only: the report is kept and one Telegram line sent at the decision; nothing is promoted.
v5_due=$(date -u -d "2026-11-24T06:35:00Z" +%s)
if [[ ! -f $OUT/v5.decided ]] && (( now >= v5_due )); then
  if [[ -z $dump ]]; then
    dump="$OUT/journal_dump-$(date -u +%Y%m%dT%H%M%SZ).json"
    ( cd "$SRC" && nice -n 19 ionice -c3 timeout 900 "$PY" scripts/export_opening_research.py ) > "$dump" || { echo "export failed"; rm -f "$dump"; exit 4; }
  fi
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  ( cd "$SRC" && PYTHONPATH="$SRC" nice -n 19 timeout 1200 "$PY" scripts/train_v5.py --journal "$dump" --artifact "$ART" --mode v5 ) > "$OUT/v5-$stamp.json" 2> "$OUT/v5-$stamp.err"
  decision=$("$PY" - "$OUT/v5-$stamp.json" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1])); e = r.get("evaluation") or {}
except Exception:
    print(""); sys.exit()
n = e.get("openings") or 0
if n < 300:
    print(f"NOT ENOUGH FORWARD DATA ({n}/300)")
else:
    passed = [k for k, v in (e.get("candidates") or {}).items() if v.get("passes")]
    print("PASSED: " + ", ".join(passed) if passed else "NO CANDIDATE PASSED")
PY
)
  echo "v5: $decision"
  if [[ -n $decision && $decision != NOT\ ENOUGH* ]]; then
    cp "$OUT/v5-$stamp.json" "$OUT/v5.decided"
    PYTHONPATH=/home/opc/dual-ai-alerts/vendor "$PY" -c '
import sys
from src.signals_display import phone_alerts as p
cfg = p.load_config()
if "telegram" in p.channels(cfg):
    p.send("telegram", cfg, "8768 v5 CLEAN-DATA RETRAIN: " + sys.argv[1] + " (research only; nothing promoted without approval)")
' "$decision" || true
  fi
fi
find "$OUT" -name 'journal_dump-*.json' -mtime +2 -delete 2>/dev/null
exit 0
