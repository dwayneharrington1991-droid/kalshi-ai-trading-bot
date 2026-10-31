"""The support report must remain local, bounded, and credential-safe."""
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dashboard_diagnostics", ROOT / "scripts/dashboard_diagnostics.py"
)
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


def test_report_prints_only_credential_presence(monkeypatch, capsys):
    secret_id = "do-not-print-key-id"
    secret_pem = "do-not-print-private-key"
    monkeypatch.setenv("KALSHI_MONITOR_KEY_ID", secret_id)
    monkeypatch.setenv("KALSHI_MONITOR_PRIVATE_KEY_PEM", secret_pem)
    monkeypatch.setattr(diagnostics, "git_value", lambda *args: "test")
    monkeypatch.setattr(diagnostics, "bot_processes", lambda: [])
    monkeypatch.setattr(diagnostics, "health", lambda: {"reachable": False})
    monkeypatch.setattr(diagnostics, "port_status", lambda: {"port": 8765, "listening": False})
    monkeypatch.setattr(diagnostics, "capacity", lambda: {})
    monkeypatch.setattr(
        diagnostics, "database_status", lambda path: {"path": path, "exists": False}
    )
    diagnostics.main()
    output = capsys.readouterr().out
    assert secret_id not in output
    assert secret_pem not in output
    assert '"key_id": true' in output
    assert '"private_key": true' in output
    assert "no API or trading request" in output
