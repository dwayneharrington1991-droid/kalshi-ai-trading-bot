from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from src.signals_display.monitor import Journal
from src.signals_display.signal_chart import (
    aggregate_candles,
    marker_is_chronological,
    markers_from_state,
)


NOW = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26NOV040815-15"


def candles(count=15):
    first = int(NOW.timestamp()) - count * 60
    return [
        {
            "start": first + i * 60,
            "open": 100 + i,
            "high": 102 + i,
            "low": 99 + i,
            "close": 101 + i,
            "volume": 2 + i,
        }
        for i in range(count)
    ]


def entry_state():
    return {
        "now": NOW.isoformat(),
        "stale": False,
        "market": {"ticker": TICKER},
        "reference": {"price": 70_000},
        "edge_scanner": {
            "actionable": True,
            "validation": {"verified": True},
            "sides": {
                "up": {
                    "side": "up",
                    "status": "VERIFIED_POSITIVE_EDGE",
                    "market_purchase_price": 0.55,
                    "model_probability": 0.65,
                    "net_expected_profit_per_contract": 0.07,
                    "reason": "Reviewed forward edge after costs",
                }
            },
        },
        "decision_intelligence": {
            "components": {
                "risk_guardian": {
                    "status": "MANUAL_REVIEW_ONLY",
                    "blockers": [],
                    "signal_kill_switch": {"active": False},
                }
            }
        },
        "account": {"status": "PUBLIC_ONLY"},
    }


def test_candle_aggregation_is_aligned_and_volume_preserving():
    bars = aggregate_candles(candles(), 300)
    assert len(bars) == 3
    assert bars[0]["source_bars"] == 5
    assert bars[0]["open"] == 100
    assert bars[0]["close"] == 105
    assert bars[0]["volume"] == sum(range(2, 7))
    with pytest.raises(ValueError):
        aggregate_candles(candles(), 15)


def test_entry_marker_requires_verified_edge_and_all_risk_gates():
    state = entry_state()
    marker = markers_from_state(state)[0]
    assert marker["signal_type"] == "BUY UP"
    assert marker["timestamp"] == NOW.isoformat()
    assert marker["contract_price"] == 0.55
    state["decision_intelligence"]["components"]["risk_guardian"]["blockers"] = [
        "STALE_OR_INCOMPLETE_DATA"
    ]
    assert markers_from_state(state) == []


def test_exit_marker_requires_fresh_verified_read_only_position():
    state = entry_state()
    state["edge_scanner"]["actionable"] = False
    state["account"] = {
        "status": "READ_ONLY_CONNECTED",
        "guidance": [
            {
                "ticker": TICKER,
                "side": "down",
                "guidance": "CONSIDER EXIT",
                "alert_id": 9,
                "timestamp": NOW.isoformat(),
                "executable_bid": 0.61,
                "forecast_probability": 0.34,
                "estimated_impact_after_costs": 3.2,
                "reason": "Confirmed cost-adjusted exit threshold",
                "exit_trigger": "STOP",
                "forecast_assessment": "ROBUST_ADVERSE",
            }
        ],
    }
    markers = markers_from_state(state)
    assert [marker["signal_type"] for marker in markers] == ["EXIT DOWN"]
    state["account"]["status"] = "ACCOUNT_STALE"
    assert markers_from_state(state) == []


def test_marker_journal_is_append_only_and_never_repaints(tmp_path):
    journal = Journal(tmp_path / "signals.db")
    state = entry_state()
    assert journal.record_ai_signal_markers(state) == 1
    assert journal.record_ai_signal_markers(state) == 0
    with journal.connect() as db:
        before = db.execute("SELECT payload FROM ai_signal_markers").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE ai_signal_markers SET btc_price=1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM ai_signal_markers")
    with journal.read() as db:
        assert db.execute("SELECT payload FROM ai_signal_markers").fetchone()[0] == before
    assert json.loads(before)["btc_price"] == 70_000


def test_chart_query_is_bounded_and_subminute_is_explicitly_unavailable(tmp_path):
    journal = Journal(tmp_path / "signals.db")
    data = {
        "source": "Coinbase BTC-USD public one-minute candles",
        "received_at": NOW.isoformat(),
        "candles": candles(60),
    }
    journal.record_candles(data, NOW)
    chart = journal.signal_chart(300, limit=30, ticker=TICKER)
    assert chart["status"] == "READY"
    assert len(chart["bars"]) <= 30
    assert chart["non_repainting"] is True
    unavailable = journal.signal_chart(15, ticker=TICKER)
    assert unavailable["status"] == "INTERVAL_UNAVAILABLE"
    assert unavailable["bars"] == []
    assert "no bars are fabricated" in unavailable["reason"]
    assert "sub-minute trades" in unavailable["required_data"]


