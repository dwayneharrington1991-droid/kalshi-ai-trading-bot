#!/usr/bin/env bash
# READ-ONLY Oracle status, readiness and accuracy diagnostic (v2, step-bounded).
#
# Run from an extracted copy of the code. Every step runs as its own process
# under its own timeout, so a slow step prints "STEP TIMED OUT" and the rest
# still run. Journal analysis covers ONLY preview 8768's journal (the source
# of the displayed opening scoreboard) using indexed/small-table queries.
#
#   A  host + dashboard processes (PID, uptime, CPU share, RSS, build, journal)
#   B  per dashboard: health (with worker reasons), readiness, candles,
#      freshness, latency, current opening, scalp, displayed accuracy
#   C  who uses each port: established clients sampled 3 times, client process
#   D  8768 journal: every verified directional call (all recorded scalar
#      fields; losses in full), WAIT/unavailable reasons, promotion coverage
#   E  disk usage of known large files and folders (sizes only)
#   F  deployment preflight verdict (preview_gates_8768.py preflight)
#   G  8769 retirement check (approved retire_service_8769.sh, `check` mode)
#   H  preview-8768 deployment jobs
# Never starts, stops or signals a process; never writes to any journal;
# never prints environment variables, credentials or tokens; deletes nothing
# except its own temporary output.
set -uo pipefail
umask 077
PYTHON=${STATUS_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STATE_ROOT=${STATUS_STATE_ROOT:-/home/opc/.kalshi-deploy/preview-8768}
export STATUS_PORTS=${STATUS_PORTS:-8765 8767 8768 8769}
export STATUS_JOURNAL_PORT=${STATUS_JOURNAL_PORT:-8768}
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true
section() { printf '\n===== %s =====\n' "$*"; }
redact() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }
step() {  # step <seconds> <python step name>
  local secs=$1 name=$2 rc
  PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 timeout -k 3 "$secs" "$PYTHON" -P "$CODE_ROOT/deploy/oracle/oracle_status_steps.py" "$name"
  rc=$?
  (( rc == 124 || rc == 137 )) && echo "STEP $name TIMED OUT after ${secs}s (other steps continue)"
  (( rc != 0 && rc != 124 && rc != 137 )) && echo "STEP $name exited $rc"
  return 0
}

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ) code=$(cat "$CODE_ROOT/.diag_commit" 2>/dev/null || echo unknown)"
echo "cpus: $(nproc)  load: $(cut -d' ' -f1-3 /proc/loadavg)"
free -m | sed -n '1,3p'

section "A. DASHBOARD PROCESSES";            step 40 processes
section "B. DASHBOARD HEALTH AND DATA";      step 120 dashboards
section "C. WHO USES EACH PORT";             step 30 clients
section "D. PREVIEW 8768 JOURNAL: CALLS, LOSSES, COVERAGE"; step 150 journal

section "E. DISK USAGE (sizes only; nothing deleted)"
df -h /home/opc 2>/dev/null
timeout 30 find /home/opc -xdev -maxdepth 4 \( -name '*.db' -o -name '*.db-wal' \) -size +100M \
  -printf '%s\t%TY-%Tm-%Td\t%p\n' 2>/dev/null | sort -rn | awk -F'\t' '{printf "%8.2f GB  %s  %s\n", $1/1e9, $2, $3}' | head -n 15 \
  || echo "journal listing unavailable or timed out"
echo "--- backups (per entry)"
timeout 30 du -sh /home/opc/kalshi-backups/* 2>/dev/null | sort -rh | head -n 10 || echo "backup sizing unavailable or timed out"
echo "--- largest logs (>50 MB)"
timeout 30 find /home/opc -xdev -maxdepth 4 -name '*.log*' -size +50M -printf '%s\t%p\n' 2>/dev/null \
  | sort -rn | awk -F'\t' '{printf "%8.2f GB  %s\n", $1/1e9, $2}' | head -n 10 || echo "log listing unavailable or timed out"
echo "--- research and deployment state"
timeout 30 du -sh /home/opc/kalshi-research /home/opc/.kalshi-deploy /home/opc/kalshi-preview-repair-* 2>/dev/null | head -n 12 \
  || echo "state sizing unavailable or timed out"

section "F. DEPLOYMENT PREFLIGHT VERDICT (read-only)"
pre=$CODE_ROOT/.preflight.json          # inside the temporary code copy
env -u PREVIEW_GATES_TEST_OVERRIDES -u PREVIEW_TEST_INSTALLER timeout -k 3 75 "$PYTHON" -P "$CODE_ROOT/deploy/oracle/preview_gates_8768.py" preflight "$pre" \
  || echo "preflight would BLOCK a deployment now (reasons above) or timed out"

section "G. 8769 RETIREMENT CHECK (approved script, check mode only)"
timeout -k 3 120 bash "$CODE_ROOT/deploy/oracle/retire_service_8769.sh" check 2>&1 | redact
echo "retirement check exit: ${PIPESTATUS[0]} (0 = all gates passed, 2 = blocked; nothing changed)"

section "H. PREVIEW-8768 DEPLOYMENT JOBS"
if [[ -d $STATE_ROOT/jobs ]]; then
  for job in $(ls -1t "$STATE_ROOT/jobs" 2>/dev/null | head -n 5); do
    d=$STATE_ROOT/jobs/$job
    printf '%s sha=%s created=%s %s\n' "$job" "$(cut -c1-12 "$d/sha" 2>/dev/null)" "$(cat "$d/created_at" 2>/dev/null)" \
      "$(grep -h '^DEPLOY_RESULT=' "$d/result" 2>/dev/null)"
  done
else
  echo "no job directory"
fi
echo
echo "ORACLE_STATUS_COMPLETE (read-only; nothing was changed)"
