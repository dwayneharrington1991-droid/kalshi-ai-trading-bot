"""Preview-8768 deployment gates (Phase 5): preflight, soak, postflight.

Called by deploy_preview_8768.sh while it holds the exclusive deployment lock:

    preview_gates_8768.py preflight  <out.json>
    preview_gates_8768.py soak       <40-char sha> <preflight.json> <out.json>
    preview_gates_8768.py postflight <40-char sha> <preflight.json> <out.json>

Exit 0 = pass, 1 = fail (the orchestrator then blocks or rolls back), 2 = usage.
Read-only: GET requests to 127.0.0.1:8768 only; /proc and `ss` for every other
fact. Never contacts 8765/8767; never prints environment values.

Thresholds are realistic for the single-core Oracle host (measured 2026-11-18:
load 7.1-8.4, MemAvailable ~2.0 GB, SwapFree ~2.4 GB, old 8768 build p95
publish-after-collection 4.25 s, prediction p50 1.70 s, 8768 CPU 20.8%).
Overrides are accepted ONLY in the orchestrator's local test mode.
"""
import json
import os
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

LIMITS = {
    # preflight (before anything changes)
    "max_load1": 12.0,                 # ~50% above the observed 7-8.4 on 1 core
    "min_mem_available_mib": 768,
    "min_swap_free_mib": 1024,
    "max_swap_pages_per_s": 200,       # no deployment during swap thrashing
    "min_disk_free_mib": 3072,
    "opening_window": True,            # block 90 s before .. 150 s after each quarter hour
    "cpu_sample_s": 20,
    "swap_sample_s": 10,
    # soak (rollback armed)
    "soak_seconds": 900,
    "soak_interval_s": 10,
    "max_age_s": 10.0,                 # any sample above this fails immediately
    "fresh_age_s": 5.0,
    "min_fresh_share": 0.95,
    "max_publish_p95_ms": 3000,
    "max_publish_p99_ms": 4500,
    "max_prediction_p50_ms": 1020,     # 60% of the measured 1698 ms
    "min_latency_samples": 100,        # per meter, for a statistic to count
    # The server keeps the last 300 cycles per stage; a statistic counts only
    # once >= 300 cycles were published after the soak began, so startup
    # cycles can never hide in (or inflate) the soak's percentiles.
    "latency_window_cycles": 300,
    "min_evaluable_share": 0.50,       # of soak samples with a fully refreshed window
    # Contract rollover: from 20 s before to 60 s after each quarter hour the
    # active contract legitimately changes; only contract-quote readiness
    # reasons are tolerated there (health, build, state, reference strict).
    "rollover_before_s": 20,
    "rollover_after_s": 60,
    "min_sample_share": 0.90,          # of the expected number of soak samples
    # postflight
    "post_min_mem_available_mib": 512,
    "post_min_swap_free_mib": 768,
    "post_max_load1": 14.0,
    "post_max_load_increase": 2.0,
    # Four CPU-hungry processes share one core: a faster build may take about
    # its fair share again, so allow +10 points, never above 40% of the core.
    "post_max_cpu_increase_pts": 10.0,
    "post_max_cpu_pct": 40.0,
    "post_cpu_sample_s": 60,
}
PROTECTED = (8765, 8767)
OBSERVED = (8769,)
PORT = 8768
OPENER = build_opener(ProxyHandler({}))


def load_limits():
    limits = dict(LIMITS)
    raw = os.environ.get("PREVIEW_GATES_TEST_OVERRIDES")
    if raw:
        if not os.environ.get("PREVIEW_TEST_INSTALLER"):
            fail_exit("PREVIEW_GATES_TEST_OVERRIDES is only accepted in local test mode")
        for key, value in json.loads(raw).items():
            if key not in limits:
                fail_exit(f"unknown override {key}")
            limits[key] = value
    return limits


def say(msg):
    print(f"DEPLOY: {msg}", flush=True)


def fail_exit(msg, code=1):
    say(f"GATE FAIL: {msg}")
    sys.exit(code)