def test_production_schema_reads_coinbase_decimal_string_payloads(tmp_path):
    """Regression for 13,467 real rows producing zero chart bars on Oracle."""
    journal = Journal(tmp_path / "signals.db")
    real_rows = []
    for bar in candles(180):
        real_rows.append(
            {
                "start": str(bar["start"]),
                "open": str(bar["open"]) + ".125",
                "high": str(bar["high"]) + ".500",
                "low": str(bar["low"]) + ".000",
                "close": str(bar["close"]) + ".250",
                "volume": str(bar["volume"]) + ".75",
                "source": "Coinbase Advanced Trade one-minute candles",
            }
        )
    data = {
        "source": "Coinbase BTC-USD public one-minute candles; Exchange endpoint fills verified gaps",
        "received_at": NOW.isoformat(),
        "candles": real_rows,
    }
    journal.record_candles(data, NOW)

    chart = journal.signal_chart(60, limit=180)
    assert chart["status"] == "READY"
    assert len(chart["bars"]) == 180
    assert chart["latest_bar_start"] == int(real_rows[-1]["start"])
    assert chart["fallback_kind"] is None
    assert chart["journal_persistence_pending"] is False
    assert chart["candle_diagnostics"] == {
        "queried_rows": 180,
        "valid_unique_candles": 180,
        "invalid_payloads": 0,
        "database_path": str(journal.path),
    }
    assert chart["markers"] == []


def test_chart_rejects_millisecond_timestamps_instead_of_guessing_units():
    raw = candles(1)[0]
    raw = {**raw, "start": str(raw["start"] * 1000)}
    assert aggregate_candles([raw], 60) == []


def test_chart_uses_verified_preload_while_first_journal_batch_is_pending(tmp_path):
    journal = Journal(tmp_path / "signals.db")
    preload = {
        "source": "Coinbase BTC-USD public one-minute candles",
        "received_at": NOW.isoformat(),
        "candles": candles(60),
        "diagnostics": {"status": "READY", "missing_bars": 0},
    }
    chart = journal.signal_chart(60, limit=30, candle_fallback=preload)
    assert chart["status"] == "READY"
    assert len(chart["bars"]) == 30
    assert chart["journal_persistence_pending"] is True
    assert "Verified live Coinbase preload" in chart["source"]
    with journal.read() as db:
        assert db.execute("SELECT COUNT(*) FROM btc_candles").fetchone()[0] == 0


def test_marker_performance_uses_only_official_settlements_and_separates_actual_results(tmp_path):
    journal = Journal(tmp_path / "signals.db")
    state = entry_state()
    journal.record_ai_signal_markers(state)
    with journal.connect() as db:
        db.execute(
            "INSERT INTO forecast_outcomes(ticker,origin,observed_at,outcome) VALUES (?,?,?,?)",
            (TICKER, "official", (NOW + timedelta(minutes=16)).isoformat(), 1),
        )
    report = journal.signal_chart(60, ticker=TICKER)["performance"]
    assert report["actionable_signals"] == 1
    assert report["independently_settled_signals"] == 1
    assert report["correct"] == 1
    assert report["directional_accuracy"] == 1
    assert report["counterfactual_after_cost_pnl"] is not None
    assert report["actual_realized_results"] is None


def test_chart_can_seed_from_immutable_opening_snapshot_without_rewriting_history(tmp_path):
    journal = Journal(tmp_path / "signals.db")
    payload = {
        "ticker": TICKER,
        "opens_at": NOW.isoformat(),
        "captured_at": NOW.isoformat(),
        "data_cutoff": NOW.isoformat(),
        "history": {
            "source": "Coinbase frozen journal history",
            "received_at": NOW.isoformat(),
            "candles": candles(60),
        },
        "reference_points": [],
        "reference_source": "test",
        "finalized": True,
    }
    with journal.connect() as db:
        db.execute(
            """INSERT INTO opening_input_snapshots
            (mode,ticker,opens_at,captured_at,finalized,payload)
            VALUES (?,?,?,?,?,?)""",
            ("PUBLIC MARKET DATA", TICKER, NOW.isoformat(), NOW.isoformat(), 1,
             json.dumps(payload)),
        )
    chart = journal.signal_chart(60, limit=30)
    assert chart["status"] == "READY"
    assert len(chart["bars"]) == 30
    assert chart["fallback_kind"] == "IMMUTABLE_OPENING_SNAPSHOT"
    assert chart["latest_bar_start"] == candles(60)[-1]["start"]
    with journal.read() as db:
        assert db.execute("SELECT COUNT(*) FROM btc_candles").fetchone()[0] == 0


def test_marker_chronology_rejects_future_and_late_backfill():
    marker = {"timestamp": NOW.isoformat()}
    assert marker_is_chronological(marker, NOW + timedelta(seconds=5))
    assert not marker_is_chronological(marker, NOW - timedelta(seconds=1))
    assert not marker_is_chronological(marker, NOW + timedelta(seconds=31))


def test_dashboard_contains_incremental_chart_without_external_script():
    page = open("src/signals_display/dashboard.html", encoding="utf-8").read()
    assert 'id="ai-chart-canvas"' in page
    assert "/api/signal-chart" in page
    assert "BUY UP/DOWN refers to Kalshi YES/NO contracts" in page
    assert "lightweight-charts" not in page.lower()
    assert "setInterval(refreshSignalChart,5000)" in page
    assert "interval:signalChart.interval,limit:'180'" in page
    assert "nextLiveRenderAt=performance.now()+500" in page
    assert "BUY UP marker" in page and "BUY DOWN marker" in page
    assert "kind==='BUY UP'" in page and "kind==='BUY DOWN'" in page
    assert "data.forming_candle?' · 1 clearly faded developing proxy candle'" in page
