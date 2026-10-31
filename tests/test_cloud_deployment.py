"""Production packaging stays private, signals-only, and non-destructive."""
import importlib.util
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/hetzner"


def text(name):
    return (DEPLOY / name).read_text()


def load_diagnostic():
    spec = importlib.util.spec_from_file_location(
        "cloud_connectivity", DEPLOY / "connectivity_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_is_loopback_signals_only_and_hardened():
    runner = text("run-signals.sh")
    service = text("kalshi-signals.service")
    assert "src.signals_display.server" in runner
    assert "--host 127.0.0.1" in runner
    assert "--account-readonly --private-dashboard --require-readonly-scope" in runner
    forbidden = ("place_order", "cancel_order", "create_order", "transfer")
    assert not any(term in runner for term in forbidden)
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "Restart=always" in service


def test_caddy_is_bound_to_tailscale_and_not_public_listener():
    caddy = text("Caddyfile")
    assert "bind {$TAILSCALE_IP}" in caddy
    assert "reverse_proxy 127.0.0.1:8765" in caddy
    assert ":80" not in caddy and "0.0.0.0" not in caddy


def test_migration_uses_sqlite_backup_checksums_and_preserves_source():
    prepare = text("prepare_migration.sh")
    importer = text("import_migration.sh")
    assert ".backup" in prepare
    assert "PRAGMA integrity_check" in prepare
    assert "sha256sum -c SHA256SUMS" in importer
    assert "pre-import" in importer
    assert "rm " not in prepare
    assert 'mountpoint -q "$ROOT"' in text("backup.sh")


def test_health_recovery_requires_three_failures():
    health = text("healthcheck.sh")
    assert "failures >= 3" in health
    assert "systemctl try-restart kalshi-signals.service" in health
    assert "age <= 600" in health


def test_all_shell_scripts_parse():
    for path in DEPLOY.glob("*.sh"):
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_connectivity_report_redacts_data_and_identifies_first_failure(monkeypatch, capsys):
    module = load_diagnostic()
    monkeypatch.setattr(module, "rest_checks", lambda: (
        {"kalshi_rest": {"status": "OK"},
         "coinbase_spot": {"status": "FAILED", "error_class": "TimeoutError"}},
        "KXBTC15M-TEST",
    ))
    monkeypatch.setattr(module, "websocket_check", lambda *_args: {"status": "OK"})
    monkeypatch.setattr(module, "account_checks", lambda _ticker: {"status": "SKIPPED"})
    monkeypatch.setattr(module, "local_dashboard", lambda: {"healthz": {"status": "FAILED"}})

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    class TLS(Socket):
        def version(self): return "TLSv1.3"

    class Context:
        def wrap_socket(self, *_args, **_kwargs): return TLS()

    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *_args, **_kwargs:
                        [(None, None, None, None, ("192.0.2.1", 443))])
    monkeypatch.setattr(module.socket, "create_connection", lambda *_args, **_kwargs: Socket())
    monkeypatch.setattr(module.ssl, "create_default_context", lambda: Context())
    assert module.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["first_external_failure"]["stage"] == "rest:coinbase_spot"
    output = json.dumps(report).lower()
    assert "private_key" not in output and "signature" not in output
