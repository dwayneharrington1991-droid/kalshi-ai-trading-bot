#!/usr/bin/env python3
"""One-command signals dashboard. Never imports an execution client."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
RUNTIME_PACKAGES = (
    "httpx==0.27.0",
    "websockets==16.1.1",
    "cryptography==42.0.0",
)


def healthy(timeout=2):
    """A responsive state endpoint proves process health, not market freshness."""
    try:
        with urlopen("http://127.0.0.1:8765/healthz", timeout=timeout) as response:
            return response.status == 200 and response.headers.get_content_type() == "application/json"
    except Exception:
        return False


def forwarded_url(environment=None):
    environment = environment or os.environ
    name = environment.get("CODESPACE_NAME")
    domain = environment.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    return f"https://{name}-8765.{domain}/" if name and domain else None


def open_dashboard():
    url = forwarded_url()
    if not url:
        print("Open Codespaces Ports > KXBTC15M signals-only dashboard > Open in Browser.", flush=True)
        return False
    try:
        subprocess.Popen(
            ["code", "--open-url", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("Opening the private Codespaces dashboard preview.", flush=True)
        return True
    except OSError:
        print("Open Codespaces Ports > KXBTC15M signals-only dashboard > Open in Browser.", flush=True)
        return False


def _run_checked(command, purpose, **kwargs):
    """Run bootstrap work with a short, actionable failure instead of a traceback."""
    try:
        return subprocess.run(command, check=True, **kwargs)
    except FileNotFoundError as error:
        raise RuntimeError(f"{purpose} failed because {command[0]} is unavailable: {error}") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"{purpose} failed with exit code {error.returncode}. "
            "Check the launcher terminal for the package installer error, then click 🔄 RESTART MY BOT."
        ) from error


def environment_works(python):
    """Validate the interpreter and every dependency used by the signals server."""
    if not python.is_file() or not os.access(python, os.X_OK):
        return False
    check = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import httpx, cryptography; "
                "from websockets.sync.client import connect; "
                "from importlib.metadata import version; "
                "assert int(version('websockets').split('.')[0]) >= 16"
            ),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return check.returncode == 0


def prepare_environment(venv=VENV, system_python=None):
    """Create or repair a Codespaces venv whose base interpreter may have changed."""
    system_python = system_python or Path(sys.executable)
    python = venv / "bin/python3"
    if environment_works(python):
        print("Python environment is ready.", flush=True)
        return python

    # A Codespaces rebuild can preserve .venv while replacing its absolute base-python
    # symlink. Recreate only this generated environment; journals and configuration live
    # elsewhere and are never touched here.
    if venv.exists() or venv.is_symlink():
        print("Python environment is missing or broken; rebuilding it safely.", flush=True)
        if venv.is_symlink() or venv.is_file():
            venv.unlink()
        else:
            shutil.rmtree(venv)
    else:
        print("Creating the Python environment for this Codespace.", flush=True)
    _run_checked(
        [str(system_python), "-m", "venv", str(venv)],
        "Python environment creation",
    )
    if not python.is_file():
        raise RuntimeError(f"Python environment creation did not produce {python}")
    print("Installing dashboard dependencies (first launch can take a minute).", flush=True)
    _run_checked(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", *RUNTIME_PACKAGES],
        "Dashboard dependency installation",
    )
    if not environment_works(python):
        raise RuntimeError("Dashboard dependencies installed but the environment validation failed.")
    print("Python environment is ready.", flush=True)
    return python


def dashboard_pids(root=ROOT, proc_root=Path("/proc"), own_pid=None):
    """Find only this checkout's signals server on port 8765."""
    own_pid = os.getpid() if own_pid is None else own_pid
    matches = []
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == own_pid:
            continue
        try:
            parts = (proc / "cmdline").read_bytes().decode().split("\0")
            if (proc / "cwd").resolve() != root.resolve():
                continue
            if "-m" not in parts or parts[parts.index("-m") + 1] != "src.signals_display.server":
                continue
            port = parts[parts.index("--port") + 1] if "--port" in parts else "8765"
            if port == "8765":
                matches.append(int(proc.name))
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
    return matches


def launcher_pids(root=ROOT, proc_root=Path("/proc"), own_pid=None):
    """Find only this checkout's dashboard supervisor, never another task."""
    own_pid = os.getpid() if own_pid is None else own_pid
    matches = []
    expected = str((root / "scripts/start_dashboard.py").resolve())
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == own_pid:
            continue
        try:
            parts = [part for part in (proc / "cmdline").read_bytes().decode().split("\0") if part]
            if (proc / "cwd").resolve() != root.resolve():
                continue
            scripts = [str((root / part).resolve()) if not Path(part).is_absolute() else part for part in parts]
            if expected in scripts:
                matches.append(int(proc.name))
        except (OSError, UnicodeError):
            continue
    return matches


