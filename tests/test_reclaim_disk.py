"""Guarded disk reclaim: only data-free categories; databases/backups never touched."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/reclaim_disk.sh"
CODE = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))


def test_syntax_and_approval_gate():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert '${2:-} == --approve && ${3:-} == RECLAIM' in CODE
    assert "REFUSED: projected free below target" in CODE


def test_only_allowlisted_removals_and_databases_excluded():
    removals = re.findall(r"rm -rf -- (\S+)", CODE)
    assert sorted(removals) == sorted(['"$HOME_DIR/.cache/pip"', '"$s"'])   # pip cache, staging dirs
    assert "*.db|*.db-wal|*.db-shm|*.gz) continue" in CODE
    assert '-path "$HOME_DIR/kalshi-backups" -prune' in CODE
    assert 'fuser -s "$f"' in CODE and "gzip -9 --" in CODE          # open logs left alone; content kept
    assert ".kalshi-preview-stage.*" in CODE and "-mtime +1" in CODE
    for forbidden in (r"\btruncate\b", r"> *\"\$f\"", r"VACUUM", r"sqlite3", r"DELETE", r"kill\b", r"systemctl\s+(stop|restart)"):
        assert not re.search(forbidden, CODE), forbidden


def test_preflight_disk_floor_matches_the_switch():
    gates = (ROOT / "deploy/oracle/preview_gates_8768.py").read_text()
    switch = (ROOT / "deploy/oracle/switch_preview_8768.sh").read_text()
    assert '"min_disk_free_mib": 10240' in gates and "MIN_FREE_BYTES=$((10 * 1024 * 1024 * 1024))" in switch
