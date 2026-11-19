"""The Oracle status diagnostic must stay strictly read-only and bounded."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "deploy/oracle/oracle_status_readonly.sh"
STEPS = ROOT / "deploy/oracle/oracle_status_steps.py"


def _code(path):
    return "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))


def test_syntax_and_every_step_is_individually_bounded():
    assert subprocess.run(["bash", "-n", str(SHELL)]).returncode == 0
    code = _code(SHELL)
    for name in ("processes", "dashboards", "clients", "journal"):
        assert re.search(rf"step \d+ {name}", code), name
    assert "timeout -k 3 120 bash" in code and "retire_service_8769.sh\" check" in code
    assert "preview_gates_8768.py\" preflight" in code


def test_never_changes_anything_or_prints_secrets():
    for path in (SHELL, STEPS):
        code = _code(path)
        for forbidden in (r"\bkill\b", r"systemctl\s+(start|stop|restart)", r"/environ", r"\bmv\b",
                          r"INSERT\s", r"UPDATE\s", r"DELETE\s", r"DROP\s", r"VACUUM", r"Journal\(path",
                          r"method=.POST", r"place_order|create_order|cancel_order", r"retire_service_8769.sh\" retire"):
            assert not re.search(forbidden, code), (path.name, forbidden)
    steps = _code(STEPS)
    assert "Journal.__new__(Journal)" in steps and "j.read()" in steps


def test_production_collector_diagnostic_is_read_only():
    code = _code(ROOT / "deploy/oracle/prod_8765_collector_diag.sh")
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy/oracle/prod_8765_collector_diag.sh")]).returncode == 0
    for forbidden in (r"\bkill\b", r"systemctl", r"/environ", r"\brm\b", r"\bmv\b", r"INSERT\s", r"UPDATE\s",
                      r"DELETE\s", r"BEGIN IMMEDIATE", r"VACUUM", r"PRAGMA\s+(?!query_only)", r"nohup", r"setsid"):
        assert not re.search(forbidden, code), forbidden
    assert "mode=ro" in code and "/proc/locks" in code


def test_production_recovery_check_is_read_only():
    path = ROOT / "deploy/oracle/prod_8765_recovery_check.sh"
    code = _code(path)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    for forbidden in (r"\bkill\b", r"systemctl\s+(start|stop|restart|edit|set-property|daemon-reload)", r"/environ",
                      r"\brm\b", r"\bmv\b", r"INSERT\s", r"UPDATE\s", r"DELETE\s", r"BEGIN", r"VACUUM",
                      r"wal_checkpoint", r"journal_mode\s*=", r"nohup", r"--vacuum", r"--rotate"):
        assert not re.search(forbidden, code), forbidden
    assert "mode=ro" in code and "PRAGMA query_only=ON" in code


def test_8768_audit_is_read_only_and_never_prints_environment_values():
    path = ROOT / "deploy/oracle/audit_8768_readonly.sh"
    code = _code(path)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    for forbidden in (r"\bkill\b", r"\bsystemctl\b", r"\brm\b", r"\bmv\b", r"\bcp\b", r"INSERT\s", r"UPDATE\s",
                      r"DELETE\s", r"VACUUM", r"wal_checkpoint", r"journal_mode\s*=", r"nohup", r"git\s+(checkout|reset|pull|merge\s)",
                      r"\bprintenv\b", r"\benv\b\s*$"):
        assert not re.search(forbidden, code, re.M), forbidden
    environ_lines = [line for line in code.splitlines() if "/environ" in line]
    assert environ_lines and all("cut -d= -f1" in line for line in environ_lines)
    assert "j.read()" in code and "AUDIT_8768_COMPLETE" in code


def test_tonight_status_is_read_only():
    path = ROOT / "deploy/oracle/tonight_status_8768.sh"
    code = _code(path)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    for forbidden in (r"kill -(TERM|KILL|STOP|CONT|9|15|19)", r"\bpkill\b", r"renice -n -?[0-9]+ -p \"?\$W",
                      r"systemctl", r"\brm\b", r"\bmv\b", r"\bcp\b", r"INSERT\s", r"UPDATE\s", r"DELETE\s",
                      r"/environ", r"nohup", r"8765.*(stop|restart)"):
        assert not re.search(forbidden, code), forbidden
    assert "renice -n 19 -p $$" in code                 # only lowers its OWN priority
    assert "VERDICT: STARVED" in code and "VERDICT: BLOCKED_ON_DISK" in code


def test_live_verification_is_read_only_and_never_prints_credentials():
    path = ROOT / "deploy/oracle/verify_8768_live.sh"
    code = _code(path)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    for forbidden in (r"\bkill\b", r"systemctl", r"\brm\b", r"\bmv\b", r"\bcat \"\$CFG\"", r"source ", r"INSERT", r"UPDATE ",
                      r"DELETE", r"/environ"):
        assert not re.search(forbidden, code), forbidden
    assert "mode=ro" in code and "values not shown" in code
