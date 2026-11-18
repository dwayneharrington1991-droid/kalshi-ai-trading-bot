#!/usr/bin/env bash
# READ-ONLY Oracle status, readiness and accuracy diagnostic (bounded ~3 min).
#
# Run from an extracted copy of the code (PYTHONPATH = that copy). Reports:
#   1. host CPU/memory/disk and every dashboard listener (8765/8767/8768/8769):
#      PID, uptime, CPU share, RSS, working directory, --build-commit, mode,
#      journal file name (never environment or other argument values);
#   2. per dashboard: /healthz, /readyz, /api/candle-integrity and the opening
#      the dashboard is showing, plus the accuracy figures it DISPLAYS
#      (/api/opening-scoreboard, /api/learning-summary);
#   3. per journal (opened read-only, mode=ro): the project's own verified
#      opening scoreboard, learning summary and promotion-gate coverage
#      (dataset_coverage), the last 16 settled opening calls with what the
#      model recorded at the time, and why recent openings were unavailable;
#   4. preview-8768 deployment jobs and the latest job's log tail.
# Never starts, stops or signals a process; never writes to any journal;
# never prints environment variables, credentials or tokens.
set -uo pipefail
umask 077
PYTHON=${STATUS_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STATE_ROOT=${STATUS_STATE_ROOT:-/home/opc/.kalshi-deploy/preview-8768}
PORTS=${STATUS_PORTS:-8765 8767 8768 8769}
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true
section() { printf '\n===== %s =====\n' "$*"; }
redact() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ) code=$(cat "$CODE_ROOT/.diag_commit" 2>/dev/null || echo unknown)"
echo "cpus: $(nproc)  load: $(cut -d' ' -f1-3 /proc/loadavg)"
free -m | sed -n '1,3p'
df -h /home/opc 2>/dev/null | tail -1

PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 timeout -k 5 170 "$PYTHON" -P - "$PORTS" <<'PY'
import json, os, re, subprocess, sys, time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

ports = sys.argv[1].split()
opener = build_opener(ProxyHandler({}))
tick, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
boot = time.time() - float(open("/proc/uptime").read().split()[0])

