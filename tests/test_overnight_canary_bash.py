import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
START_SH = ROOT / "scripts" / "start_overnight_canary.sh"
START_PS1 = ROOT / "scripts" / "start_overnight_canary.ps1"
STOP = ROOT / "scripts" / "stop_overnight_canary.sh"
BASH = shutil.which("bash")


def test_shell_canary_is_permanently_disabled_before_any_action(tmp_path):
    if not BASH:
        pytest.skip("bash is unavailable")

    marker = tmp_path / "must-not-run"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!/usr/bin/env bash\ntouch '{marker}'\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    environment = dict(os.environ)
    environment["CANARY_PYTHON"] = str(fake_python)
    environment["KALSHI_CANARY_PID_FILE"] = str(tmp_path / "canary.pid")
    result = subprocess.run(
        [BASH, str(START_SH), "I_APPROVE_OVERNIGHT_CANARY", "production"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "permanently disabled" in result.stderr
    assert not marker.exists()
    assert not (tmp_path / "canary.pid").exists()


def test_canary_launchers_contain_no_execution_commands():
    shell = START_SH.read_text(encoding="utf-8")
    powershell = START_PS1.read_text(encoding="utf-8")

    for source in (shell, powershell):
        assert "permanently disabled" in source
        assert "cli.py" not in source
        assert "LIVE_TRADING_ENABLED" not in source
        assert "AUTHORITATIVE_LIVE_EXECUTION_ENABLED" not in source
        assert "LIVE_ORDER_SUBMISSION_KILL_SWITCH" not in source


@pytest.mark.skipif(os.name == "nt" or not BASH, reason="requires Linux /proc and bash")
def test_stop_script_terminates_recorded_canary_pid(tmp_path):
    process = subprocess.Popen(
        [BASH, "-c", "exec -a kalshi-overnight-canary sleep 30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = tmp_path / "canary.pid"
    pid_file.write_text(str(process.pid), encoding="ascii")
    environment = dict(os.environ)
    environment["KALSHI_CANARY_PID_FILE"] = str(pid_file)
    try:
        result = subprocess.run(
            [BASH, str(STOP)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0
        process.wait(timeout=3)
        assert not pid_file.exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=3)