def listener(port):
    """PIDs listening on port (list); raises if ss cannot be run."""
    for cmd in (["sudo", "-n", "ss", "-H", "-ltnp", f"sport = :{port}"], ["ss", "-H", "-ltnp", f"sport = :{port}"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode == 0 and (out.stdout.strip() or cmd[0] == "ss"):
            return sorted(set(re.findall(r"pid=(\d+)", out.stdout)))
    raise RuntimeError(f"ss failed for port {port}")


def proc_identity(pid):
    """PID and kernel start time: together they prove it is the same process.
    (The run state R/S/D changes constantly and must not be compared.)"""
    raw = open(f"/proc/{pid}/stat").read()
    fields = raw.rsplit(")", 1)[1].split()
    if fields[0] in ("Z", "X"):
        raise OSError(f"process {pid} has exited")
    return {"pid": int(pid), "start": fields[19]}


def cpu_ticks(pid):
    fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])


def cpu_share(pid, seconds):
    tick = os.sysconf("SC_CLK_TCK")
    a, t0 = cpu_ticks(pid), time.monotonic()
    time.sleep(seconds)
    b, t1 = cpu_ticks(pid), time.monotonic()
    return 100.0 * (b - a) / tick / (t1 - t0)


def meminfo():
    values = {}
    for line in open("/proc/meminfo"):
        key, rest = line.split(":", 1)
        values[key] = int(rest.split()[0]) // 1024
    return values


def swap_rate(seconds):
    def read():
        v = {}
        for line in open("/proc/vmstat"):
            k, n = line.split()
            if k in ("pswpin", "pswpout"):
                v[k] = int(n)
        return v.get("pswpin", 0) + v.get("pswpout", 0)
    a = read(); time.sleep(seconds); b = read()
    return (b - a) / seconds


def load1():
    return float(open("/proc/loadavg").read().split()[0])


def in_opening_window(now=None):
    pos = int(now if now is not None else time.time()) % 900
    return pos >= 810 or pos < 150


ROLLOVER_REASONS = {"NO_ACTIVE_CONTRACT", "ACTIVE_CONTRACT_SELECTION_PENDING", "CONTRACT_NOT_OPEN",
                    "CONTRACT_EXPIRED", "QUOTE_OLDER_THAN_5_SECONDS", "INVALID_CONTRACT_OR_QUOTE_TIMESTAMP"}


def in_rollover(L, now=None):
    pos = (now if now is not None else time.time()) % 900
    return pos >= 900 - L["rollover_before_s"] or pos < L["rollover_after_s"]


