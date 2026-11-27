#!/usr/bin/env bash
# READ-ONLY verification of the live 8768 bot after a deployment.
#   verify_8768_live.sh <expected-commit-sha> [--require-telegram]
# Prints build, health, readiness, signals-only, data freshness, latency,
# whether the old ports 8765/8767 are free, and whether phone alerts are
# armed (alert ledger beside the journal; private credentials file present).
# Never prints credentials; never changes anything. Exit 0 only if 8768 runs
# the expected build, HEALTHY, READY, signals-only, with fresh quote and
# reference data (<= 5 s in one of up to 6 samples, 5 s apart), and, with
# --require-telegram, a private credentials file holding both Telegram keys.
set -uo pipefail
SHA=${1:-}
REQUIRE_TELEGRAM=0; [[ ${2:-} == --require-telegram ]] && REQUIRE_TELEGRAM=1
PY=${VERIFY_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
PORT=${VERIFY_PORT:-8768}; OLD_PORTS=${VERIFY_OLD_PORTS:-8765 8767}; RETRY_SLEEP=${VERIFY_RETRY_SLEEP:-5}   # overrides: local tests only
CFG=${VERIFY_ALERTS_FILE:-$HOME/.config/kalshi-8768-alerts.env}
pid_of() { ( sudo -n ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }
P=$(pid_of "$PORT"); [[ -n $P ]] || { echo "LIVE_8768: NOT RUNNING"; exit 1; }
mapfile -d '' -t A <"/proc/$P/cmdline"
J=""; for ((i=0; i<${#A[@]}-1; i++)); do [[ ${A[i]} == --journal ]] && J=${A[i+1]}; done
[[ -n $J && $J != /* ]] && J=$(readlink -f "/proc/$P/cwd")/$J
for port in $OLD_PORTS; do echo "old port $port: $([[ -n $(pid_of $port) ]] && echo 'STILL LISTENING' || echo 'free (retired)')"; done
if [[ -f $J.alerts.sqlite3 ]]; then
  echo "phone alerts: ARMED in the live process (ledger present; alerts sent so far: $("$PY" -I -c 'import sqlite3,sys;print(sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True).execute("SELECT COUNT(*) FROM opening_alerts WHERE result=?",("SENT",)).fetchone()[0])' "$J.alerts.sqlite3" 2>/dev/null || echo '?'))"
else
  echo "phone alerts: NOT loaded by the live process (no ledger beside the journal)"
fi
TG=0
if [[ -f $CFG ]]; then
  mode=$(stat -c %a "$CFG")
  keys=$(grep -oE '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TWILIO_ACCOUNT_SID|TWILIO_AUTH_TOKEN|TWILIO_FROM|ALERT_SMS_TO)=.' "$CFG" | cut -d= -f1 | sort -u | tr '\n' ' ')
  echo "alert credentials file: present, mode $mode, keys set: ${keys:-none} (values not shown)"
  [[ $mode == 600 && $keys == *TELEGRAM_BOT_TOKEN* && $keys == *TELEGRAM_CHAT_ID* ]] && TG=1
else
  echo "alert credentials file: MISSING"
fi
echo "telegram: $([[ $TG == 1 ]] && echo 'CONFIGURED (private file, both keys)' || echo 'NOT configured')"
"$PY" -I - "$SHA" "$REQUIRE_TELEGRAM" "$TG" "$PORT" "$RETRY_SLEEP" <<'PY'
import json, sys, time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
o = build_opener(ProxyHandler({}))
def get(path):
    try:
        with o.open(f"http://127.0.0.1:{sys.argv[4]}" + path, timeout=15) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)
    except Exception as e:
        return None, {"error": type(e).__name__}
num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
fresh = False
for attempt in range(6):
    hc, h = get("/healthz"); rc, r = get("/readyz"); sc, s = get("/api/state?view=main")
    mh, rt = s.get("market_health") or {}, s.get("runtime") or {}
    q, ref = mh.get("quote_age_seconds"), mh.get("reference_age_seconds")
    fresh = num(q) and num(ref) and 0 <= q <= 5 and 0 <= ref <= 5
    if fresh and r.get("status") == "READY":
        break
    time.sleep(float(sys.argv[5]))
build = h.get("build_commit")
print(f"build: {build}  health: {h.get('status')}  ready: {r.get('status')} {r.get('stale_reasons') or ''}  signals_only: {h.get('signals_only')}")
print(f"quote age: {q} s  reference age: {ref} s  fresh: {fresh}  contract: {mh.get('contract')}")
for k in ("prediction_calculation", "publish_after_collection"):
    v = (rt.get("latency_percentiles") or {}).get(k) or {}
    print(f"{k}: p50 {v.get('p50_ms')} ms  p95 {v.get('p95_ms')} ms")
checks = {"exact build": build == sys.argv[1], "HEALTHY": h.get("status") == "HEALTHY", "READY": r.get("status") == "READY",
          "signals-only": h.get("signals_only") is True, "fresh data": bool(fresh)}
if sys.argv[2] == "1":
    checks["telegram configured"] = sys.argv[3] == "1"
failed = [k for k, v in checks.items() if not v]
print("LIVE_8768:", "VERIFIED (" + ", ".join(checks) + ")" if not failed else "NOT VERIFIED (failed: " + ", ".join(failed) + ")")
sys.exit(0 if not failed else 1)
PY
