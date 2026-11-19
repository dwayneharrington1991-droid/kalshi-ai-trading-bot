#!/usr/bin/env bash
# READ-ONLY shadow evaluation of the experimental 8768 upgrade on Oracle.
#
#   shadow_upgrade_8768.sh <commit-sha> [shadow_upgrade|combined_opening]
#
# combined_opening (the combined decision engine) is anchored to the first
# shadow_upgrade report: its holdout start and as-of time fix which contracts
# are development, previously viewed holdout, and fresh unseen markets.
#
# 1. First run: the Phase 1 audit (audit_8768_readonly.sh), then extracts
#    src/ of <commit-sha> into /home/opc/kalshi-research/shadow-upgrade-<sha>
#    and starts shadow_upgrade against the 8768 journal opened read-only
#    (mode=ro, query_only), detached, at nice 19 / lowest best-effort I/O, with a
#    3-hour limit and a pinned as-of time.
# 2. It then follows progress for up to FOLLOW_MINUTES. Rerunning the same
#    command reattaches to a running job or prints the finished summary; it
#    never starts a second job.
# Never stops, restarts or signals 8768 (or anything else), never writes a
# journal, artifact, registry or setting; writes only inside its run folder.
set -uo pipefail
SHA=${1:-}
MODULE=${2:-shadow_upgrade}
[[ $SHA =~ ^[0-9a-f]{40}$ && $MODULE =~ ^(shadow_upgrade|combined_opening)$ ]] \
  || { echo "usage: $0 <40-hex commit sha> [shadow_upgrade|combined_opening]"; exit 2; }
REPO=${SHADOW_REPO:-/home/opc/kalshi-ai-trading-bot}
ROOT=${SHADOW_RUN_ROOT:-/home/opc/kalshi-research}
PREVIOUS=${SHADOW_PREVIOUS_REPORT:-$ROOT/shadow-upgrade-e2357e058e67/report.json}
RUN=$ROOT/${MODULE//_/-}-${SHA:0:12}
[[ $MODULE == shadow_upgrade ]] && RUN=$ROOT/shadow-upgrade-${SHA:0:12}
EXTRA=()
if [[ $MODULE == combined_opening ]]; then
  [[ -f $PREVIOUS ]] || { echo "BLOCKED: previous shadow report $PREVIOUS not found"; exit 2; }
  EXTRA=(--previous-report "$PREVIOUS")
fi
FOLLOW_MINUTES=${FOLLOW_MINUTES:-50}
umask 077
pid_of() { ( sudo -n ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }

P=$(pid_of 8768); [[ -n $P ]] || { echo "BLOCKED: no listener on 8768"; exit 2; }
mapfile -d '' -t A <"/proc/$P/cmdline"
CWD=$(readlink -f "/proc/$P/cwd"); PY=${A[0]}; [[ $PY == /* ]] || PY=$(readlink -f "/proc/$P/exe")
J=""; for ((i=0; i<${#A[@]}-1; i++)); do [[ ${A[i]} == --journal ]] && J=${A[i+1]}; done
[[ -n $J && $J != /* ]] && J=$CWD/$J
[[ -f $J ]] || { echo "BLOCKED: 8768 journal not found"; exit 2; }

summary() {
  ( cd "$RUN/code" && "$PY" -c 'import json,sys; import importlib; s = importlib.import_module("src.signals_display." + sys.argv[2]); print(s.summary(json.load(open(sys.argv[1]))))' "$RUN/report.json" "$MODULE" )
  echo "SHADOW_REPORT $RUN/report.json"
}
running() { [[ -f $RUN/pid ]] && kill -0 "$(cat "$RUN/pid")" 2>/dev/null; }

if [[ -f $RUN/report.json ]]; then summary; exit 0; fi
if ! running; then
  mkdir -p "$RUN"
  free_mib=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  load1=$(cut -d' ' -f1 /proc/loadavg)
  (( free_mib >= 768 )) || { echo "BLOCKED: MemAvailable ${free_mib} MiB < 768; nothing started"; exit 3; }
  awk -v l="$load1" 'BEGIN{exit !(l <= 10)}' || { echo "BLOCKED: load ${load1} > 10; nothing started"; exit 3; }
  (( $(df -Pk "$RUN" | awk 'NR==2{print $4}') >= 1048576 )) || { echo "BLOCKED: < 1 GiB free for the report"; exit 3; }
  git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "BLOCKED: commit $SHA not fetched"; exit 2; }
  if [[ ! -f $RUN/audit.txt ]]; then
    timeout 600 bash <(git -C "$REPO" show "$SHA:deploy/oracle/audit_8768_readonly.sh") >"$RUN/audit.txt" 2>&1
  fi
  cat "$RUN/audit.txt"
  rm -rf "$RUN/code.next" && mkdir -p "$RUN/code.next" \
    && git -C "$REPO" archive "$SHA" src | tar -x -C "$RUN/code.next" && rm -rf "$RUN/code" && mv "$RUN/code.next" "$RUN/code" \
    || { echo "BLOCKED: could not extract code"; exit 2; }
  "$PY" -c 'import numpy' 2>/dev/null || echo "NOTE: numpy missing in the 8768 interpreter; the boosted-tree candidate will be skipped"
  [[ -f $RUN/asof ]] || date -u +%Y-%m-%dT%H:%M:%S+00:00 >"$RUN/asof"
  echo "SHADOW: starting on $(basename "$J") (read-only), as-of $(cat "$RUN/asof"), output $RUN"
  ( cd "$RUN/code" && exec setsid nohup nice -n 19 ionice -c2 -n7 timeout 3h "$PY" -m "src.signals_display.$MODULE" \
      --journal "$J" --output "$RUN/report.json" --asof "$(cat "$RUN/asof")" "${EXTRA[@]}" >"$RUN/log" 2>&1 </dev/null ) &
  echo $! >"$RUN/pid"
  sleep 2
else
  echo "SHADOW: reattaching to the running evaluation (pid $(cat "$RUN/pid"))"
fi

deadline=$(( $(date +%s) + FOLLOW_MINUTES * 60 )); shown=0
while (( $(date +%s) < deadline )); do
  n=$(wc -l <"$RUN/log" 2>/dev/null || echo 0)
  (( n > shown )) && { sed -n "$((shown + 1)),${n}p" "$RUN/log" | grep -E '^SHADOW:|Error|Traceback' | cut -c1-200; shown=$n; }
  [[ -f $RUN/report.json ]] && { summary; exit 0; }
  running || { echo "SHADOW_FAILED: the evaluation ended without a report; last lines:"; tail -n 15 "$RUN/log"; exit 4; }
  sleep 20
done
echo "SHADOW_STILL_RUNNING: rerun the same command later to reattach or print the result (8768 is untouched)"
exit 5
