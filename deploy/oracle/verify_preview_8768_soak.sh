#!/usr/bin/env bash
# READ-ONLY post-deployment soak gate for preview 8768 (Phase 5).
#
#   verify_preview_8768_soak.sh <expected build sha> <minutes 5..30> [protected pid 8765]
#
# Samples preview 8768 every 10 s (GET /healthz, /readyz, /api/state?view=main
# on localhost only) and enforces, over the whole window:
#   * every sample: HTTP 200 HEALTHY, workers healthy, signals_only, build ==
#     expected sha; /readyz READY with fresh quotes and reference;
#   * quote and reference age <= 5 s in >= 95% of samples, and never > 10 s;
#   * server-reported publish-after-collection p95 <= 3000 ms, p99 <= 4500 ms;
#   * prediction-calculation p50 <= 1020 ms (60% of the measured 1698 ms);
#   * if a protected 8765 PID is given, the 8765 listener PID never changes.
# Exit 0 = PASS, 4 = FAIL (roll back), 2 = usage/tooling error. Never changes
# anything; never contacts 8765 (only reads its listener PID from ss).
set -Euo pipefail
sha=${1:-}; minutes=${2:-}; protected_pid=${3:-}
PORT=${SOAK_PORT:-8768}
PROTECTED_PORT=${SOAK_PROTECTED_PORT:-8765}
INTERVAL=${SOAK_INTERVAL:-10}
PYTHON=${SOAK_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
[[ $sha =~ ^[0-9a-f]{7,40}$ ]] || { echo "usage: $0 <sha> <minutes 5..30> [8765 pid]"; exit 2; }
[[ $minutes =~ ^[0-9]+$ ]] && (( minutes >= ${SOAK_MIN_MINUTES:-5} && minutes <= 30 )) || { echo "minutes must be 5..30"; exit 2; }
[[ -z $protected_pid || $protected_pid =~ ^[0-9]+$ ]] || { echo "protected pid must be numeric"; exit 2; }
[[ -x $PYTHON ]] || { echo "python missing: $PYTHON"; exit 2; }
command -v ss >/dev/null || { echo "ss missing"; exit 2; }

exec "$PYTHON" - "$sha" "$minutes" "$protected_pid" "$PORT" "$PROTECTED_PORT" "$INTERVAL" "${SOAK_TEST_SECONDS:-}" <<'PY'
import json, re, subprocess, sys, time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

sha, minutes, protected_pid, port, protected_port, interval, test_seconds = sys.argv[1:8]
minutes, interval = int(minutes), float(interval)
window = float(test_seconds) if test_seconds else minutes * 60   # override: local tests only
opener = build_opener(ProxyHandler({}))

def get(path):
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {}
    except Exception as e:
        return None, {"error": type(e).__name__}

def listener(p):
    out = subprocess.run(["ss", "-Hltnp", f"sport = :{p}"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        return "SS_FAILED"
    found = sorted(set(re.findall(r"pid=(\d+)", out.stdout)))
    return ",".join(found)

failures, samples, ages = [], 0, []
last_runtime = {}
deadline = time.monotonic() + window
while time.monotonic() < deadline:
    samples += 1
    hc, h = get("/healthz"); rc, r = get("/readyz"); sc, s = get("/api/state?view=main")
    stamp = time.strftime("%H:%M:%S", time.gmtime())
    if not (hc == 200 and h.get("status") == "HEALTHY" and h.get("signals_only") is True
            and (h.get("workers") or {}).get("healthy") is True and str(h.get("build_commit", "")).startswith(sha[:7])):
        failures.append(f"{stamp} healthz not HEALTHY/signals-only/expected build: {hc} {json.dumps(h)[:160]}")
    if not (rc == 200 and r.get("status") == "READY" and r.get("contract_quotes_fresh") is True
            and r.get("reference_fresh") is True):
        failures.append(f"{stamp} readyz not READY: {rc} {json.dumps(r)[:160]}")
    mh = (s or {}).get("market_health") or {}
    q, ref = mh.get("quote_age_seconds"), mh.get("reference_age_seconds")
    ages.append((q, ref))
    if isinstance(s, dict) and s.get("runtime"):
        last_runtime = s["runtime"]
    if protected_pid and listener(protected_port) != protected_pid:
        failures.append(f"{stamp} protected {protected_port} listener changed or unreadable")
    print(f"{stamp} health={hc} ready={rc} quote_age={q} reference_age={ref} "
          f"processing_ms={(s or {}).get('runtime', {}).get('processing_ms')}", flush=True)
    time.sleep(max(0, min(interval, deadline - time.monotonic())))

def fresh(v):
    return isinstance(v, (int, float)) and 0 <= v <= 5
fresh_share = sum(1 for q, r in ages if fresh(q) and fresh(r)) / max(1, len(ages))
worst = max((max(q, r) for q, r in ages if isinstance(q, (int, float)) and isinstance(r, (int, float))), default=None)
if fresh_share < 0.95:
    failures.append(f"quote/reference fresh (<=5 s) in only {fresh_share:.0%} of samples (need >= 95%)")
if worst is None or worst > 10:
    failures.append(f"worst quote/reference age {worst} s (limit 10 s, must be measurable)")
lat = last_runtime.get("latency_percentiles") or {}
pub, pred = lat.get("publish_after_collection") or {}, lat.get("prediction_calculation") or {}
if not (isinstance(pub.get("p95_ms"), (int, float)) and pub["p95_ms"] <= 3000):
    failures.append(f"publish-after-collection p95 {pub.get('p95_ms')} ms (limit 3000)")
if not (isinstance(pub.get("p99_ms"), (int, float)) and pub["p99_ms"] <= 4500):
    failures.append(f"publish-after-collection p99 {pub.get('p99_ms')} ms (limit 4500)")
if not (isinstance(pred.get("p50_ms"), (int, float)) and pred["p50_ms"] <= 1020):
    failures.append(f"prediction-calculation p50 {pred.get('p50_ms')} ms (limit 1020)")
print(f"\nsamples={samples} fresh_share={fresh_share:.0%} worst_age={worst} "
      f"publish_p95={pub.get('p95_ms')} publish_p99={pub.get('p99_ms')} prediction_p50={pred.get('p50_ms')}")
if failures:
    print(f"SOAK FAIL ({len(failures)} finding(s)); roll back:")
    for f in failures[:25]:
        print("  - " + f)
    sys.exit(4)
print("SOAK PASS")
PY
