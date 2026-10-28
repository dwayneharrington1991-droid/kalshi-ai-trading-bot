"""Launcher tests never start the bot, access credentials, or call a network."""
import importlib.util
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dashboard_launcher", ROOT / "scripts/start_dashboard.py"
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def test_codespaces_forwarded_url_is_https_and_never_localhost():
    value = launcher.forwarded_url({
        "CODESPACE_NAME": "mysterious-haunting",
        "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev",
    })
    assert value == "https://mysterious-haunting-8765.app.github.dev/"
    assert launcher.forwarded_url({}) is None


def test_process_detection_is_checkout_port_and_module_scoped(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()

    def fake(pid, cwd, args):
        item = proc / str(pid)
        item.mkdir()
        (item / "cmdline").write_bytes("\0".join(args).encode() + b"\0")
        (item / "cwd").symlink_to(cwd, target_is_directory=True)

    fake(100, ROOT, ["python", "-m", "src.signals_display.server", "--port", "8765"])
    fake(101, ROOT, ["python", "-m", "src.signals_display.server", "--port", "9999"])
    fake(102, tmp_path, ["python", "-m", "src.signals_display.server", "--port", "8765"])
    fake(103, ROOT, ["python", "some_trading_bot.py", "--port", "8765"])
    assert launcher.dashboard_pids(ROOT, proc, own_pid=999) == [100]


def test_tasks_and_port_configuration_are_private_and_idempotent():
    tasks = json.loads((ROOT / ".vscode/tasks.json").read_text())
    by_name = {task["label"]: task for task in tasks["tasks"]}
    launch = by_name["🚀 OPEN MY BOT"]
    restart = by_name["🔄 RESTART MY BOT"]
    assert "--restart" not in launch["command"]
    assert "--restart" in restart["command"]
    assert "--open-browser" in launch["command"]
    assert launch["runOptions"]["instanceLimit"] == 1
    config = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())
    assert 8765 in config["forwardPorts"]
    port = config["portsAttributes"]["8765"]
    assert port["visibility"] == "private"
    assert port["protocol"] == "http"
    assert port["onAutoForward"] == "openBrowser"
    assert config["postCreateCommand"] == "python3 scripts/start_dashboard.py --prepare-only"


def test_environment_works_rejects_broken_python_symlink(tmp_path):
    python = tmp_path / "python3"
    python.symlink_to(tmp_path / "base-python-removed-by-rebuild")
    assert launcher.environment_works(python) is False


def test_prepare_environment_recreates_broken_venv_and_installs_dependencies(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    binary = venv / "bin/python3"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(tmp_path / "old-codespaces-python")
    calls = []

    monkeypatch.setattr(launcher, "environment_works", lambda value: value == binary and len(calls) == 2)

    def fake_run(command, purpose, **kwargs):
        calls.append((command, purpose))
        if purpose == "Python environment creation":
            binary.parent.mkdir(parents=True)
            binary.write_text("replacement")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher, "_run_checked", fake_run)
    result = launcher.prepare_environment(venv, Path("/usr/bin/python3"))
    assert result == binary
    assert calls[0][0] == ["/usr/bin/python3", "-m", "venv", str(venv)]
    assert calls[1][0][0:4] == [str(binary), "-m", "pip", "install"]
    assert calls[1][0][-3:] == list(launcher.RUNTIME_PACKAGES)


def test_open_dashboard_uses_codespaces_url(monkeypatch):
    monkeypatch.setenv("CODESPACE_NAME", "mysterious-haunting")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **kwargs: calls.append(command))
    assert launcher.open_dashboard() is True
    assert calls == [["code", "--open-url", "https://mysterious-haunting-8765.app.github.dev/"]]
