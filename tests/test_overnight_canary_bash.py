import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "scripts" / "start_overnight_canary.sh"
STOP = ROOT / "scripts" / "stop_overnight_canary.sh"
BASH = shutil.which("bash")


def run_start(*args, environment=None):
    if not BASH:
        pytest.skip("bash is unavailable")
    return subprocess.run(
        [BASH, str(START), *args], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=20, check=False,
    )


def test_missing_approval_blocks_startup():
    result = run_start()
    assert result.returncode == 2
    assert "approval" in result.stderr.lower()


def test_incorrect_environment_blocks_startup():
    result = run_start("I_APPROVE_OVERNIGHT_CANARY", "demo")
    assert result.returncode == 2
    assert "production-only" in result.stderr


def test_failed_persistent_reconciliation_blocks_before_launch(tmp_path):
    fake_python = tmp_path / "python"
    counter = tmp_path / "counter"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        f"counter='{counter.as_posix()}'\n"
        "n=$(cat \"$counter\" 2>/dev/null || echo 0); n=$((n+1)); echo $n >\"$counter\"\n"
        "if [[ $n -eq 2 ]]; then\n"
        "  printf 'BALANCE=500\\nRECONCILIATION_HEALTH=HEALTHY\\nCRITICAL_ALERTS=0\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ $n -eq 3 ]]; then echo 'BLOCKED: latest persistent reconciliation failed' >&2; exit 2; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    environment = dict(os.environ)
    environment["CANARY_PYTHON"] = str(fake_python)
    result = run_start("I_APPROVE_OVERNIGHT_CANARY", "production", environment=environment)
    assert result.returncode == 2
    assert "persistent reconciliation failed" in result.stderr


def test_launcher_sets_exact_gates_and_all_canary_limits():
    source = START.read_text(encoding="utf-8")
    required = {
        'KALSHI_ENVIRONMENT="production"',
        'AUTHORITATIVE_LIVE_EXECUTION_ENABLED="true"',
        'LIVE_ORDER_SUBMISSION_KILL_SWITCH="false"',
        'LIVE_TRADING_ENABLED="true"',
        'PRODUCTION_EXECUTION_ACKNOWLEDGEMENT="I_ACKNOWLEDGE_PRODUCTION_ORDER_RISK"',
        'OVERNIGHT_CANARY_MAX_TOTAL_RISK="20"',
        'OVERNIGHT_CANARY_MAX_MARKET_RISK="2"',
        'OVERNIGHT_CANARY_MAX_POSITIONS="5"',
        'OVERNIGHT_CANARY_MAX_REJECTIONS="3"',
        'OVERNIGHT_CANARY_MAX_DAILY_LOSS="5"',
        'LEVERAGE_ENABLED="false"',
        'MARTINGALE_ENABLED="false"',
        'AUTO_POSITION_SIZE_INCREASE_ENABLED="false"',
    }
    assert all(value in source for value in required)
    assert "CRITICAL_ALERTS=0" in source
    assert "RECONCILIATION_HEALTH=HEALTHY" in source
    assert "BALANCE=-?" in source


@pytest.mark.skipif(os.name == "nt" or not BASH, reason="requires Linux /proc and bash")
def test_stop_script_terminates_recorded_canary_pid(tmp_path):
    process = subprocess.Popen(
        [BASH, "-c", "exec -a kalshi-overnight-canary sleep 30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pid_file = tmp_path / "canary.pid"
    pid_file.write_text(str(process.pid), encoding="ascii")
    environment = dict(os.environ)
    environment["KALSHI_CANARY_PID_FILE"] = str(pid_file)
    try:
        result = subprocess.run(
            [BASH, str(STOP)], cwd=ROOT, env=environment,
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.returncode == 0
        process.wait(timeout=3)
        assert not pid_file.exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=3)
