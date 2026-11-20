"""Production restart integration for guarded model activation. Refuses anything it cannot identify exactly.

A restart replaces the running service process with the SAME launch command, changing only the
model artifact argument. The restarter:

  1 finds the process LISTENING on the configured port (ss), never by name
  2 verifies its command line equals the captured template with the current artifact substituted
     (the same executable, arguments and working directory), and refuses otherwise
  3 refuses if the process is managed by systemd (a cgroup *.service): that needs the systemd path
  4 sends SIGTERM only, waits up to 60 seconds for a clean exit (no force-kill)
  5 starts the identical command with the new artifact, output appended to a log, detached
  6 reports success only when the port listens again (the controller then verifies the model file)

The captured environment is stored in a private (0600) file outside the repository and is never
printed or logged. Secrets in it are never written to reports, status files or Telegram.
"""
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

ARTIFACT_FLAGS = ("--opening-model-artifact", "--artifact")
TERM_WAIT_S = 60
START_WAIT_S = 30


def _listener_pid(port):
    try:
        out = subprocess.run(["ss", "-Hltnp", f"sport = :{port}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = set(re.findall(r"pid=(\d+)", out))
    return int(pids.pop()) if len(pids) == 1 else None


def _cmdline(pid):
    return [a.decode() for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]


def _exited(pid):
    """Gone, or a zombie (exited and waiting to be reaped by its parent)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state == "Z"


def _service_units(text):
    return set(re.findall(r"([^/\s]+\.service)", text))


def _cgroup_is_systemd(pid):
    """True when the process is managed by a systemd service other than our own.

    A process inside the caller's own service unit (for example every process
    of a CI runner, or of a cron job inside crond.service) is not separately
    managed: treating that unit as the target's manager would make a restart
    act on the caller itself.
    """
    try:
        target = _service_units(Path(f"/proc/{pid}/cgroup").read_text())
    except OSError:
        return True                                   # cannot prove it is not systemd-managed: refuse
    try:
        own = _service_units(Path("/proc/self/cgroup").read_text())
    except OSError:
        own = set()
    return bool(target - own)


def artifact_flag_of(argv):
    """The artifact flag present in a launch command (exactly one), else None."""
    present = [f for f in ARTIFACT_FLAGS if f in argv]
    return present[0] if len(present) == 1 else None


def expected_argv(template, artifact_path):
    """The captured template with the artifact value replaced. Refuses if no single artifact flag exists."""
    flag = artifact_flag_of(template)
    if flag is None or template.index(flag) + 1 >= len(template):
        raise ValueError("launch template has no single artifact argument")
    argv = list(template)
    argv[argv.index(flag) + 1] = str(artifact_path)
    return argv


def same_launch(live_argv, template):
    """True when the live command line equals the captured template in every position except the artifact value."""
    flag = artifact_flag_of(template)
    if flag is None or len(live_argv) != len(template):
        return False
    skip = template.index(flag) + 1
    return all(a == b for i, (a, b) in enumerate(zip(live_argv, template)) if i != skip)


def auth_config(cfg):
    """The configuration an authorization file covers. The artifact VALUE is masked, so promotions do not
    change the authorization; the flag, port, working directory and every other argument are bound."""
    template = expected_argv(cfg["argv"], "{artifact}")
    return {"restart": "production-process", "port": int(cfg["port"]), "argv": template, "cwd": cfg["cwd"]}


def capture(port, journal=None):
    """Read-only: the launch template of the process listening on the port, or a refusal reason."""
    pid = _listener_pid(port)
    if pid is None:
        return None, "no single listener on the port"
    if _cgroup_is_systemd(pid):
        return None, "process is systemd-managed; manual restart is refused"
    argv = _cmdline(pid)
    if artifact_flag_of(argv) is None:
        return None, "launch command has no single artifact argument"
    cwd = os.readlink(f"/proc/{pid}/cwd")
    if journal is not None and str(journal) not in " ".join(argv) and "--journal" in argv:
        return None, "launch command names a different journal"
    flag = artifact_flag_of(argv)
    artifact = argv[argv.index(flag) + 1]
    return {"port": port, "argv": argv, "cwd": cwd, "pid_at_capture": pid,
            "artifact_flag": flag, "artifact_path": artifact}, "captured"


class ProductionRestarter:
    def __init__(self, config, env_file, log_file):
        self.config = config                          # {"port", "argv", "cwd"} from capture()
        self.env_file = Path(env_file)
        self.log_file = Path(log_file)
        self.restarts = 0

    def _env(self):
        data = json.loads(self.env_file.read_text())
        return {k: v for k, v in data.items()}

    def _stop(self, pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(TERM_WAIT_S * 10):
            if _exited(pid):
                return True
            time.sleep(0.1)
        return False                                   # no force-kill: refuse and report

    def can_restart(self):
        """Read-only: True only when the port's single listener is exactly the captured launch."""
        pid = _listener_pid(self.config["port"])
        if pid is None:
            return False
        try:
            return not _cgroup_is_systemd(pid) and same_launch(_cmdline(pid), self.config["argv"])
        except OSError:
            return False

    def __call__(self, artifact_path):
        pid = _listener_pid(self.config["port"])
        if pid is None:
            return False
        if _cgroup_is_systemd(pid) or not same_launch(_cmdline(pid), self.config["argv"]):
            return False                               # not the process we captured: do nothing
        if not self._stop(pid):
            return False
        argv = expected_argv(self.config["argv"], artifact_path)
        with open(self.log_file, "ab") as log:
            subprocess.Popen(argv, cwd=self.config["cwd"], env=self._env(), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
        self.restarts += 1
        for _ in range(START_WAIT_S * 10):
            if _listener_pid(self.config["port"]) is not None:
                return True
            time.sleep(0.1)
        return False


def _main(argv=None):
    """Read-only capture of the live launch into a restart configuration (private files, no secrets printed)."""
    import argparse
    ap = argparse.ArgumentParser(description="capture the live launch (read-only)")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--config-out", type=Path, required=True)
    ap.add_argument("--env-out", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.port in (8765, 8767):
        raise SystemExit("refused: retired port")
    captured, reason = capture(args.port)
    if captured is None:
        raise SystemExit("capture refused: " + reason)
    raw = Path(f"/proc/{captured['pid_at_capture']}/environ").read_bytes().split(b"\0")
    env = dict(kv.decode(errors="replace").split("=", 1) for kv in raw if b"=" in kv)
    args.env_out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.env_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(env, f)
    os.chmod(args.env_out, 0o600)                     # explicit: never rely on the creation mode
    config = {"kind": "production", "port": captured["port"], "argv": captured["argv"], "cwd": captured["cwd"],
              "artifact_flag": captured["artifact_flag"], "artifact_path": captured["artifact_path"],
              "env_file": str(args.env_out), "log": str(args.log)}
    args.config_out.write_text(json.dumps(config, indent=1, sort_keys=True))
    os.chmod(args.config_out, 0o600)
    print(json.dumps({"captured": True, "port": captured["port"], "argv_length": len(captured["argv"]),
                      "cwd": captured["cwd"], "environment_values": "not printed"}))


if __name__ == "__main__":
    _main()
