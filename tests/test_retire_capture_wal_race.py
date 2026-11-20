"""The 8769 retirement capture tolerates SQLite removing -wal/-shm after the journal gate.

CI 2026-11-20 (run on 28a5435): two rehearsals stopped with "BLOCKED: capture
failed: journal files" because the gate listed db-wal/db-shm, then SQLite
removed them before the capture ran `stat` on the stale list. The capture is
evidence only; it now records the files that still exist and requires only
the main journal.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy/oracle/retire_service_8769.sh").read_text(encoding="utf-8")


def _capture_block():
    start = SCRIPT.index('[[ -f $TARGET_JOURNAL ]] || die "capture failed: journal')
    end = SCRIPT.index('>"$S/journal-files"', start)
    return SCRIPT[start:SCRIPT.index("\n", end)]


def test_capture_filters_the_gate_list_to_files_that_still_exist():
    block = _capture_block()
    assert 'for f in "${JOURNAL_FILES[@]}"; do [[ -e $f ]] && present+=("$f"); done' in block
    assert 'stat -c \'%n %i %s %Y\' -- "${present[@]}"' in block


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("stat") is None, reason="needs bash and GNU stat")
def test_capture_block_runs_when_wal_and_shm_vanished(tmp_path):
    journal = tmp_path / "j.db"
    journal.write_text("x")
    state = tmp_path / "s"
    state.mkdir()
    script = "\n".join([
        "set -u", 'die() { echo "DIE: $*"; exit 3; }',
        f'TARGET_JOURNAL="{journal.as_posix()}"', f'S="{state.as_posix()}"',
        f'JOURNAL_FILES=("{journal.as_posix()}" "{journal.as_posix()}-wal" "{journal.as_posix()}-shm")',
        _capture_block(),
    ])
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    if "stat:" in out.stderr and "invalid" in out.stderr:
        pytest.skip("non-GNU stat")
    assert out.returncode == 0, out.stdout + out.stderr
    lines = (state / "journal-files").read_text().splitlines()
    assert len(lines) == 1 and lines[0].split()[0].endswith("j.db")
