"""The one-click updater is fast-forward-only and preserves dirty work."""
import importlib.util
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_updater", ROOT / "scripts/update_dashboard.py")
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)


def test_updater_preserves_dirty_work_and_fast_forwards(tmp_path, monkeypatch):
    remote, seed, checkout = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "checkout"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", "-b", updater.BRANCH, str(seed))
    git(seed, "config", "user.email", "test@example.invalid")
    git(seed, "config", "user.name", "Test")
    (seed / "tracked.txt").write_text("one")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "one")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", updater.BRANCH)
    git(tmp_path, "clone", "--branch", updater.BRANCH, str(remote), str(checkout))
    (seed / "tracked.txt").write_text("two")
    git(seed, "commit", "-am", "two")
    git(seed, "push")
    (checkout / "local.txt").write_text("preserve me")
    monkeypatch.setattr(updater, "ROOT", checkout)
    commit, saved = updater.update_checkout()
    assert saved.startswith("dashboard-auto-preserve-")
    assert len(commit) == 7
    assert (checkout / "tracked.txt").read_text() == "two"
    assert saved in git(checkout, "stash", "list").stdout


def test_tasks_use_safe_updater():
    text = (ROOT / ".vscode/tasks.json").read_text()
    assert text.count("scripts/update_dashboard.py") == 2
