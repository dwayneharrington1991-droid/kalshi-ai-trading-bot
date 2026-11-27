#!/usr/bin/env bash
# READ-ONLY evidence collection for one failed preview-8768 deployment job.
#
#   collect_failed_job_evidence.sh <job id> <40-char commit>
#
# Prints the job's own record and full log, the failed preview's server log,
# host resources, and the CURRENT state of ports 8768/8765/8767. It never
# starts, stops or signals a process, never writes to the journal, never
# takes the deployment lock, and never prints environment variables (the
# job's private environment snapshot is listed by name only, never read).
# Lines that look like credentials are redacted defensively.
set -uo pipefail
umask 077
id=${1:-}; sha=${2:-}
[[ $id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { echo "invalid job id"; exit 2; }
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit"; exit 2; }
STATE_ROOT=${PREVIEW_STATE_ROOT:-/home/opc/.kalshi-deploy/preview-8768}
JOB=$STATE_ROOT/jobs/$id
SERVER_LOG=${PREVIEW_LOG_DIR:-/home/opc/kalshi-preview-upgrade/logs}/preview-8768-${sha:0:7}.log
PYTHON=${PREVIEW_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}

redact() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }
section() { printf '\n===== %s =====\n' "$*"; }

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"

section "JOB RECORD $id"
if [[ -d $JOB ]]; then
  for f in sha created_at run_id pid result; do
    [[ -f $JOB/$f ]] && printf '%s: %s\n' "$f" "$(tr '\n' ' ' <"$JOB/$f")"
  done
  [[ -f $JOB/sha && $(<"$JOB/sha") == "$sha" ]] && echo "commit binding: MATCH" || echo "commit binding: MISMATCH"
else
  echo "job directory not found"
fi

section "FULL JOB LOG"
[[ -f $JOB/log ]] && redact <"$JOB/log" || echo "no job log"

if [[ -f $JOB/run_id ]]; then
  run=$STATE_ROOT/runs/$(<"$JOB/run_id")
  section "RUN DIRECTORY (names only; snapshots are not read)"
  ls -la "$run" 2>&1
fi

section "FAILED PREVIEW SERVER LOG $SERVER_LOG"
if [[ -f $SERVER_LOG ]]; then
  ls -la "$SERVER_LOG"
  echo "--- error summary (counts by error/event type) ---"
  grep -oE '"(event|error_type|component)": "[^"]*"' "$SERVER_LOG" | sort | uniq -c | sort -rn | head -40
  echo "--- tracebacks (first 3, 40 lines each) ---"
  awk '/^Traceback/{n++; c=40} c>0 && n<=3 {print; c--}' "$SERVER_LOG" | redact
  echo "--- first 60 lines ---"
  head -n 60 "$SERVER_LOG" | redact
  echo "--- last 200 lines ---"
  tail -n 200 "$SERVER_LOG" | redact
else
  echo "server log not found"
fi

section "HOST RESOURCES"
df -h /home/opc 2>&1
free -m 2>&1
uptime
ls -la /home/opc/kalshi-preview-upgrade/logs/upgraded-live-8768.db* 2>&1

section "CURRENT LISTENERS 8768 8765 8767"
for port in 8768 8765 8767; do
  pid=$(ss -Hltnp "sport = :$port" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2)
  if [[ -n $pid ]]; then
    printf 'port %s pid %s started %s cwd %s\n' "$port" "$pid" \
      "$(ps -o lstart= -p "$pid")" "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)"
    printf '  argv: %s\n' "$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | redact)"
  else
    printf 'port %s: NO LISTENER\n' "$port"
  fi
done

section "CURRENT 8768 HEALTH AND READINESS"
"$PYTHON" - <<'PY' 2>&1
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
opener = build_opener(ProxyHandler({}))
for path in ("/healthz", "/readyz", "/api/candle-integrity"):
    try:
        with opener.open(f"http://127.0.0.1:8768{path}", timeout=5) as r:
            code, body = r.status, json.load(r)
    except HTTPError as exc:
        code, body = exc.code, {}
    except Exception as exc:
        code, body = None, {"error": type(exc).__name__}
    keep = {k: body.get(k) for k in ("status", "build_commit", "signals_only", "contract",
            "contract_quotes_fresh", "reference_fresh", "stale_reasons", "bars", "unique_bars",
            "missing_intervals", "duplicate_bars", "error") if k in body}
    if "workers" in body:
        keep["workers_healthy"] = (body.get("workers") or {}).get("healthy")
        keep["worker_reasons"] = (body.get("workers") or {}).get("reasons")
    print(path, "HTTP", code, json.dumps(keep, default=str)[:800])
PY
echo
echo "EVIDENCE_COLLECTION_COMPLETE (read-only; nothing was changed)"
