from datetime import datetime, timezone

import pytest

from src.signals_display.risk_controls import daily_drawdown, signal_kill_switch


NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def account(**overrides):
    value = {
        "status": "READ_ONLY_CONNECTED",
        "available_balance_dollars": 970.0,
        "completed_trades": [
            {
                "closed_at": "2026-11-04T10:00:00+00:00",
                "realized_pnl": -30.0,
            }
        ],
    }
    value.update(overrides)
    return value


def test_three_percent_drawdown_warns_without_execution_capability():
    result = daily_drawdown(account(), NOW)
    assert result["status"] == "ESTIMATED"
    assert result["drawdown_fraction"] == pytest.approx(0.03)
    assert result["warning"] is True

    switch = signal_kill_switch(
        {"now": NOW.isoformat(), "stale": False, "account": account()}
    )
    assert switch["active"] is True
    assert "ESTIMATED_DAILY_DRAWDOWN_AT_OR_ABOVE_3_PERCENT" in switch["reasons"]
    assert switch["automatic_execution_allowed"] is False


def test_drawdown_is_unavailable_instead_of_zero_when_account_or_fees_are_missing():
    disconnected = daily_drawdown({"status": "ACCOUNT_NOT_ENABLED"}, NOW)
    assert disconnected["status"] == "UNAVAILABLE"
    assert disconnected["warning"] is False

    missing = daily_drawdown(
        account(completed_trades=[{"closed_at": NOW.isoformat(), "realized_pnl": None}]),
        NOW,
    )
    assert missing["status"] == "UNAVAILABLE"
    assert "unavailable net P&L" in missing["reason"]


def test_operator_stale_and_worker_kill_switches_are_advisory_only():
    result = signal_kill_switch(
        {
            "stale": True,
            "emergency_signal_kill_switch": True,
            "feed_health": {"workers": {"healthy": False}},
            "account": {"status": "ACCOUNT_NOT_ENABLED"},
        }
    )
    assert result["active"] is True
    assert set(result["reasons"]) == {
        "OPERATOR_EMERGENCY_SIGNAL_KILL_SWITCH",
        "STALE_OR_INCOMPLETE_DATA",
        "BACKGROUND_WORKER_UNHEALTHY",
    }
    assert result["scope"] == "Display-signal suppression and warning only"


def test_invalid_drawdown_threshold_is_rejected():
    with pytest.raises(ValueError):
        daily_drawdown(account(), NOW, threshold=0)
