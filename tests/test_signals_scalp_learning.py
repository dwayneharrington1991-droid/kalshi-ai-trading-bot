"""Chronological shadow learning cannot leak future contracts or enable trading."""
from datetime import datetime, timedelta, timezone

from src.signals_display import scalp_learning


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def record(index, *, settlement=False):
    ticker = f"KXBTC15M-{index:04d}"
    at = NOW + timedelta(minutes=15 * index)
    if settlement:
        return {
            "type": "official_settlement",
            "ticker": ticker,
            "available_at": (at + timedelta(minutes=15)).isoformat(),
        }
    return {
        "recorded_at": at.isoformat(),
        "market": {"ticker": ticker},
    }


def metrics(pnl=10, trades=40, lower=0.01):
    return {
        "observed_complete_trades": trades,
        "net_simulated_pnl": pnl,
        "maximum_drawdown": 1,
        "mean_net_pnl_approximate_interval": [lower, 0.10],
    }


def test_scalp_shadow_split_is_chronological_and_never_actionable(monkeypatch):
    records = [item for i in range(90) for item in (record(i), record(i, settlement=True))]
    calls = []

    def fake_evaluate(rows, criteria):
        tickers = {_ticker(row) for row in rows}
        calls.append(tickers)
        return {"strategies": {"candidate": metrics()}}

    def _ticker(row):
        return (row.get("market") or {}).get("ticker") or row.get("ticker")

    monkeypatch.setattr(scalp_learning, "evaluate", fake_evaluate)
    report = scalp_learning.evaluate_chronological(records)
    assert report["split_contracts"] == [54, 18, 18]
    assert report["selection_frozen_before_test"] is True
    assert report["actionable"] is False
    assert report["active_model_changed"] is False
    assert report["chronological_out_of_sample"] is True
    assert report["development_last_available_at"] < report["validation_last_available_at"]
    assert report["validation_last_available_at"] < report["test_first_available_at"]
    assert all(not ({f"KXBTC15M-{i:04d}" for i in range(72, 90)} & call) for call in calls[:-1])
    assert calls[-1] == {f"KXBTC15M-{i:04d}" for i in range(72, 90)}


def test_insufficient_history_cannot_fit_or_promote(monkeypatch):
    monkeypatch.setattr(
        scalp_learning, "evaluate", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())
    )
    report = scalp_learning.evaluate_chronological([record(i) for i in range(89)])
    assert report["status"] == "INSUFFICIENT_FORWARD_SCALP_HISTORY"
    assert report["actionable"] is False
