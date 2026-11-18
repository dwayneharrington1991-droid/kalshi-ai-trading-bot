"""Archive-then-remove of the retired 8769 journal: proves unused, manifest-bound."""
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/archive_8769_journal.sh"
NAME = "repair-test-" + str(8000 + 769) + ".db"


def _env(tmp_path):
    return {**os.environ, "ARCHIVE_DIR": str(tmp_path), "ARCHIVE_FS_PATH": str(tmp_path),
            "ARCHIVE_PYTHON": "python3", "ARCHIVE_PORT": "1"}


def _run(tmp_path, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=120, env=_env(tmp_path))


def _setup(tmp_path):
    db = tmp_path / NAME
    con = sqlite3.connect(db); con.execute("create table t(x)"); con.execute("insert into t values (1)"); con.commit(); con.close()
    (tmp_path / (NAME + ".dashboard.lock")).write_bytes(b"")
    (tmp_path / "active-other.db").write_text("must stay")
    return db


def test_lint_and_never_touches_other_files():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    code = SCRIPT.read_text()
    removals = re.findall(r"rm -f -- (\S+)", code)
    assert removals == ['"$f"'] and 'done <<<"$m"' in code      # only manifest files
    assert 'repair-test-$((8000 + 769)).db' in code              # name never literal in argv


def test_prepare_then_remove_with_matching_token(tmp_path):
    db = _setup(tmp_path)
    prep = _run(tmp_path, "prepare")
    assert prep.returncode == 0, prep.stdout + prep.stderr
    assert "unused: 0 open handles" in prep.stdout
    token = re.search(r"REMOVAL_TOKEN: (\S+)", prep.stdout).group(1)
    assert prep.stdout.count("MANIFEST ") == 2                   # db + lock (no wal/shm)
    assert _run(tmp_path, "remove", "--approve", "REMOVE-8769-0000000000000000").returncode == 2
    assert db.exists()
    done = _run(tmp_path, "remove", "--approve", token)
    assert "removed" in done.stdout and not db.exists()
    assert not (tmp_path / (NAME + ".dashboard.lock")).exists()
    assert (tmp_path / "active-other.db").read_text() == "must stay"


def test_changed_or_open_journal_is_never_removed(tmp_path):
    db = _setup(tmp_path)
    token = re.search(r"REMOVAL_TOKEN: (\S+)", _run(tmp_path, "prepare").stdout).group(1)
    with open(db, "ab") as f:                                     # changed after the copy
        f.write(b"x")
    changed = _run(tmp_path, "remove", "--approve", token)
    assert changed.returncode == 2 and "manifest differs" in changed.stdout and db.exists()
    holder = subprocess.Popen(["python3", "-c", "import sys,time; f=open(sys.argv[1],'rb'); time.sleep(20)", str(db)])
    try:
        time.sleep(1)
        held = _run(tmp_path, "prepare")
        assert held.returncode == 2 and "still open" in held.stdout
    finally:
        holder.kill()
    assert db.exists()