def get(path):
    try:
        with OPENER.open(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        try:                                   # /readyz answers 503 with a JSON body of reasons
            return e.code, json.load(e)
        except Exception:
            return e.code, None
    except Exception as e:
        return None, {"_error": type(e).__name__}


def identities(ports, required):
    out, problems = {}, []
    for port in ports:
        try:
            pids = listener(port)
        except RuntimeError as exc:
            problems.append(str(exc)); continue
        if len(pids) != 1:
            if required or pids:
                problems.append(f"port {port}: expected exactly one listener, got {pids or 'none'}")
            continue
        try:
            out[str(port)] = proc_identity(pids[0])
        except OSError:
            problems.append(f"port {port}: cannot read process {pids[0]}")
    return out, problems


def write(path, data):
    tmp = path + ".next"
    with open(tmp, "w") as f:
        json.dump(data, f, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def preflight(out_path, L):
    problems = []
    if L["opening_window"] and in_opening_window():
        problems.append("inside an opening window (90 s before to 150 s after a quarter hour)")
    l1 = load1()
    if l1 > L["max_load1"]:
        problems.append(f"load1 {l1} > {L['max_load1']}")
    mem = meminfo()
    if mem.get("MemAvailable", 0) < L["min_mem_available_mib"]:
        problems.append(f"MemAvailable {mem.get('MemAvailable')} MiB < {L['min_mem_available_mib']}")
    if mem.get("SwapFree", 0) < L["min_swap_free_mib"]:
        problems.append(f"SwapFree {mem.get('SwapFree')} MiB < {L['min_swap_free_mib']}")
    rate = swap_rate(L["swap_sample_s"])
    if rate > L["max_swap_pages_per_s"]:
        problems.append(f"swap activity {rate:.0f} pages/s > {L['max_swap_pages_per_s']}")
    st = os.statvfs(os.path.expanduser("~"))
    disk = st.f_bavail * st.f_frsize // 1048576
    if disk < L["min_disk_free_mib"]:
        problems.append(f"disk free {disk} MiB < {L['min_disk_free_mib']}")
    protected, p = identities(PROTECTED, True); problems += p
    observed, p = identities(OBSERVED, False); problems += p
    old_cpu = None
    try:
        pids = listener(PORT)
        if len(pids) != 1:
            problems.append(f"port {PORT}: expected exactly one listener, got {pids or 'none'}")
        else:
            old_cpu = cpu_share(pids[0], L["cpu_sample_s"])
    except (RuntimeError, OSError) as exc:
        problems.append(f"cannot measure the current 8768 CPU: {exc}")
    data = {"load1": l1, "mem_available_mib": mem.get("MemAvailable"), "swap_free_mib": mem.get("SwapFree"),
            "swap_pages_per_s": rate, "disk_free_mib": disk, "protected": protected, "observed": observed,
            "old_8768_cpu_pct": old_cpu, "cpus": os.cpu_count(), "at": time.time()}
    say("preflight " + json.dumps({k: v for k, v in data.items() if k not in ("protected", "observed")}, sort_keys=True, default=str))
    if problems:
        for msg in problems:
            say(f"GATE FAIL: preflight: {msg}")
        return 1
    write(out_path, data)
    say("preflight passed (nothing changed yet)")
    return 0


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def soak(sha, pre_path, out_path, L):
    pre = json.load(open(pre_path))
    expected_n = int(L["soak_seconds"] / L["soak_interval_s"])
    started = time.monotonic()
    deadline = started + L["soak_seconds"]
    samples = evaluable = fresh = strict_samples = rollover_samples = 0
    first_pid = None
    version0 = None
    worst = {"publish_p95": 0, "publish_p99": 0, "prediction_p50": 0, "age": 0}
    say(f"soak started: {L['soak_seconds']} s, every {L['soak_interval_s']} s; rollback armed")

    def stop(msg):
        write(out_path, {"result": "FAIL", "reason": msg, "samples": samples})
        fail_exit(f"soak: {msg}")

    while time.monotonic() < deadline:
        samples += 1
        hc, h = get("/healthz")
        if not (hc == 200 and isinstance(h, dict) and h.get("status") == "HEALTHY" and h.get("signals_only") is True
                and (h.get("workers") or {}).get("healthy") is True):
            stop(f"sample {samples}: /healthz not HEALTHY with healthy workers and signals-only (HTTP {hc})")
        if h.get("build_commit") != sha:
            stop(f"sample {samples}: build {h.get('build_commit')!r} is not exactly {sha}")
        rollover = in_rollover(L)
        rc, r = get("/readyz")
        if not (rc in (200, 503) and isinstance(r, dict) and r.get("signals_only") is True):
            stop(f"sample {samples}: /readyz unsuccessful or not signals-only (HTTP {rc})")
        ready = (rc == 200 and r.get("status") == "READY" and r.get("contract_quotes_fresh") is True
                 and r.get("reference_fresh") is True)
        if not ready:
            reasons = set(r.get("stale_reasons") or [])
            tolerated = (rollover and r.get("reference_fresh") is True and reasons
                         and reasons <= ROLLOVER_REASONS)
            if not tolerated:
                stop(f"sample {samples}: /readyz not READY (HTTP {rc}, reasons {sorted(reasons)}"
                     f"{', outside the rollover window' if not rollover else ''})")
        sc, s = get("/api/state?view=main")
        if sc != 200 or not isinstance(s, dict):
            stop(f"sample {samples}: /api/state unsuccessful (HTTP {sc})")
        mh, rt = s.get("market_health"), s.get("runtime")
        if not isinstance(mh, dict) or not isinstance(rt, dict):
            stop(f"sample {samples}: /api/state lacks market_health/runtime")
        q, ref = mh.get("quote_age_seconds"), mh.get("reference_age_seconds")
        if not number(ref) or ref < 0 or ref > L["max_age_s"]:
            stop(f"sample {samples}: reference age {ref!r} unmeasurable or outside 0..{L['max_age_s']} s")
        if ready:
            strict_samples += 1
            if not number(q):
                stop(f"sample {samples}: quote age unmeasurable ({q!r}) while READY")
            age = max(q, ref)
            worst["age"] = max(worst["age"], age)
            if age < 0 or age > L["max_age_s"]:
                stop(f"sample {samples}: quote/reference age {age} s outside 0..{L['max_age_s']}")
            if age <= L["fresh_age_s"]:
                fresh += 1
        else:
            rollover_samples += 1
            age = ref
        version = rt.get("state_version")
        if not number(version):
            stop(f"sample {samples}: runtime state_version unmeasurable")
        version0 = version if version0 is None else version0
        lat = rt.get("latency_percentiles")
        if not isinstance(lat, dict):
            stop(f"sample {samples}: latency percentiles missing")
        pub, pred = lat.get("publish_after_collection"), lat.get("prediction_calculation")
        if not (isinstance(pub, dict) and isinstance(pred, dict)):
            stop(f"sample {samples}: publish/prediction latency meters missing")
        for meter, keys in ((pub, ("p95_ms", "p99_ms", "samples")), (pred, ("p50_ms", "samples"))):
            if not all(number(meter.get(k)) for k in keys):
                stop(f"sample {samples}: latency meter values unmeasurable")
        refreshed = version - version0 >= L["latency_window_cycles"]
        if refreshed and min(pub["samples"], pred["samples"]) >= L["min_latency_samples"]:
            evaluable += 1
            worst["publish_p95"] = max(worst["publish_p95"], pub["p95_ms"])
            worst["publish_p99"] = max(worst["publish_p99"], pub["p99_ms"])
            worst["prediction_p50"] = max(worst["prediction_p50"], pred["p50_ms"])
            if pub["p95_ms"] > L["max_publish_p95_ms"]:
                stop(f"sample {samples}: publish p95 {pub['p95_ms']} ms > {L['max_publish_p95_ms']}")
            if pub["p99_ms"] > L["max_publish_p99_ms"]:
                stop(f"sample {samples}: publish p99 {pub['p99_ms']} ms > {L['max_publish_p99_ms']}")
            if pred["p50_ms"] > L["max_prediction_p50_ms"]:
                stop(f"sample {samples}: prediction p50 {pred['p50_ms']} ms > {L['max_prediction_p50_ms']}")
        try:
            pids = listener(PORT)
        except RuntimeError as exc:
            stop(str(exc))
        if len(pids) != 1 or (first_pid and pids[0] != first_pid):
            stop(f"sample {samples}: 8768 listener changed or not unique ({pids})")
        first_pid = first_pid or pids[0]
        now, problems = identities(PROTECTED, True)
        if problems or now != pre["protected"]:
            stop(f"sample {samples}: protected listener identity changed or unreadable")
        if samples % 6 == 0:
            say(f"soak sample {samples}: age {age:.2f} s{' (rollover)' if not ready else ''}, publish p95 {pub['p95_ms']} ms, prediction p50 {pred['p50_ms']} ms")
        # Fixed cadence: sample k is due at started + k * interval, so slow
        # requests do not stretch the schedule (samples ~= expected).
        next_due = started + samples * L["soak_interval_s"]
        time.sleep(max(0, min(next_due, deadline) - time.monotonic()))
    if samples < L["min_sample_share"] * expected_n:
        stop(f"only {samples} samples (need >= {L['min_sample_share']:.0%} of {expected_n})")
    if evaluable < L["min_evaluable_share"] * samples:
        stop(f"latency statistics were fully refreshed (>= {L['latency_window_cycles']} soak cycles, "
             f">= {L['min_latency_samples']} measurements) in only {evaluable}/{samples} samples")
    if strict_samples == 0:
        stop("no READY samples during the soak")
    share = fresh / strict_samples
    if share < L["min_fresh_share"]:
        stop(f"quote/reference fresh in {share:.0%} of samples (need {L['min_fresh_share']:.0%})")
    write(out_path, {"result": "PASS", "samples": samples, "evaluable": evaluable, "fresh_share": share,
                     "rollover_samples": rollover_samples, "worst": worst, "pid": first_pid})
    say(f"soak passed: {samples} samples, {evaluable} with full latency data, fresh {share:.0%}, worst {worst}")
    return 0


def postflight(sha, pre_path, out_path, L):
    pre = json.load(open(pre_path))
    problems = []
    now, p = identities(PROTECTED, True)
    problems += p
    if now != pre["protected"]:
        problems.append("protected listener identity changed")
    mem = meminfo()
    if mem.get("MemAvailable", 0) < L["post_min_mem_available_mib"]:
        problems.append(f"MemAvailable {mem.get('MemAvailable')} MiB < {L['post_min_mem_available_mib']}")
    if mem.get("SwapFree", 0) < L["post_min_swap_free_mib"]:
        problems.append(f"SwapFree {mem.get('SwapFree')} MiB < {L['post_min_swap_free_mib']}")
    rate = swap_rate(L["swap_sample_s"])
    if rate > L["max_swap_pages_per_s"]:
        problems.append(f"swap activity {rate:.0f} pages/s > {L['max_swap_pages_per_s']}")
    l1 = load1()
    if l1 > L["post_max_load1"] or l1 > pre["load1"] + L["post_max_load_increase"]:
        problems.append(f"load1 {l1} (pre {pre['load1']}; limits {L['post_max_load1']}, +{L['post_max_load_increase']})")
    cpu = None
    try:
        pids = listener(PORT)
        if len(pids) != 1:
            problems.append(f"8768 listener not unique: {pids}")
        else:
            cpu = cpu_share(pids[0], L["post_cpu_sample_s"])
            base = pre.get("old_8768_cpu_pct")
            if not number(base):
                problems.append("no preflight CPU baseline")
            elif cpu > base + L["post_max_cpu_increase_pts"] or cpu > L["post_max_cpu_pct"]:
                problems.append(f"new 8768 CPU {cpu:.1f}% (old {base:.1f}%; limits +{L['post_max_cpu_increase_pts']} pts, {L['post_max_cpu_pct']}%)")
    except (RuntimeError, OSError) as exc:
        problems.append(f"cannot measure 8768 CPU: {exc}")
    hc, h = get("/healthz")
    if not (hc == 200 and isinstance(h, dict) and h.get("build_commit") == sha and h.get("status") == "HEALTHY"):
        problems.append("final /healthz not HEALTHY on the exact build")
    data = {"mem_available_mib": mem.get("MemAvailable"), "swap_free_mib": mem.get("SwapFree"),
            "swap_pages_per_s": rate, "load1": l1, "new_8768_cpu_pct": cpu}
    say("postflight " + json.dumps(data, sort_keys=True, default=str))
    write(out_path, {**data, "result": "FAIL" if problems else "PASS", "problems": problems})
    if problems:
        for msg in problems:
            say(f"GATE FAIL: postflight: {msg}")
        return 1
    say("postflight passed")
    return 0


def main(argv):
    if len(argv) < 2:
        fail_exit("usage: preflight <out> | soak <sha> <pre> <out> | postflight <sha> <pre> <out>", 2)
    L = load_limits()
    cmd = argv[1]
    if cmd == "preflight" and len(argv) == 3:
        return preflight(argv[2], L)
    if cmd in ("soak", "postflight") and len(argv) == 5:
        if not re.fullmatch(r"[0-9a-f]{40}", argv[2]):
            fail_exit("an exact 40-character commit is required", 2)
        return (soak if cmd == "soak" else postflight)(argv[2], argv[3], argv[4], L)
    fail_exit("usage: preflight <out> | soak <sha> <pre> <out> | postflight <sha> <pre> <out>", 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
