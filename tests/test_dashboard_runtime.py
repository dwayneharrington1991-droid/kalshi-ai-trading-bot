from pathlib import Path

import pytest

from src.utils.dashboard_runtime import (
    dashboard_credential_status,
    resolve_dashboard_runtime,
)


def test_dashboard_resolves_relative_key_and_database_from_repository_root(tmp_path):
    key = tmp_path / "credentials" / "account.pem"
    key.parent.mkdir()
    key.write_text("test key", encoding="utf-8")
    environment = {
        "KALSHI_ENVIRONMENT": "production",
        "KALSHI_API_KEY": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": "credentials/account.pem",
        "DB_PATH": "state/trading_system.db",
    }

    runtime = resolve_dashboard_runtime(environment, repository_root=tmp_path)

    assert runtime.environment == "production"
    assert runtime.private_key_path == key.resolve()
    assert runtime.database_path == (tmp_path / "state/trading_system.db").resolve()


def test_dashboard_runtime_rejects_unknown_environment(tmp_path):
    with pytest.raises(RuntimeError, match="environment"):
        resolve_dashboard_runtime(
            {
                "KALSHI_ENVIRONMENT": "staging",
                "KALSHI_PRIVATE_KEY_PATH": "key.pem",
            },
            repository_root=tmp_path,
        )


def test_dashboard_credential_status_reports_presence_only(monkeypatch, tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("test key", encoding="utf-8")
    monkeypatch.setattr("src.utils.dashboard_runtime.REPOSITORY_ROOT", tmp_path)
    environment = {
        "KALSHI_ENVIRONMENT": "production",
        "KALSHI_API_KEY": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
    }

    assert dashboard_credential_status(environment) == {
        "api_key_present": True,
        "private_key_found": True,
    }
