"""As-of, coverage, bounded-memory and unit safety using synthetic data only."""
from datetime import datetime, timedelta, timezone
import math

import pytest

from src.signals_display.intelligence import HORIZONS, MarketIntelligence, technical_context


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def observe(engine, seconds, *, source="Coinbase", **extra):
    at = NOW + timedelta(seconds=seconds)
    return engine.ingest(source, 100000 + seconds, at, at, **extra)


def test_subsecond_window_requires_real_subsecond_coverage():
    stream = MarketIntelligence()
    for i in range(101):
        observe(stream, i / 10)
    summary = stream.summarize(NOW + timedelta(seconds=10), primary_source="Coinbase")
    window = summary["sources"]["Coinbase"]["windows"]["500ms"]
    assert window["status"] == "READY"
    assert window["features"]["velocity_dollars_per_second"] == pytest.approx(1.0)
    assert summary["candidate_features"]["micro_500ms_return_fraction"] is not None
    assert summary["probability_adjustment"] == 0
    rest = MarketIntelligence()
    for i in range(11):
        observe(rest, i)
    slow = rest.summarize(NOW + timedelta(seconds=10))["sources"]["Coinbase"]["windows"]
    assert slow["500ms"]["status"] != "READY"
    assert slow["5s"]["status"] == "READY"
    assert slow["5s"]["mean_observation_interval_seconds"] == 1


def test_unknown_trade_volume_direction_and_quote_depth_are_not_zero():
    engine = MarketIntelligence()
    for i in range(11):
        observe(engine, i)
    result = engine.summarize(NOW + timedelta(seconds=10))["sources"]["Coinbase"]["windows"]["5s"][
        "features"
    ]
    for key in (
        "trade_volume",
        "trade_flow_imbalance",
        "trade_frequency_per_second",
        "spread",
        "depth_imbalance",
        "price_impact_per_net_volume",
    ):
        assert result[key] is None
    assert result["velocity_dollars_per_second"] == 1


def test_trades_deduplicate_and_preserve_units_and_known_fees_absence():
    engine = MarketIntelligence()
    for i in range(11):
        kwargs = dict(
            kind="trade",
            trade_id=i,
            volume=2.0,
            aggressor="buy" if i % 2 == 0 else "sell",
            bid=100000 + i - 1,
            ask=100000 + i + 1,
            bid_depth=3.0,
            ask_depth=1.0,
        )
        assert observe(engine, i, **kwargs)
        assert not observe(engine, i, **kwargs)
    source = engine.summarize(NOW + timedelta(seconds=10))["sources"]["Coinbase"]
    result = source["windows"]["5s"]["features"]
    assert result["trade_volume"] == 12
    assert result["trade_flow_imbalance"] == 0
    assert result["depth_imbalance"] == 0.5
    assert result["spread"] == 2
    assert result["price_impact_per_net_volume"] is None
    anonymous = MarketIntelligence()
    for i in range(11):
        observe(anonymous, i, kind="trade", volume=2.0, aggressor="buy")
    assert (
        anonymous.summarize(NOW + timedelta(seconds=10))["sources"]["Coinbase"]["windows"]["5s"][
            "features"
        ]["trade_volume"]
        is None
    )


def test_unknown_one_trade_quantity_or_direction_withholds_whole_window_flow():
    engine = MarketIntelligence()
    for i in range(11):
        observe(
            engine, i, kind="trade", trade_id=i, volume=None if i == 8 else 1.0, aggressor="buy"
        )
    result = engine.summarize(NOW + timedelta(seconds=10))["sources"]["Coinbase"]["windows"]["5s"][
        "features"
    ]
    assert result["trade_volume"] is None and result["trade_flow_imbalance"] is None
    assert result["trade_frequency_per_second"] is not None


def test_asof_summary_never_reads_later_bin_observations():
    engine = MarketIntelligence()
    for i in range(101):
        observe(engine, i / 10)
    cutoff = NOW + timedelta(seconds=5)
    before = engine.summarize(cutoff)
    engine.ingest("Coinbase", 999999, NOW + timedelta(seconds=11), NOW + timedelta(seconds=11))
    after = engine.summarize(cutoff)
    assert before["sources"]["Coinbase"]["windows"] == after["sources"]["Coinbase"]["windows"]
    assert after["sources"]["Coinbase"]["latest_price"] <= 100005
    assert after["sources"]["Coinbase"]["event_to_receipt_seconds"] is None