def section(t): print(f"\n===== {t} =====", flush=True)
def listener(port):
    for cmd in (["sudo", "-n", "ss", "-H", "-ltnp", f"sport = :{port}"], ["ss", "-H", "-ltnp", f"sport = :{port}"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        if out.returncode == 0 and (out.stdout.strip() or cmd[0] == "ss"):
            pids = sorted(set(re.findall(r"pid=(\d+)", out.stdout)))
            return pids[0] if len(pids) == 1 else None
    return None
def stat(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return int(f[11]) + int(f[12]), int(f[19]), int(f[21])
def get(port, path, timeout=8):
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, None
    except Exception as e:
        return None, {"error": type(e).__name__}
def pick(d, keys):
    return {k: d.get(k) for k in keys if isinstance(d, dict) and k in d}

section("DASHBOARD PROCESSES")
procs = {}
for port in ports:
    pid = listener(port)
    if not pid:
        print(f"port {port}: no single listener"); continue
    argv = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0")
    val = lambda f: argv[argv.index(f) + 1] if f in argv[:-1] else None
    cwd = os.readlink(f"/proc/{pid}/cwd")
    journal = val("--journal")
    if journal and not journal.startswith("/"):
        journal = os.path.join(cwd, journal)
    procs[port] = {"pid": pid, "journal": journal}
    c0, start, rss = stat(pid); procs[port]["c0"] = c0
    print(f"port {port}: pid {pid} uptime {(time.time() - boot - start / tick) / 3600:.1f} h "
          f"rss {rss * page // 1048576} MiB cwd {cwd} build {val('--build-commit')} mode {val('--mode')} "
          f"journal {os.path.basename(journal) if journal else None} "
          f"account_flags {any(a in argv for a in ('--account-readonly', '--private-dashboard'))}")
t0 = time.monotonic(); time.sleep(10); dt = time.monotonic() - t0
for port, p in procs.items():
    try:
        print(f"port {port}: CPU {100 * (stat(p['pid'])[0] - p['c0']) / tick / dt:.1f}% of one core (10 s)")
    except OSError:
        print(f"port {port}: exited during sampling")

for port in procs:
    section(f"DASHBOARD {port}: HEALTH, READINESS, CANDLES, OPENING, DISPLAYED ACCURACY")
    hc, h = get(port, "/healthz"); print("healthz", hc, json.dumps(pick(h, ("status", "build_commit", "signals_only", "state_age_seconds")) | {"workers_healthy": (h or {}).get("workers", {}).get("healthy") if isinstance(h, dict) else None}))
    rc, r = get(port, "/readyz"); print("readyz", rc, json.dumps(pick(r, ("status", "contract", "contract_quotes_fresh", "reference_fresh", "stale_reasons"))))
    cc, c = get(port, "/api/candle-integrity"); print("candles", cc, json.dumps(pick(c, ("status", "bars", "unique_bars", "missing_intervals", "duplicate_bars", "reason")), default=str)[:600])
    sc, s = get(port, "/api/state?view=main", timeout=12)
    if isinstance(s, dict):
        mh, rt = s.get("market_health") or {}, s.get("runtime") or {}
        print("freshness", json.dumps(pick(mh, ("contract", "quote_age_seconds", "reference_age_seconds", "seconds_remaining", "contract_quote_reasons", "reference_reasons"))))
        lat = rt.get("latency_percentiles") or {}
        print("latency", json.dumps({k: pick(lat.get(k) or {}, ("p50_ms", "p95_ms", "p99_ms", "samples")) for k in ("publish_after_collection", "prediction_calculation", "total_processing")}))
        eng = s.get("prediction_engine") or {}
        op = eng.get("initial") or {}
        print("opening", json.dumps(pick(op, ("ticker", "status", "direction", "up", "reason", "opening_delay_seconds", "model")) | {"confidence": (op.get("confidence") or {}).get("status")}, default=str)[:900])
        print("scalp", json.dumps(pick(s, ("scalp", "scalp_explanation", "stale", "direction")), default=str)[:400])
    else:
        print("state", sc, s)
    for path in ("/api/opening-scoreboard", "/api/learning-summary"):
        code, body = get(port, path, timeout=15)
        if isinstance(body, dict) and body.get("status") == "CALCULATING":
            time.sleep(6)                                   # cached report warming up: one retry
            code, body = get(port, path, timeout=15)
        print(path, code, json.dumps(body, default=str)[:1500])

section("JOURNALS (read-only, project code): VERIFIED ACCURACY, COVERAGE, PROMOTION GATES, RECENT CALLS")
from src.signals_display.monitor import Journal
from src.signals_display.opening_models import dataset_coverage
seen = set()
for port, p in procs.items():
    path = p["journal"]
    if not path or path in seen or not os.path.exists(path):
        continue
    seen.add(path)
    print(f"\n--- journal of port {port}: {os.path.basename(path)} ({os.path.getsize(path) / 1e9:.2f} GB)")
    j = Journal.__new__(Journal); j.path = path          # no schema setup: read-only connections only
    try:
        print("verified_scoreboard", json.dumps(j.verified_opening_scoreboard(), default=str))
    except Exception as e:
        print("verified_scoreboard ERROR", type(e).__name__, str(e)[:200])
    try:
        q = j.quick_learning_summary()
        print("learning_summary", json.dumps({k: v for k, v in q.items() if not isinstance(v, (list, dict)) or k == "current_policy"}, default=str)[:1500])
    except Exception as e:
        print("learning_summary ERROR", type(e).__name__, str(e)[:200])
    try:
        with j.read() as db:
            cov = dataset_coverage(db)
        print("promotion_coverage", json.dumps({k: cov.get(k) for k in ("official_settled_contracts", "eligible_frozen_openings", "excluded_contracts", "exclusion_reasons", "eligibility_rate", "minimum_for_first_shadow_evaluation", "remaining_to_first_shadow_evaluation", "newest_eligible_contract")}, default=str))
    except Exception as e:
        print("promotion_coverage ERROR", type(e).__name__, str(e)[:200])
    try:
        with j.read() as db:
            rows = db.execute(
                """SELECT b.ticker,b.generated_at,b.up,b.payload,f.outcome FROM btc_engine_opening b
                   LEFT JOIN forecast_outcomes f ON f.ticker=b.ticker AND f.origin='official'
                   WHERE b.mode='PUBLIC MARKET DATA' ORDER BY b.generated_at DESC LIMIT 200""").fetchall()
        settled = [r for r in rows if r[4] in (0, 1)]
        print(f"recent settled openings (newest first, of last {len(rows)} openings):")
        shown = 0
        for ticker, gen, up, payload, outcome in settled:
            try: d = json.loads(payload) if payload else {}
            except Exception: d = {}
            direction = d.get("direction")
            call = direction in ("UP", "DOWN")
            result = ("WIN" if (direction == "UP") == bool(outcome) else "LOSS") if call else "no call"
            dq, fz = d.get("data_quality") or {}, d.get("frozen_features") or {}
            print("  " + json.dumps({"ticker": ticker, "generated_at": gen, "direction": direction, "up": up,
                  "outcome_up": outcome, "result": result, "confidence": (d.get("confidence") or {}).get("status"),
                  "threshold": (d.get("decision_policy") or {}).get("threshold"), "model": d.get("model"),
                  "delay_s": d.get("opening_delay_seconds"), "bars": dq.get("closed_history_bars"),
                  "market_implied_up": (d.get("market_comparison") or {}).get("market_implied_up"),
                  "regime": d.get("regime"), "reason": str(d.get("reason"))[:120]}, default=str))
            shown += 1
            if shown >= 16: break
        last96 = rows[:96]
        unavailable = {}
        for ticker, gen, up, payload, outcome in last96:
            if up is None:
                try: reason = str((json.loads(payload) if payload else {}).get("reason"))[:90]
                except Exception: reason = "unparseable"
                unavailable[reason] = unavailable.get(reason, 0) + 1
        calls = sum(1 for r in last96 if r[2] is not None)
        print(f"last {len(last96)} openings: with probability {calls}, unavailable {len(last96) - calls}")
        for reason, n in sorted(unavailable.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  unavailable x{n}: {reason}")
        if rows:
            print("payload keys of newest opening:", sorted((json.loads(rows[0][3]) if rows[0][3] else {}).keys())[:60])
    except Exception as e:
        print("recent openings ERROR", type(e).__name__, str(e)[:200])
PY
echo "python section exit: $?"

section "PREVIEW-8768 DEPLOYMENT JOBS"
if [[ -d $STATE_ROOT/jobs ]]; then
  for job in $(ls -1t "$STATE_ROOT/jobs" 2>/dev/null | head -n 5); do
    d=$STATE_ROOT/jobs/$job
    printf '%s sha=%s created=%s %s\n' "$job" "$(cut -c1-12 "$d/sha" 2>/dev/null)" "$(cat "$d/created_at" 2>/dev/null)" \
      "$(grep -h '^DEPLOY_RESULT=' "$d/result" 2>/dev/null)"
  done
  latest=$(ls -1t "$STATE_ROOT/jobs" 2>/dev/null | head -n 1)
  [[ -n $latest ]] && { echo "--- latest job log tail ($latest)"; tail -n 25 "$STATE_ROOT/jobs/$latest/log" 2>/dev/null | redact; }
else
  echo "no job directory"
fi
echo
echo "ORACLE_STATUS_COMPLETE (read-only; nothing was changed)"