def stop_child(child, timeout=15):
    """Stop one supervised signals server without touching any unrelated process."""
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGTERM)
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def supervise(command, *, open_browser=False, health=healthy, popen=subprocess.Popen):
    """Keep the read-only dashboard alive while this Codespaces task is running."""
    stopping = False

    def requested_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    old_term = signal.signal(signal.SIGTERM, requested_stop)
    failures = 0
    opened = False
    try:
        while not stopping:
            child = popen(command, cwd=ROOT)
            started = time.monotonic()
            unhealthy = 0
            while not stopping and child.poll() is None:
                if health():
                    unhealthy = 0
                    failures = 0
                    if open_browser and not opened:
                        open_dashboard()
                        opened = True
                elif time.monotonic() - started > 30:
                    unhealthy += 1
                    if unhealthy >= 3:
                        print(
                            "Dashboard health checks failed repeatedly; restarting safely.",
                            flush=True,
                        )
                        stop_child(child)
                        break
                time.sleep(5)
            if stopping:
                stop_child(child)
                return 0
            failures += 1
            delay = min(60, 2 ** min(failures, 6))
            print(
                f"Dashboard stopped unexpectedly; automatic restart in {delay}s "
                f"(exit {child.poll()}).",
                flush=True,
            )
            deadline = time.monotonic() + delay
            while not stopping and time.monotonic() < deadline:
                time.sleep(0.25)
        return 0
    except KeyboardInterrupt:
        stopping = True
        stop_child(child)
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Stop only dashboard servers from this checkout on port 8765",
    )
    parser.add_argument(
        "--private-dashboard",
        action="store_true",
        help="Confirm port 8765 is Private in Codespaces Ports",
    )
    parser.add_argument("--public-only", action="store_true")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create/repair the Python environment without starting the dashboard",
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    lock = open(ROOT / ".dashboard-launch.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if args.restart:
            supervisors = launcher_pids()
            print(
                f"Stopping {len(supervisors)} existing dashboard supervisor(s) for an explicit restart.",
                flush=True,
            )
            for pid in supervisors:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 20
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        parser.exit(1, "Existing dashboard supervisor did not stop; stop its task terminal and retry.\n")
                    time.sleep(0.1)
        elif healthy():
            print("Dashboard supervisor is already running; reusing it.", flush=True)
            if args.open_browser:
                open_dashboard()
            return 0
        else:
            parser.exit(1, "Dashboard supervisor is already recovering the server; try again shortly.\n")
    try:
        python = prepare_environment()
    except RuntimeError as error:
        parser.exit(1, f"Launcher setup error: {error}\nNo journal or credential was changed.\n")
    if args.prepare_only:
        return 0
    matches = dashboard_pids()
    print(f"Verified dashboard servers on port 8765 in this checkout: {len(matches)}", flush=True)
    if matches and not args.restart and healthy():
        print("Healthy dashboard already running; reusing it without creating another process.", flush=True)
        if args.open_browser:
            open_dashboard()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        return 0
    if matches and not args.restart:
        print("Existing dashboard is unresponsive; restarting it safely.", flush=True)
    for pid in matches:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15

    def running(pid):
        try:
            return Path("/proc", str(pid), "stat").read_text().split(") ", 1)[1][0] != "Z"
        except OSError:
            return False

    while any(running(pid) for pid in matches):
        if time.monotonic() > deadline:
            parser.exit(
                1,
                "Old dashboard has not stopped. No database was deleted; stop its terminal with Ctrl+C.\n",
            )
        time.sleep(0.1)
    configured = bool(
        os.environ.get("KALSHI_MONITOR_KEY_ID")
        and (
            os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PEM")
            or os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PATH")
        )
    )
    account = configured and not args.public_only
    private = False
    if os.environ.get("CODESPACE_NAME"):
        try:
            result = subprocess.run(
                [
                    "gh",
                    "codespace",
                    "ports",
                    "visibility",
                    "8765:private",
                    "-c",
                    os.environ["CODESPACE_NAME"],
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            private = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    if account:
        if not private and not args.private_dashboard:
            parser.exit(
                1,
                "Set Ports > 8765 > Port Visibility > Private, then rerun with --private-dashboard.\n",
            )
        if not private:
            print(
                "Port privacy not API-verified: relying on your --private-dashboard confirmation.",
                flush=True,
            )
    command = [
        str(python),
        "-m",
        "src.signals_display.server",
        "--mode",
        "public",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
        "--journal",
        "signals_monitor.db",
        "--processing-ms",
        "250",
        "--stop",
        "0.10",
    ]
    if account:
        command += [
            "--account-readonly",
            "--private-dashboard",
            "--require-readonly-scope",
            "--account-store",
            "signals_account_monitor.db",
        ]
    print(
        "Starting "
        + (
            "authenticated monitoring; broker read-only permissions must verify"
            if account
            else "public data only; account not configured/enabled"
        )
        + ". Journals preserved.",
        flush=True,
    )
    print("Open Codespaces Ports > 8765 > Open in Browser. Keep visibility Private.", flush=True)
    # Hold the launcher lock for the supervisor lifetime. Repeated clicks reuse
    # this owner and cannot race an automatic restart into a duplicate process.
    try:
        return supervise(command, open_browser=args.open_browser)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