def test_gaps_and_reconnections_restart_continuity_without_erasing_history():
    engine = MarketIntelligence()
    for i in range(31):
        observe(engine, i, session_id="one")
    assert (
        engine.summarize(NOW + timedelta(seconds=30))["sources"]["Coinbase"]["windows"]["30s"][
            "status"
        ]
        == "READY"
    )
    observe(engine, 31, session_id="two", gap_detected=True, missing_updates_count=3)
    source = engine.summarize(NOW + timedelta(seconds=31))["sources"]["Coinbase"]
    assert source["windows"]["30s"]["status"] == "WARMING UP"
    assert source["reported_dropped_updates"] == 3
    assert source["retained_fine_bins"] > 30
    # A later reconnect never contaminates an earlier as-of view.
    assert (
        engine.summarize(NOW + timedelta(seconds=30))["sources"]["Coinbase"]["windows"]["30s"][
            "status"
        ]
        == "READY"
    )
    for i in range(32, 70):
        observe(engine, i, session_id="two")
    observe(engine, 80, session_id="two")
    assert (
        engine.summarize(NOW + timedelta(seconds=80))["sources"]["Coinbase"]["windows"]["1m"][
            "status"
        ]
        != "READY"
    )


def test_stale_book_does_not_ride_alongside_fresh_trade_prices():
    engine = MarketIntelligence()
    observe(engine, 0, bid=99999, ask=100001, bid_depth=2, ask_depth=2)
    for i in range(1, 31):
        observe(engine, i)
    result = engine.summarize(NOW + timedelta(seconds=30))["sources"]["Coinbase"]["windows"]["30s"][
        "features"
    ]
    assert result["spread"] is None and result["depth_imbalance"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"price": float("nan")},
        {"bid": 1},
        {"bid": 3, "ask": 2},
        {"bid_depth": 1},
        {"volume": -1},
        {"aggressor": "guessed"},
        {"received_mono": 2, "processed_mono": 1},
    ],
)
def test_invalid_observations_are_rejected(extra):
    kwargs = dict(source="Coinbase", price=100000, event_at=NOW, received_at=NOW)
    kwargs.update(extra)
    with pytest.raises(ValueError):
        MarketIntelligence().ingest(**kwargs)


def test_event_and_receipt_clocks_are_distinct_and_not_network_latency():
    engine = MarketIntelligence()
    engine.ingest(
        "Coinbase",
        100000,
        NOW - timedelta(milliseconds=10),
        NOW,
        received_mono=3.0,
        processed_mono=3.002,
    )
    source = engine.summarize(NOW)["sources"]["Coinbase"]
    assert source["event_to_receipt_seconds"] == 0.01
    assert source["processing_latency_ms"] == pytest.approx(2.0)
    assert source["network_latency_ms"] is None
    with pytest.raises(ValueError, match="future-dated"):
        engine.ingest("Coinbase", 100001, NOW + timedelta(seconds=1), NOW)
    with pytest.raises(ValueError, match="include timezone"):
        engine.ingest("Coinbase", 100001, None, datetime(2026, 1, 1))


def candles(count=1440):
    result = []
    for i in range(count):
        price = 100000 + i + 3 * math.sin(i * 0.2)
        result.append(
            dict(
                start=int(NOW.timestamp()) - 60 * (count - i),
                open=price,
                close=price + 1,
                high=price + 3,
                low=price - 3,
                volume=1 + i % 3,
            )
        )
    return dict(source="Synthetic Coinbase", received_at=NOW.isoformat(), candles=result)


def test_closed_candle_analytics_are_past_only_and_context_not_probability():
    history = candles()
    result = technical_context(history, NOW)
    assert result["status"] == "READY" and result["closed_bars"] == 1440
    assert result["features"]["atr_14"] > 0
    assert 0 <= result["features"]["rsi_14"] <= 100
    assert result["features"]["return_4h"] > 0
    history["candles"].append(
        dict(start=int(NOW.timestamp()), open=1, close=1, high=1, low=1, volume=99999)
    )
    assert technical_context(history, NOW) == result
    history["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert technical_context(history, NOW)["status"] == "UNAVAILABLE"
    history = candles()
    history["candles"].pop(100)
    assert technical_context(history, NOW)["status"] == "UNAVAILABLE"


def test_source_and_24h_memory_are_bounded_under_faster_than_500ms_inputs():
    engine = MarketIntelligence(max_sources=2)
    # Twenty updates per second over ten minutes must not retain 12,000 ticks.
    for i in range(12001):
        observe(engine, i / 20, kind="trade", trade_id=i, volume=1.0, aggressor="buy")
    source = engine.summarize(NOW + timedelta(seconds=600))["sources"]["Coinbase"]
    assert source["retained_fine_bins"] <= 602
    assert source["retained_minute_bins"] <= 12
    assert len(engine.sources["Coinbase"].ids) <= 4096
    engine.ingest("Kraken", 100000, NOW, NOW)
    with pytest.raises(ValueError, match="Maximum source"):
        engine.ingest("third", 100000, NOW, NOW)
    assert len(HORIZONS) == 13
