import asyncio

import pytest

from beast_mode_bot import BeastModeBot
from src.config.settings import TradingConfig, settings


def test_reconciliation_feature_is_disabled_and_shadowed_by_default(monkeypatch):
    for name in ("ORDER_RECONCILIATION_ENABLED", "RECONCILIATION_SHADOW_MODE"):
        monkeypatch.delenv(name, raising=False)
    config = TradingConfig()
    assert config.order_reconciliation_enabled is False
    assert config.reconciliation_shadow_mode is True


@pytest.mark.asyncio
async def test_periodic_reconciliation_task_cancels_cleanly(monkeypatch):
    class NeverCalledReconciler:
        async def reconcile(self, **kwargs):
            raise AssertionError("canceled task must not reconcile")

    bot = object.__new__(BeastModeBot)
    bot.shutdown_event = asyncio.Event()
    monkeypatch.setattr(settings.trading, "reconciliation_interval_seconds", 60)
    task = asyncio.create_task(
        bot._run_order_reconciliation(NeverCalledReconciler())
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
