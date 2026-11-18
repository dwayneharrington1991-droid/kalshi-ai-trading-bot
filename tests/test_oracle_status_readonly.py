"""The Oracle status diagnostic must stay strictly read-only and bounded."""
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/oracle/oracle_status_readonly.sh"
CODE = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))


def test_syntax_and_bounds():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert "timeout -k 5 170" in CODE


def test_never_changes_anything_or_prints_secrets():
    for forbidden in (r"\bkill\b", r"systemctl\s+(start|stop|restart)", r"/environ", r"\brm\b", r"\bmv\b",
                      r"INSERT\s", r"UPDATE\s", r"DELETE\s", r"DROP\s", r"VACUUM", r"Journal\(path",
                      r"method=.POST", r"place_order|create_order|cancel_order"):
        assert not re.search(forbidden, CODE), forbidden
    # Journals are opened only through the read-only connection, without schema setup.
    assert "Journal.__new__(Journal)" in CODE and "j.read()" in CODE
