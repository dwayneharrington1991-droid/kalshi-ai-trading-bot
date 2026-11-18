"""Read-only steps for oracle_status_readonly.sh (each runs in its own process).

    oracle_status_steps.py processes|dashboards|clients|journal

Environment: STATUS_PORTS (space separated), STATUS_JOURNAL_PORT (default 8768).
Never writes, signals or prints environment values. Journals are opened only
through the project's read-only connection (SQLite mode=ro), without schema
setup.
"""
import json
import os
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

PORTS = os.environ.get("STATUS_PORTS", "8765 8767 8768 8769").split()
JOURNAL_PORT = os.environ.get("STATUS_JOURNAL_PORT", "8768")
OPENER = build_opener(ProxyHandler({}))
TICK, PAGE = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")


def out(*a):
    print(*a, flush=True)


def ss(*args):
    for cmd in (["sudo", "-n", "ss", *args], ["ss", *args]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except Exception:
            continue
        if r.returncode == 0 and (r.stdout.strip() or cmd[0] == "ss"):
            return r.stdout
    return ""


def listener(port):
    pids = sorted(set(re.findall(r"pid=(\d+)", ss("-H", "-ltnp", f"sport = :{port}"))))
    return pids[0] if len(pids) == 1 else None


def argv_of(pid):
    return open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0")


def journal_of(pid):
    argv = argv_of(pid)
    j = argv[argv.index("--journal") + 1] if "--journal" in argv[:-1] else None
    if j and not j.startswith("/"):
        j = os.path.join(os.readlink(f"/proc/{pid}/cwd"), j)
    return j


def get(port, path, timeout=6):
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, None
    except Exception as e:
        return None, {"error": type(e).__name__}


def pick(d, keys):
    return {k: d.get(k) for k in keys if isinstance(d, dict) and k in d}


def processes():
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    rows = {}
    for port in PORTS:
        pid = listener(port)
        if not pid:
            out(f"port {port}: no single listener"); continue
        argv = argv_of(pid)
        val = lambda f: argv[argv.index(f) + 1] if f in argv[:-1] else None
        f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        rows[port] = (pid, int(f[11]) + int(f[12]))
        j = journal_of(pid)
        out(f"port {port}: pid {pid} uptime {(time.time() - boot - int(f[19]) / TICK) / 3600:.1f} h "
            f"rss {int(f[21]) * PAGE // 1048576} MiB cwd {os.readlink(f'/proc/{pid}/cwd')} "
            f"build {val('--build-commit')} mode {val('--mode')} journal {os.path.basename(j) if j else None} "
            f"account_flags {any(a in argv for a in ('--account-readonly', '--private-dashboard'))}")
    t0 = time.monotonic(); time.sleep(10); dt = time.monotonic() - t0
    for port, (pid, c0) in rows.items():
        try:
            f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            out(f"port {port}: CPU {100 * (int(f[11]) + int(f[12]) - c0) / TICK / dt:.1f}% of one core (10 s)")
        except OSError:
            out(f"port {port}: exited during sampling")


def dashboards():
    for port in PORTS:
        if not listener(port):
            continue
        out(f"\n--- dashboard {port}")
        hc, h = get(port, "/healthz")
        w = (h or {}).get("workers") if isinstance(h, dict) else None
        out("healthz", hc, json.dumps(pick(h, ("status", "build_commit", "signals_only", "state_age_seconds", "state_version"))
                                      | {"workers_healthy": (w or {}).get("healthy"), "worker_reasons": (w or {}).get("reasons")}, default=str)[:700])
        if isinstance(w, dict):
            for name, info in (w.get("workers") or {}).items():
                out(f"  worker {name}:", json.dumps(pick(info, ("status", "thread_alive", "cycles", "last_error_type",
                                                                "last_completed_at", "seconds_since_completion")), default=str)[:300])
        rc, r = get(port, "/readyz")
        out("readyz", rc, json.dumps(pick(r, ("status", "contract", "contract_quotes_fresh", "reference_fresh", "stale_reasons"))))
        cc, c = get(port, "/api/candle-integrity")
        out("candles", cc, json.dumps(pick(c, ("status", "bars", "unique_bars", "duplicate_bars", "reason"))
                                      | {"missing_count": len((c or {}).get("missing_intervals") or []) if isinstance(c, dict) else None,
                                         "missing_first": ((c or {}).get("missing_intervals") or [])[:5] if isinstance(c, dict) else None}, default=str)[:500])
        sc, s = get(port, "/api/state?view=main", timeout=12)
        if isinstance(s, dict):
            mh, rt = s.get("market_health") or {}, s.get("runtime") or {}
            out("freshness", json.dumps(pick(mh, ("contract", "quote_age_seconds", "reference_age_seconds", "seconds_remaining",
                                                  "contract_quote_reasons", "reference_reasons", "active_candidates",
                                                  "next_known_open", "last_known_close")), default=str)[:600])
            lat = rt.get("latency_percentiles") or {}
            out("latency", json.dumps({k: pick(lat.get(k) or {}, ("p50_ms", "p95_ms", "samples")) for k in
                                       ("market_collection", "advanced_analysis", "prediction_calculation", "journal_write",
                                        "publish_after_collection", "total_processing")}))
            out("resources", json.dumps(rt.get("resources"), default=str)[:300])
            op = (s.get("prediction_engine") or {}).get("initial") or {}
            out("opening", json.dumps(pick(op, ("ticker", "status", "direction", "up", "reason", "opening_delay_seconds", "model"))
                                      | {"confidence": (op.get("confidence") or {}).get("status")}, default=str)[:700])
            out("scalp", json.dumps(pick(s, ("scalp", "scalp_explanation", "stale", "direction")), default=str)[:300])
            out("warnings", json.dumps(s.get("warnings"), default=str)[:400])
        else:
            out("state", sc, s)
        if port in ("8765", JOURNAL_PORT):
            code, body = get(port, "/api/opening-scoreboard", timeout=10)
            out("displayed opening scoreboard", code, json.dumps(body, default=str)[:900])


def clients():
    seen = {p: set() for p in PORTS}
    for n in range(3):
        snap = ss("-Htn", "state", "established")
        for line in snap.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                for p in PORTS:
                    if parts[2].endswith(":" + p):
                        seen[p].add(parts[3])
        if n < 2:
            time.sleep(5)
    for p in PORTS:
        out(f"port {p}: {len(seen[p])} distinct client connections in 3 samples over 10 s")
    names = {}
    for line in ss("-Htnp", "state", "established").splitlines():
        parts = line.split()
        if len(parts) >= 5:
            for p in PORTS:
                if parts[3].endswith(":" + p):
                    proc = re.findall(r'"([^"]+)"', parts[4])
                    names.setdefault(p, set()).add(proc[0] if proc else "?")
    for p, n in names.items():
        out(f"port {p}: client processes {sorted(n)}")


def journal():
    from src.signals_display.monitor import Journal
    pid = listener(JOURNAL_PORT)
    path = journal_of(pid) if pid else None
    if not path or not os.path.exists(path):
        out(f"no journal for port {JOURNAL_PORT}"); return
    out(f"journal of port {JOURNAL_PORT}: {os.path.basename(path)} ({os.path.getsize(path) / 1e9:.2f} GB)")
    j = Journal.__new__(Journal); j.path = path
    t = time.monotonic()
    with j.read() as db:
        rows = db.execute(
            """SELECT b.ticker,b.generated_at,b.up,b.payload,f.outcome FROM btc_engine_opening b
               LEFT JOIN forecast_outcomes f ON f.ticker=b.ticker AND f.origin='official'
               WHERE b.mode='PUBLIC MARKET DATA' ORDER BY b.generated_at""").fetchall()
    out(f"openings read: {len(rows)} in {time.monotonic() - t:.1f} s")
    board = j.verified_opening_scoreboard()
    out("verified_scoreboard", json.dumps(board, default=str))
    calls, waits, unavailable = [], {}, {}
    for ticker, gen, up, payload, outcome in rows:
        try:
            d = json.loads(payload) if payload else {}
        except Exception:
            d = {}
        direction, status = d.get("direction"), (d.get("confidence") or {}).get("status")
        if up is None:
            key = str(d.get("reason"))[:90]
            unavailable[key] = unavailable.get(key, 0) + 1
            continue
        if direction in ("UP", "DOWN") and status in j.DISPLAYABLE_OPENING_CONFIDENCE and outcome in (0, 1):
            scalars = {k: (v if not isinstance(v, str) else v[:80]) for k, v in d.items()
                       if isinstance(v, (int, float, str, bool)) or v is None}
            scalars.update(confidence=status, threshold=(d.get("decision_policy") or {}).get("threshold"),
                           bars=(d.get("data_quality") or {}).get("closed_history_bars"))
            calls.append((ticker, gen, up, direction, outcome, scalars))
        else:
            key = f"{direction} / {status}"
            waits[key] = waits.get(key, 0) + 1
    losses = [c for c in calls if (c[3] == "UP") != bool(c[4])]
    out(f"verified directional calls: {len(calls)}  losses: {len(losses)}")
    out("ALL LOSSES (every recorded scalar field):")
    for ticker, gen, up, direction, outcome, sc in losses:
        out("  LOSS " + json.dumps({"ticker": ticker, "generated_at": gen, "direction": direction, "up": up,
                                    "outcome_up": outcome, **sc}, default=str)[:1200])
    out("ALL VERIFIED CALLS (compact, chronological): generated_at direction up outcome result delay")
    for ticker, gen, up, direction, outcome, sc in calls:
        res = "WIN" if (direction == "UP") == bool(outcome) else "LOSS"
        out(f"  {gen} {direction:4} {up:.3f} {outcome} {res} delay={sc.get('opening_delay_seconds')} {ticker}")
    out("non-directional settled/unsettled openings by direction/confidence:", json.dumps(waits))
    out("openings without probability, by reason:")
    for reason, n in sorted(unavailable.items(), key=lambda kv: -kv[1])[:10]:
        out(f"  x{n}: {reason}")
    if rows:
        out("payload keys of newest opening:", sorted((json.loads(rows[-1][3]) if rows[-1][3] else {}).keys()))
    from src.signals_display.opening_models import dataset_coverage
    with j.read() as db:
        cov = dataset_coverage(db)
    out("promotion_coverage", json.dumps({k: cov.get(k) for k in (
        "official_settled_contracts", "eligible_frozen_openings", "excluded_contracts", "exclusion_reasons",
        "eligibility_rate", "minimum_for_first_shadow_evaluation", "remaining_to_first_shadow_evaluation")}, default=str))


if __name__ == "__main__":
    {"processes": processes, "dashboards": dashboards, "clients": clients, "journal": journal}[sys.argv[1]]()
