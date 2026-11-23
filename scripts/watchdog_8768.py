"""Unattended watchdog for production 8768 (signals only; never trades, never changes models or thresholds).

Run every 2 minutes by deploy/oracle/kalshi-8768-watchdog.timer. Each run:
  * snapshots the RUNNING 8768 server's exact argv / environment / working directory while it is healthy;
  * if no process listens on 8768 for two consecutive runs and no guarded deploy holds its lock, relaunches
    that exact snapshot (same artifact, journal, environment) - at most MAX_RESTARTS per RESTART_WINDOW_H,
    after which it stops trying and alerts (no restart loops);
  * checks data freshness, that an opening call was recorded recently, the Telegram alert service and its
    last delivery, official-settlement intake, the lifecycle training status, disk space and CPU;
  * sends ONE short Telegram message only when a check changes state (failing after 2 consecutive bad
    runs, or recovered), using the existing bot and destination. Routine runs send nothing.
Read-only against the journal (it never opens it); writes only under ~/dual-ai-alerts/watchdog/.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

PORT = 8768
HOME = Path.home()
BASE = HOME / "dual-ai-alerts"
WD = BASE / "watchdog"
DEPLOY_LOCKS = (HOME / ".kalshi-deploy/preview-8768/deploy.lock", HOME / ".kalshi-deploy/preview-8768/jobs.lock")
LIFECYCLE_STATUS = HOME / "kalshi-preview-upgrade/logs/upgraded-live-8768.db.lifecycle-status.json"
MAX_RESTARTS, RESTART_WINDOW_H = 3, 6
FAIL_AFTER = 2                       # consecutive bad runs before alerting
LIMITS = {"stale_s": 180, "opening_gap_min": 17, "disk_free_gb": 5.0, "settlement_gap_min": 120,
          "lifecycle_gap_min": 150, "cpu_pct": 97.0}


# ---------------------------------------------------------------- pure decision logic (unit tested)
def evaluate(checks, state):
    """checks: {name: (ok: bool, detail: str)}. Returns (messages, new_state). Alerts on transitions only."""
    st = {k: dict(v) for k, v in (state.get("checks") or {}).items()}
    msgs = []
    for name, (ok, detail) in sorted(checks.items()):
        c = st.setdefault(name, {"bad": 0, "alerted": False})
        if ok:
            if c.get("alerted"):
                msgs.append(f"RECOVERED: {name}")
            c.update(bad=0, alerted=False)
        else:
            c["bad"] = c.get("bad", 0) + 1
            if c["bad"] >= FAIL_AFTER and not c.get("alerted"):
                msgs.append(f"PROBLEM: {name} - {detail}")
                c["alerted"] = True
    return msgs, {**state, "checks": st}


def restart_decision(listening, state, deploy_running, snapshot_ok, now):
    """Return (action, new_state): action in NONE, WAIT, RESTART, LIMIT, NO_SNAPSHOT, DEPLOY_RUNNING."""
    s = dict(state)
    hist = [t for t in s.get("restarts", []) if now - t < RESTART_WINDOW_H * 3600]
    s["restarts"] = hist
    if listening:
        s["down_runs"] = 0
        s.pop("limit_alerted", None)
        return "NONE", s
    s["down_runs"] = s.get("down_runs", 0) + 1
    if deploy_running:
        return "DEPLOY_RUNNING", s
    if s["down_runs"] < 2:
        return "WAIT", s
    if not snapshot_ok:
        return "NO_SNAPSHOT", s
    if len(hist) >= MAX_RESTARTS:
        return "LIMIT", s
    s["restarts"] = hist + [now]
    s["down_runs"] = 0
    return "RESTART", s


# ---------------------------------------------------------------- probes
def listener_pid(port=PORT):
    out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True).stdout
    for part in out.split("pid=")[1:]:
        return int(part.split(",")[0])
    return None


def lock_held(path):
    import fcntl
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def snapshot(pid, wd=WD):
    """Save argv/env/cwd of the healthy server (NUL-separated, 0600) for an identical relaunch."""
    wd.mkdir(parents=True, exist_ok=True)
    argv = Path(f"/proc/{pid}/cmdline").read_bytes()
    env = Path(f"/proc/{pid}/environ").read_bytes()
    cwd = os.readlink(f"/proc/{pid}/cwd")
    for name, data in (("argv", argv), ("env", env), ("cwd", cwd.encode())):
        p = wd / f"server.{name}"
        if not p.exists() or p.read_bytes() != data:
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)


def relaunch(wd=WD):
    """Start the snapshot as its own transient systemd unit, so it outlives this one-shot watchdog run."""
    argv = [a.decode() for a in (wd / "server.argv").read_bytes().split(b"\0") if a]
    env = [e.decode() for e in (wd / "server.env").read_bytes().split(b"\0") if b"=" in e]
    cwd = (wd / "server.cwd").read_bytes().decode()
    unit = f"kalshi-8768-relaunch-{int(time.time())}"
    cmd = ["sudo", "-n", "systemd-run", f"--unit={unit}", f"--uid={os.getuid()}", f"--gid={os.getgid()}",
           f"--working-directory={cwd}", "--property=KillMode=process", "--collect",
           "/usr/bin/env", "-i", *env, *argv]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return unit


def get(path, timeout=10):
    with urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def cpu_pct(pid, state):
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(f[11]) + int(f[12])
    except (OSError, IndexError, ValueError):
        return None, state
    now = time.monotonic()
    prev = state.get("cpu_sample")
    state = {**state, "cpu_sample": [pid, ticks, now]}
    if not prev or prev[0] != pid or now <= prev[2]:
        return None, state
    return 100.0 * (ticks - prev[1]) / os.sysconf("SC_CLK_TCK") / (now - prev[2]), state


def gather(pid, state, now):
    checks = {}
    try:
        code, st = get("/api/state")
        pe = st.get("prediction_engine") or {}
        gen = _ts(st.get("generated_at"))
        age = (now - gen).total_seconds() if gen else None
        checks["dashboard data fresh"] = (age is not None and age < LIMITS["stale_s"], f"state age {age}s")
        init = _ts((pe.get("initial") or {}).get("generated_at"))
        gap = (now - init).total_seconds() / 60 if init else None
        checks["opening calls recorded"] = (gap is not None and gap < LIMITS["opening_gap_min"],
                                            f"last opening call {gap and round(gap)} min ago")
    except Exception as exc:
        checks["dashboard API"] = (False, type(exc).__name__)
    active = subprocess.run(["systemctl", "is-active", "kalshi-dual-ai-alerts"], capture_output=True, text=True).stdout.strip()
    checks["telegram alert service"] = (active == "active", active)
    try:
        db = sqlite3.connect(f"file:{BASE / 'sent.sqlite3'}?mode=ro", uri=True)
        last = db.execute("SELECT status, at FROM deliveries ORDER BY id DESC LIMIT 1").fetchone()
        checks["telegram delivery"] = (last is None or last[0] == "SENT", f"last delivery {last}")
        seen = db.execute("SELECT MAX(seen_at) FROM outcomes7").fetchone()[0]
        sgap = (now - _ts(seen)).total_seconds() / 60 if seen else None
        checks["official settlements recorded"] = (sgap is not None and sgap < LIMITS["settlement_gap_min"],
                                                   f"newest settlement recorded {sgap and round(sgap)} min ago")
    except Exception as exc:
        checks["telegram delivery"] = (False, type(exc).__name__)
    try:
        ls = json.loads(LIFECYCLE_STATUS.read_text())
        lgap = (now - _ts(ls.get("generated_at"))).total_seconds() / 60
        checks["lifecycle training"] = (not ls.get("errors") and lgap < LIMITS["lifecycle_gap_min"],
                                        f"last cycle {round(lgap)} min ago, errors {ls.get('errors')}")
    except Exception as exc:
        checks["lifecycle training"] = (False, type(exc).__name__)
    free = os.statvfs("/")
    gb = free.f_bavail * free.f_frsize / 1e9
    checks["disk space"] = (gb > LIMITS["disk_free_gb"], f"{gb:.1f} GB free")
    cpu, state = cpu_pct(pid, state) if pid else (None, state)
    if cpu is not None:
        hot = state.get("hot_runs", 0) + 1 if cpu > LIMITS["cpu_pct"] else 0
        state = {**state, "hot_runs": hot}
        checks["server CPU"] = (hot < 8, f"{cpu:.0f}% for {hot} runs")     # ~16 min sustained before alerting
    return checks, state


def send(lines):
    sys.path.insert(0, str(BASE / "vendor"))
    from src.signals_display import phone_alerts
    cfg = phone_alerts.load_config()
    if "telegram" in phone_alerts.channels(cfg):
        phone_alerts.send("telegram", cfg, "8768 WATCHDOG\n" + "\n".join(lines))


def main(argv=None):
    dry = "--dry-run" in (argv or sys.argv[1:])
    WD.mkdir(parents=True, exist_ok=True)
    state_path = WD / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    now = datetime.now(timezone.utc)
    pid = listener_pid()
    msgs = []
    if pid:
        try:
            if get("/readyz", timeout=10)[0] == 200:
                snapshot(pid)
        except Exception:
            pass
    deploy = any(lock_held(p) for p in DEPLOY_LOCKS)
    snap_ok = all((WD / f"server.{n}").exists() for n in ("argv", "env", "cwd"))
    action, state = restart_decision(pid is not None, state, deploy, snap_ok, time.time())
    if action == "RESTART" and not dry:
        try:
            unit = relaunch()
            msgs.append(f"8768 was down; relaunched the last healthy build ({unit}).")
        except Exception as exc:
            msgs.append(f"8768 is DOWN and the relaunch failed ({type(exc).__name__}). Manual action needed.")
    elif action == "LIMIT" and not state.get("limit_alerted"):
        msgs.append(f"8768 is DOWN and {MAX_RESTARTS} restarts in {RESTART_WINDOW_H} h were used; not retrying. Manual action needed.")
        state["limit_alerted"] = True
    elif action == "NO_SNAPSHOT" and not state.get("limit_alerted"):
        msgs.append("8768 is DOWN and no healthy launch snapshot exists; not restarting. Manual action needed.")
        state["limit_alerted"] = True
    checks, state = gather(pid, state, now) if pid else ({}, state)
    more, state = evaluate(checks, state)
    msgs += more
    state["last_run"] = now.isoformat()
    state["last_checks"] = {k: [ok, d] for k, (ok, d) in checks.items()}
    state["last_action"] = action
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    os.replace(tmp, state_path)
    if msgs:
        print(json.dumps({"at": now.isoformat(), "action": action, "messages": msgs}))
        if not dry:
            try:
                send(msgs)
            except Exception as exc:
                print(json.dumps({"telegram_error": type(exc).__name__}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
