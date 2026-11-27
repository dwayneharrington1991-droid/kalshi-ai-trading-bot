#!/usr/bin/env bash
# READ-ONLY CPU/memory profile of the Python processes on the Oracle host.
#
#   collect_process_profile.sh [seconds]      (default 60, allowed 20..300)
#
# For every Python process: CPU share over the sampling window (from
# /proc/<pid>/stat deltas), resident memory, nice value, uptime, thread count,
# the busiest threads, and a minimal identity (module, --port, --mode, working
# directory and journal file NAME only; never full arguments or environment).
# Host: CPU count, load, memory, swap and I/O wait (vmstat), and the size of
# every SQLite journal and its WAL.
# Preview 8768 only (never 8765/8767): its own self-reported per-stage cycle
# timings from GET /api/state?view=main, sampled every 10 s.
#
# Nothing is started, stopped, signalled, attached to (no ptrace), installed,
# written or deleted. It runs at the lowest CPU and I/O priority.
set -uo pipefail
umask 077
secs=${1:-60}
[[ $secs =~ ^[0-9]+$ ]] && (( secs >= 20 && secs <= 300 )) || { echo "seconds must be 20..300"; exit 2; }
PYTHON=${PREVIEW_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
PREVIEW_PORT=${PREVIEW_PROBE_PORT:-8768}
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true
section() { printf '\n===== %s =====\n' "$*"; }

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ) window=${secs}s"
echo "cpus: $(nproc 2>/dev/null)  clock_ticks: $(getconf CLK_TCK)"
cat /proc/loadavg
free -m
section "VMSTAT (5 s samples: r=run queue, si/so=swap, wa=I/O wait, st=steal)"
vmstat 5 4 2>&1

section "PYTHON PROCESSES AND THREADS OVER ${secs}s"
"$PYTHON" - "$secs" "$PREVIEW_PORT" <<'PY'
import json, os, sys, time
from urllib.request import ProxyHandler, build_opener

secs, preview_port = int(sys.argv[1]), sys.argv[2]
tick = os.sysconf("SC_CLK_TCK")
page = os.sysconf("SC_PAGE_SIZE")
boot = time.time() - float(open("/proc/uptime").read().split()[0])

def stat(path):
    try:
        raw = open(path).read()
    except OSError:
        return None
    rest = raw[raw.rindex(")") + 2:].split()
    # fields after "(comm) ": state=0 ... utime=11 stime=12 nice=16 threads=17 starttime=19 rss=21
    return {"comm": raw[raw.index("(") + 1:raw.rindex(")")], "cpu": int(rest[11]) + int(rest[12]),
            "nice": int(rest[16]), "threads": int(rest[17]), "start": int(rest[19]), "rss": int(rest[21])}

def identity(pid):
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        argv = [a.decode("utf-8", "replace") for a in argv if a]
    except OSError:
        return None
    if not argv or "python" not in os.path.basename(argv[0]):
        return None
    def after(flag):
        return argv[argv.index(flag) + 1] if flag in argv[:-1] else None
    module = after("-m")
    script = None if module else next((os.path.basename(a) for a in argv[1:] if a.endswith(".py")), None)
    try:
        cwd = os.path.basename(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        cwd = "?"
    journal = after("--journal")
    return {"module": module or script or "?", "port": after("--port"), "mode": after("--mode"),
            "cwd": cwd, "journal": os.path.basename(journal) if journal else None}

def threads(pid):
    out = {}
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            s = stat(f"/proc/{pid}/task/{tid}/stat")
            if s:
                out[tid] = s
    except OSError:
        pass
    return out

pids = {}
for pid in os.listdir("/proc"):
    if pid.isdigit() and int(pid) not in (os.getpid(), os.getppid()):
        who = identity(pid)
        if who:
            pids[pid] = (who, stat(f"/proc/{pid}/stat"), threads(pid))

opener = build_opener(ProxyHandler({}))
samples = []
deadline = time.monotonic() + secs
while time.monotonic() < deadline:
    try:
        with opener.open(f"http://127.0.0.1:{preview_port}/api/state?view=main", timeout=5) as r:
            body = json.load(r)
        rt, mh = body.get("runtime") or {}, body.get("market_health") or {}
        samples.append({
            "at": time.strftime("%H:%M:%S", time.gmtime()),
            "quote_age_s": mh.get("quote_age_seconds"), "reference_age_s": mh.get("reference_age_seconds"),
            "processing_ms": rt.get("processing_ms"), "update_interval_ms": rt.get("update_interval_ms"),
            "phases_ms": rt.get("phases_ms"), "build": rt.get("build_short"),
            "state_age_s": rt.get("state_age_seconds"),
        })
        last_runtime = rt
    except Exception as exc:
        samples.append({"at": time.strftime("%H:%M:%S", time.gmtime()), "error": type(exc).__name__})
    time.sleep(min(10, max(0, deadline - time.monotonic())))

rows = []
for pid, (who, first, first_threads) in pids.items():
    last = stat(f"/proc/{pid}/stat")
    if not first or not last:
        continue
    cpu = 100.0 * (last["cpu"] - first["cpu"]) / tick / secs
    busiest = []
    for tid, s in threads(pid).items():
        if tid in first_threads:
            busiest.append((100.0 * (s["cpu"] - first_threads[tid]["cpu"]) / tick / secs, tid, s["comm"], s["nice"]))
    busiest.sort(reverse=True)
    rows.append((cpu, pid, who, last, busiest[:6]))
rows.sort(key=lambda r: r[0], reverse=True)
total = sum(r[0] for r in rows)
print(f"Python processes: {len(rows)}; combined CPU {total:.1f}% of one core over {secs}s")
for cpu, pid, who, last, busiest in rows:
    up = time.time() - (boot + last["start"] / tick)
    print(f"\npid {pid}  CPU {cpu:5.1f}%  RSS {last['rss'] * page / 1048576:7.1f} MiB  nice {last['nice']}  "
          f"threads {last['threads']}  uptime {up / 3600:6.1f} h")
    print("  identity: " + json.dumps(who, sort_keys=True))
    for tcpu, tid, comm, nice in busiest:
        print(f"    thread {tid:>7} {comm:<16} CPU {tcpu:5.1f}%  nice {nice}")

print(f"\n--- preview {preview_port} self-reported cycle timing (every 10 s) ---")
for s in samples:
    print(json.dumps(s, sort_keys=True, default=str)[:700])
try:
    print("latency_percentiles: " + json.dumps(last_runtime.get("latency_percentiles"), sort_keys=True, default=str)[:1500])
    print("resources: " + json.dumps(last_runtime.get("resources"), sort_keys=True, default=str)[:600])
except NameError:
    print("preview runtime unavailable")
PY

section "SQLITE JOURNALS AND WAL FILES (sizes only)"
find /home/opc -xdev -maxdepth 4 \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) -size +1M \
  -printf '%10s bytes  %TY-%Tm-%Td %TH:%TM  %p\n' 2>/dev/null | sort -rn | head -n 25

echo
echo "PROCESS_PROFILE_COMPLETE (read-only; nothing was changed)"
