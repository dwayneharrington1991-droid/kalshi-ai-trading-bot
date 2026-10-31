import sqlite3
from types import SimpleNamespace

import pytest

from scripts import preview_persistent_reconciliation as previewer


def safe_environment(tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("test-only")
    return {
        "KALSHI_API_KEY": "test-only",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "KALSHI_ENVIRONMENT": "production",
        "READ_ONLY_ACCOUNT_VALIDATION": "true",
        "AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "false",
        "LIVE_ORDER_SUBMISSION_KILL_SWITCH": "true",
        "RECONCILIATION_SHADOW_MODE": "true",
        "LIVE_TRADING_ENABLED": "false",
    }


@pytest.mark.asyncio
async def test_preview_uses_copy_and_reports_sanitized_diff(tmp_path, monkeypatch):
    source = tmp_path / "ledger.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, state TEXT, raw_response TEXT)")
        db.execute("INSERT INTO orders VALUES (1, 'submitted', 'original secret payload')")
        db.commit()
    original = source.read_bytes()

    async def fake_validation(*, environment, db_path):
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE orders SET state='resting', raw_response='remote secret payload'")
            db.commit()
        return {"reconciliation": SimpleNamespace(status="completed_with_mismatches",
                                                   mismatch_count=1)}

    monkeypatch.setattr(previewer, "run_read_only_validation", fake_validation)
    report = await previewer.preview(source, tmp_path / "backups", safe_environment(tmp_path))

    assert source.read_bytes() == original
    assert report["source_ledger_modified"] is False
    assert report["exchange_writes"] is False
    assert report["change_counts"] == {"orders:update": 1}
    fields = report["proposed_local_changes"][0]["fields"]
    assert fields["state"] == {"before": "submitted", "after": "resting"}
    assert "secret payload" not in str(report)
    assert fields["raw_response"]["before"].startswith("<redacted sha256:")


def test_preview_requires_live_trading_explicitly_disabled(tmp_path):
    environment = safe_environment(tmp_path)
    environment["LIVE_TRADING_ENABLED"] = "true"
    with pytest.raises(previewer.ReadOnlyValidationError):
        previewer._require_preview_safety(environment)
