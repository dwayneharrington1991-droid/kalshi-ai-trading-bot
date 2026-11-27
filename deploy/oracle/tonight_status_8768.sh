#!/usr/bin/env bash
# READ-ONLY status for tonight's 8768 decision (bounded, low priority).
#
#  1. Shadow research worker: progressing, starved (waiting for CPU) or
#     blocked (waiting on disk)? Two samples 15 s apart of its CPU time,
#     run-queue wait (/proc/<pid>/schedstat), state and bytes read.
#  2. CPU share of each service over 15 s (8765, 8767, 8768, research).
#  3. Why the previous guarded deployment rolled back (soak.json, gate lines).
#  4. 8768 now: build, health, readiness, latency percentiles.
#  5. Seconds to the next 15-minute opening; disk, memory, load.
# Never signals, renices, restarts, writes or deletes anything.
set -uo pipefail
ROOT=${STATUS_RESEARCH_ROOT:-/home/opc/kalshi-research}
JOBS=${STATUS_JOBS:-/home/opc/.kalshi-deploy/preview-8768/jobs}
PY=${STATUS_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
renice -n 19 -p $$ >/dev/null 2>&1 || true
section() { printf '\n===== %s =====\n' "$*"; }
pid_of() { ( sudo -n ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }
ticks() { awk '{print $14 + $15}' "/proc/$1/stat" 2>/dev/null || echo 0; }
HZ=$(getconf CLK_TCK)

section "1. SHADOW RESEARCH WORKER"
W=""
for f in "$ROOT"/*/pid; do
  [[ -f $f ]] || continue
  p=$(cat "$f")
  if kill -0 "$p" 2>/dev/null; then
    W=$(pgrep -P "$p" -f 'src.signals_display' | head -n1); W=${W:-$p}
    echo "run: $(dirname "$f")  launcher pid $p  worker pid $W  nice $(ps -o ni= -p "$W")  elapsed $(ps -o etime= -p "$W")"
    break
  fi
done
if [[ -n $W ]]; then
  read -r c1 q1 _ <"/proc/$W/schedstat"; r1=$(awk '/^read_bytes/{print $2}' "/proc/$W/io" 2>/dev/null || echo 0)
  states=""; for _ in 1 2 3 4 5; do states+=$(awk '{print $3}' "/proc/$W/stat"); sleep 3; done
  read -r c2 q2 _ <"/proc/$W/schedstat"; r2=$(awk '/^read_bytes/{print $2}' "/proc/$W/io" 2>/dev/null || echo 0)
  cpu=$(( (c2 - c1) / 1000000 )); wait=$(( (q2 - q1) / 1000000 ))
  echo "over 15 s: on-CPU ${cpu} ms, waiting for CPU ${wait} ms, disk read $(( (r2 - r1) / 1048576 )) MiB, states ${states}"
  echo "total CPU so far: $(( $(ticks "$W") / HZ )) s; total disk read $(( r2 / 1048576 )) MiB"
  if [[ $states == *D*D*D* ]]; then echo "VERDICT: BLOCKED_ON_DISK (mostly uninterruptible I/O)"
  elif (( wait > 4 * (cpu + 1) )); then echo "VERDICT: STARVED (runnable but waiting for CPU behind higher-priority services)"
  elif (( cpu > 0 || r2 > r1 )); then echo "VERDICT: PROGRESSING"
  else echo "VERDICT: IDLE_OR_SLEEPING (state ${states})"; fi
  grep -E '^SHADOW:' "$(dirname "$f")/log" 2>/dev/null | tail -n 3
else
  echo "no shadow research evaluation is running"
  for d in "$ROOT"/*/; do [[ -f $d/report.json ]] && echo "finished report: $d/report.json"; done
fi

section "2. CPU SHARE OVER 15 s"
declare -A T0 N
for port in 8765 8767 8768; do p=$(pid_of $port); [[ -n $p ]] && { N[$port]=$p; T0[$port]=$(ticks "$p"); }; done
[[ -n $W ]] && { N[research]=$W; T0[research]=$(ticks "$W"); }
sleep 15
for k in "${!N[@]}"; do
  echo "$k (pid ${N[$k]}, nice $(ps -o ni= -p "${N[$k]}")): $(( ( $(ticks "${N[$k]}") - T0[$k] ) * 100 / (15 * HZ) ))% of one core"
done
echo "cores: $(nproc)  load: $(cut -d' ' -f1-3 /proc/loadavg)"

section "3. PREVIOUS GUARDED DEPLOYMENTS (newest 2)"
for job in $(ls -1dt "$JOBS"/*/ 2>/dev/null | head -n 2); do
  echo "--- $(basename "$job")  $(stat -c %y "$job" | cut -c1-19)"
  [[ -f $job/soak.json ]] && "$PY" -I -c 'import json,sys;d=json.load(open(sys.argv[1]));print("  soak:",d.get("result"),"|",d.get("reason"),"| samples",d.get("samples"))' "$job/soak.json"
  grep -hE 'GATE FAIL|ROLLED_BACK|DEPLOY_RESULT|soak passed|free storage' "$job/log" 2>/dev/null | tail -n 4 | cut -c1-260 | sed 's/^/  /'
done

section "4. 8768 NOW"
timeout 40 "$PY" -I - <<'PY' || echo "8768 probe failed"
import json, time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
o = build_opener(ProxyHandler({}))
def get(path):
    t = time.monotonic()
    try:
        with o.open("http://127.0.0.1:8768" + path, timeout=15) as r:
            return r.status, json.load(r), (time.monotonic() - t) * 1000
    except HTTPError as e:
        return e.code, json.load(e), (time.monotonic() - t) * 1000
    except Exception as e:
        return None, {"error": type(e).__name__}, (time.monotonic() - t) * 1000
c, h, ms = get("/healthz")
print(f"healthz HTTP {c} {h.get('status')} build {str(h.get('build_commit'))[:12]} signals_only {h.get('signals_only')} workers_healthy {(h.get('workers') or {}).get('healthy')} ({ms:.0f} ms)")
c, r, ms = get("/readyz")
print(f"readyz HTTP {c} {r.get('status')} stale {r.get('stale_reasons')}")
c, s, ms = get("/api/state?view=main")
mh, rt = s.get("market_health") or {}, s.get("runtime") or {}
print(f"/api/state {ms:.0f} ms; quote age {mh.get('quote_age_seconds')} s; reference age {mh.get('reference_age_seconds')} s")
for k, v in (rt.get("latency_percentiles") or {}).items():
    if isinstance(v, dict) and k in ("prediction_calculation", "total_processing", "publish_after_collection", "http_main_state"):
        print(f"  {k}: p50 {v.get('p50_ms')} p95 {v.get('p95_ms')} p99 {v.get('p99_ms')} ms ({v.get('samples')} samples)")
PY

section "5. TIMING AND HEADROOM"
now=$(date -u +%s); next=$(( (now / 900 + 1) * 900 ))
echo "UTC $(date -u +%H:%M:%S); next 15-minute opening in $(( next - now )) s ($(date -u -d @$next +%H:%M))"
awk '/^MemAvailable:|^SwapFree:/{printf "%s %d MiB\n", $1, $2/1024}' /proc/meminfo
df -h /home/opc | tail -n 1
echo
echo "TONIGHT_STATUS_COMPLETE (read-only; nothing was changed)"
