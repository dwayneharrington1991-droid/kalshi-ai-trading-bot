#!/usr/bin/env bash
# READ-ONLY diagnosis of production 8765's stopped collector (bounded).
#
# Finds the 8765 process, reads its OWN stderr/stdout log (via /proc/<pid>/fd)
# for the thread traceback and the exact sqlite3.OperationalError message,
# reports data staleness (dashboard state and newest journal rows, read-only),
# and inspects the journal's files, open handles and kernel locks
# (/proc/locks) without opening any write transaction. Never signals,
# restarts, writes or deletes anything; credential-like text is redacted.
set -uo pipefail
PORT=${DIAG_PORT:-8765}
PY=${DIAG_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
R() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }
section() { printf '\n===== %s =====\n' "$*"; }

P=$( (sudo -n ss -Hltnp "sport = :$PORT" 2>/dev/null || ss -Hltnp "sport = :$PORT") | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2)
[[ -n $P ]] || { echo "no listener on $PORT"; exit 2; }
section "PROCESS"
ps -o pid,lstart,etime,stat -p "$P"
CWD=$(readlink -f "/proc/$P/cwd")
mapfile -d '' -t A <"/proc/$P/cmdline"
J=""; B=""
for ((i=0; i<${#A[@]}-1; i++)); do
  [[ ${A[i]} == --journal ]] && J=${A[i+1]}
  [[ ${A[i]} == --build-commit ]] && B=${A[i+1]}
done
[[ -n $J && $J != /* ]] && J=$CWD/$J
echo "cwd: $CWD"; echo "build: ${B:-unknown}"; echo "journal: $J"
echo "git HEAD of cwd: $(timeout 5 git -C "$CWD" rev-parse HEAD 2>/dev/null || echo n/a)"

section "WORKERS (/healthz)"
timeout 10 "$PY" -P - "$PORT" <<'PY' || echo "healthz unavailable"
import json, sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
try:
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=8) as r:
        c, h = r.status, json.load(r)
except HTTPError as e:
    c, h = e.code, json.load(e)
print("HTTP", c, "status", h.get("status"), "state_age_seconds", h.get("state_age_seconds"))
for name, w in ((h.get("workers") or {}).get("workers") or {}).items():
    print(f"  {name}: " + json.dumps({k: w.get(k) for k in ("status", "thread_alive", "cycles", "last_error_type",
          "last_completed_at", "last_started_at", "seconds_since_completion")}, default=str))
print("  reasons:", (h.get("workers") or {}).get("reasons"))
PY

section "LOG: EXACT ERROR"
for fd in 1 2; do echo "fd$fd -> $(readlink "/proc/$P/fd/$fd")"; done
LOG=$(readlink "/proc/$P/fd/2")
if [[ -f $LOG ]]; then
  ls -la "$LOG"
  for pat in 'database is locked' 'database or disk is full' 'disk I/O error' 'malformed' 'unable to open' 'readonly database' 'OperationalError'; do
    printf '  %-26s %s\n' "$pat:" "$(grep -c -- "$pat" "$LOG")"
  done
  echo "--- collector thread tracebacks (last 2, with context)"
  grep -n -B4 -A18 -E 'Exception in thread .*collect|in collect$' "$LOG" | tail -n 90 | R
  echo "--- last 5 OperationalError lines with line numbers"
  grep -n 'OperationalError' "$LOG" | tail -n 5 | cut -c1-300 | R
  echo "--- log lines timestamped 2026-11-12 mentioning collector/error (last 15)"
  grep -n '2026-11-12' "$LOG" | grep -iE 'collector|error|stopped' | tail -n 15 | cut -c1-300 | R
else
  echo "stderr is not a regular file ($LOG); the traceback is not in a readable log"
fi

section "STALENESS"
timeout 15 "$PY" -P - "$PORT" "$J" <<'PY' || echo "staleness probe unavailable"
import json, sqlite3, sys
from urllib.request import ProxyHandler, build_opener
port, path = sys.argv[1], sys.argv[2]
try:
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/api/state?view=main", timeout=10) as r:
        s = json.load(r)
    mh = s.get("market_health") or {}
    print("dashboard now:", s.get("now"), "| contract:", mh.get("contract"), "| quote age:", mh.get("quote_age_seconds"),
          "| reasons:", mh.get("contract_quote_reasons"), mh.get("reference_reasons"))
except Exception as e:
    print("dashboard state unavailable:", type(e).__name__)
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
print("newest candle start (UTC epoch):", db.execute("SELECT MAX(start) FROM btc_candles").fetchone()[0])
print("newest prediction row:", db.execute("SELECT timestamp FROM predictions ORDER BY id DESC LIMIT 1").fetchone())
print("read-only open and indexed queries: OK")
PY

section "JOURNAL FILES, HOLDERS, LOCKS (read-only)"
ls -la "$J" "$J-wal" "$J-shm" "$J.dashboard.lock" 2>&1
echo "header: $(head -c 15 "$J")"
for h in $(timeout 30 find /proc/[0-9]*/fd -maxdepth 1 -lname "$J*" 2>/dev/null | cut -d/ -f3 | sort -u); do
  echo "holder pid $h ($(cat "/proc/$h/comm" 2>/dev/null))"
done
for f in "$J" "$J-shm" "$J-wal" "$J.dashboard.lock"; do
  [[ -e $f ]] || continue
  ino=$(stat -c %i "$f")
  echo "kernel locks on $(basename "$f") (inode $ino):"
  grep -E ":$ino " /proc/locks | sed 's/^/   /' || echo "   none"
done
section "DISK NOW"
df -h "$J" | tail -n 1
echo
echo "PROD_DIAG_COMPLETE (read-only; nothing was changed)"
