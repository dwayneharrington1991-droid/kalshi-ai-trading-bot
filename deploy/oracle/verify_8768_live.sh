#!/usr/bin/env bash
# READ-ONLY verification of the live 8768 bot after a deployment.
#   verify_8768_live.sh <expected-commit-sha>
# Prints build, health, readiness, signals-only, data freshness, latency,
# whether the old ports 8765/8767 are free, and whether phone alerts are
# armed (alert ledger beside the journal; private credentials file present).
# Never prints credentials; never changes anything. Exit 0 only if 8768 runs
# the expected build, HEALTHY, READY and signals-only.
set -uo pipefail
SHA=${1:-}
PY=${VERIFY_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
CFG=${VERIFY_ALERTS_FILE:-$HOME/.config/kalshi-8768-alerts.env}
pid_of() { ( sudo -n ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }
P=$(pid_of 8768); [[ -n $P ]] || { echo "LIVE_8768: NOT RUNNING"; exit 1; }
mapfile -d '' -t A <"/proc/$P/cmdline"
J=""; for ((i=0; i<${#A[@]}-1; i++)); do [[ ${A[i]} == --journal ]] && J=${A[i+1]}; done
[[ -n $J && $J != /* ]] && J=$(readlink -f "/proc/$P/cwd")/$J
for port in 8765 8767; do echo "old port $port: $([[ -n $(pid_of $port) ]] && echo 'STILL LISTENING' || echo 'free (retired)')"; done
if [[ -f $J.alerts.sqlite3 ]]; then
  echo "phone alerts: ARMED in the live process (ledger present; alerts sent so far: $("$PY" -I -c 'import sqlite3,sys;print(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True).execute("SELECT COUNT(*) FROM opening_alerts WHERE result=?",("SENT",)).fetchone()[0])' "$J.alerts.sqlite3" 2>/dev/null || echo '?'))"
else
  echo "phone alerts: NOT loaded by the live process (no ledger beside the journal)"
fi
if [[ -f $CFG ]]; then echo "SMS credentials file: present, mode $(stat -c %a "$CFG") (values not shown)"; else echo "SMS credentials file: MISSING"; fi
"$PY" -I - "$SHA" <<'PY'
import json, sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
o = build_opener(ProxyHandler({}))
def get(path):
    try:
        with o.open("http://127.0.0.1:8768" + path, timeout=15) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)
    except Exception as e:
        return None, {"error": type(e).__name__}
hc, h = get("/healthz"); rc, r = get("/readyz"); sc, s = get("/api/state?view=main")
mh, rt = s.get("market_health") or {}, s.get("runtime") or {}
build = h.get("build_commit")
print(f"build: {build}  health: {h.get('status')}  ready: {r.get('status')} {r.get('stale_reasons') or ''}  signals_only: {h.get('signals_only')}")
print(f"quote age: {mh.get('quote_age_seconds')} s  reference age: {mh.get('reference_age_seconds')} s  contract: {mh.get('contract')}")
for k in ("prediction_calculation", "publish_after_collection"):
    v = (rt.get("latency_percentiles") or {}).get(k) or {}
    print(f"{k}: p50 {v.get('p50_ms')} ms  p95 {v.get('p95_ms')} ms")
ok = build == sys.argv[1] and h.get("status") == "HEALTHY" and r.get("status") == "READY" and h.get("signals_only") is True
print("LIVE_8768:", "VERIFIED (expected build, HEALTHY, READY, signals-only)" if ok else "NOT VERIFIED")
sys.exit(0 if ok else 1)
PY
