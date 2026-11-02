"""Actual-volume analytics use mocked observations only; never network/account I/O."""
from datetime import datetime, timedelta, timezone
import math

import pytest

from src.signals_display.advanced_flow import AdvancedFlow, FEATURE_NAMES


NOW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def trade(engine, seconds, price=100.0, volume=1.0, side="buy", source="spot", **kw):
    at = NOW + timedelta(seconds=seconds)
    return engine.observe(
        dict(
            source=source,
            price=price,
            volume=volume,
            aggressor=side,
            received_at=at,
            event_at=at,
            kind="trade",
            trade_id=str(seconds),
            **kw,
        )
    )


def quote(engine, seconds, bid=99.0, ask=101.0, source="spot", **kw):
    at = NOW + timedelta(seconds=seconds)
    return engine.observe(
        dict(
            source=source,
            price=(bid + ask) / 2,
            bid=bid,
            ask=ask,
            received_at=at,
            event_at=at,
            kind="quote",
            **kw,
        )
    )


def summary(engine, seconds=2.0, **kwargs):
    return engine.summarize(NOW + timedelta(seconds=seconds), **kwargs)["sources"]["spot"]


def test_vwap_uses_price_times_actual_quantity_and_deduplicates():
    engine = AdvancedFlow()
    trade(engine, 0, 100, 1)
    trade(engine, 1, 110, 3)
    assert not trade(engine, 1, 110, 3)
    result = summary(engine, 1)
    assert result["vwap"]["rolling"] == pytest.approx(107.5)
    assert result["vwap"]["session"] == pytest.approx(107.5)
    assert result["vwap"]["volume_weighted_momentum"] == 10
    assert result["vwap"]["crossing"] == "ABOVE"
    assert result["order_flow"]["aggressive_buy_volume"] == 4
    assert result["cvd"]["session_net_delta"] == 4
    assert result["vwap"]["session_basis"].startswith("Since this uninterrupted")
    assert result["active_contribution"] is False


def test_quotes_never_fabricate_executed_volume_or_cvd():
    engine = AdvancedFlow()
    quote(engine, 0, bid_depth=100, ask_depth=2)
    quote(engine, 1, bid_depth=200, ask_depth=1)
    result = summary(engine, 1)
    assert result["vwap"]["rolling"] is None
    assert result["volume_profile"]["status"] == "UNAVAILABLE"
    assert result["cvd"]["window_net_delta"] is None
    assert result["order_flow"]["aggressive_buy_volume"] is None
    assert result["microstructure"]["quote_updates_per_second"] == 2
    assert result["microstructure"]["replenishment_quantity"] == 99


@pytest.mark.parametrize("field", ["volume", "trade_id"])
def test_missing_real_quantity_or_id_withholds_volume_families(field):
    engine = AdvancedFlow()
    trade(engine, 0)
    data = dict(
        source="spot",
        price=110.0,
        received_at=NOW + timedelta(seconds=1),
        event_at=NOW + timedelta(seconds=1),
        kind="trade",
        volume=3.0,
        trade_id="second",
        aggressor="buy",
    )
    data[field] = None
    engine.observe(data)
    result = summary(engine, 1)
    assert result["vwap"]["rolling"] is None
    assert result["vwap"]["session"] is None
    assert result["volume_profile"]["status"] == "UNAVAILABLE"
    assert result["cvd"]["session_net_delta"] is None


def test_unknown_aggressor_does_not_invent_cvd_but_allows_vwap():
    engine = AdvancedFlow()
    trade(engine, 0, side=None)
    result = summary(engine, 0)
    assert result["vwap"]["rolling"] == 100
    assert result["cvd"]["session_net_delta"] is None
    assert result["order_flow"]["imbalance"] is None


def test_native_units_and_sources_are_not_pooled_and_primary_is_explicit():
    engine = AdvancedFlow()
    trade(engine, 0, volume=1, quantity_unit="BTC")
    trade(engine, 0, volume=10000, source="perp", quantity_unit="contracts")
    result = engine.summarize(NOW, primary_source="spot")
    assert result["sources"]["spot"]["order_flow"]["aggressive_buy_volume"] == 1
    assert result["sources"]["perp"]["order_flow"]["aggressive_buy_volume"] == 10000
    assert result["primary_source"] == "spot"
    assert result["sources"]["perp"]["quantity_unit"] == "contracts"


def test_volume_profile_real_volume_bins_value_area_and_threshold():
    engine = AdvancedFlow(bucket_dollars=10)
    for i, (price, volume) in enumerate([(100, 1), (110, 6), (120, 3)]):
        trade(engine, i, price, volume)
    profile = summary(engine, 2, threshold=115)["volume_profile"]
    assert profile["point_of_control"] == 115
    assert profile["value_area_low"] == 110
    assert profile["value_area_high"] == 130
    assert profile["value_area_fraction"] == 0.9
    assert profile["total_volume"] == 10
    assert profile["concentration_near_threshold"] == 1
    assert profile["high_volume_nodes"] == [115, 125]  # bounded, documented quantile nodes
    assert profile["low_volume_nodes"] == [105]
    snapshot = engine.summarize(NOW + timedelta(seconds=2), threshold=115)
    assert snapshot["candidate_features"]["advanced_profile_threshold_concentration"] == 1
    assert (
        snapshot["candidate_feature_sources"]["advanced_profile_threshold_concentration"][
            "received_at"
        ]
        == (NOW + timedelta(seconds=2)).isoformat()
    )
    assert (
        engine.summarize(NOW + timedelta(seconds=2))["candidate_features"][
            "advanced_profile_threshold_concentration"
        ]
        is None
    )


def test_multi_level_book_imbalance_microprice_and_depth_weighted_price():
    engine = AdvancedFlow()
    quote(engine, 0, bids=[[99, 3], [98, 2]], asks=[[101, 1], [102, 2]])
    book = summary(engine, 0)["order_book"]
    assert book["depth_levels"] == 2
    assert book["top_of_book_imbalance"] == 0.5
    assert book["multi_level_imbalance"] == 0.25
    assert book["microprice"] == 100.5
    assert book["depth_weighted_midprice"] == pytest.approx((99 * 3 + 98 * 2 + 101 + 102 * 2) / 8)
    assert book["liquidity_concentration"] == 3 / 8


def test_top_only_does_not_pretend_twenty_levels_and_aggregate_is_not_top():
    engine = AdvancedFlow()
    quote(engine, 0, bid_depth=3, ask_depth=1, depth_levels=1)
    book = summary(engine, 0)["order_book"]
    assert book["depth_levels"] == 1
    assert book["microprice"] == 100.5
    quote(engine, 1, bid_depth=30, ask_depth=10, depth_levels=20)
    book = summary(engine, 1)["order_book"]
    assert book["multi_level_imbalance"] == 0.5
    assert book["microprice"] is None
    assert book["top_of_book_imbalance"] is None
    assert book["depth_weighted_midprice"] is None


def test_effective_spread_requires_actual_contemporaneous_pretrade_mid():
    engine = AdvancedFlow()
    quote(engine, 0)
    trade(engine, 0.5, price=101)
    result = summary(engine, 0.5)["microstructure"]
    assert result["effective_spread"] == 2
    assert result["effective_spread_samples"] == 1
    trade(engine, 2, price=102)
    assert summary(engine, 2)["microstructure"]["effective_spread_samples"] == 1
    # A quote received first but exchange-timestamped AFTER the trade is not a
    # pre-trade benchmark; the overall parser rejects exchange time reversal.
    with pytest.raises(ValueError, match="Out-of-order exchange"):
        engine.observe(
            dict(
                source="spot",
                price=102,
                volume=1,
                aggressor="buy",
                kind="trade",
                trade_id="old-event",
                received_at=NOW + timedelta(seconds=3),
                event_at=NOW + timedelta(seconds=1),
            )
        )


def test_cvd_actual_slope_momentum_acceleration_and_inferred_label():
    engine = AdvancedFlow()
    for seconds, side in ((0, "sell"), (60, "buy"), (90, "buy"), (120, "buy")):
        trade(
            engine, seconds, price=100 + seconds / 100, volume=2, side=side, direction_inferred=True
        )
    result = summary(engine, 120)
    assert result["cvd"]["window_net_delta"] == 4
    assert result["cvd"]["slope_per_second"] == pytest.approx(4 / 120)
    assert result["cvd"]["acceleration_per_second_squared"] > 0
    assert result["order_flow"]["direction_reliability"] == "inferred; not validated"
    assert result["order_flow"]["large_trade_count"] == 4


def test_cvd_divergence_descriptive_without_probability_claim():
    engine = AdvancedFlow()
    for i, (price, side, volume) in enumerate(
        [
            (100, "buy", 10),
            (101, "sell", 2),
            (100, "sell", 2),
            (102, "sell", 2),
            (103, "sell", 2),
            (102, "sell", 2),
        ]
    ):
        trade(engine, i, price, volume, side)
    assert summary(engine, 5)["cvd"]["divergence"].startswith("BEARISH")
    assert engine.summarize(NOW + timedelta(seconds=5))["probability_adjustment"] == 0


def test_asof_cutoff_rejects_future_receipt_and_keeps_snapshot_proof():
    engine = AdvancedFlow()
    trade(engine, 0, 100)
    trade(engine, 1, 200)
    past = summary(engine, 0)
    assert past["vwap"]["rolling"] == 100
    assert past["vwap"]["session"] == 100
    assert past["received_at"] == NOW.isoformat()
    proof = past["candidate_feature_sources"]["advanced_vwap_deviation_fraction"]
    assert proof["received_at"] == NOW.isoformat()
    assert set(past["candidate_features"]) == set(FEATURE_NAMES)
    assert (
        engine.summarize(NOW - timedelta(seconds=1))["sources"]["spot"]["status"] == "UNAVAILABLE"
    )


def test_closed_thirty_second_bars_only_actual_trades_no_future_or_forming():
    engine = AdvancedFlow()
    trade(engine, 1, 100, 1)
    trade(engine, 20, 110, 2)
    quote(engine, 21, bid=1000, ask=1002)
    trade(engine, 31, 120, 1)
    bars = engine.closed_bars("spot", NOW + timedelta(seconds=31))["candles"]
    assert len(bars) == 1
    assert bars[0]["open"] == 100 and bars[0]["close"] == 110
    assert bars[0]["high"] == 110 and bars[0]["volume"] == 3
    assert bars[0]["first_received_at"] == (NOW + timedelta(seconds=31)).isoformat()
    assert bars[0]["start"] == NOW.timestamp()
    assert engine.closed_bars("spot", NOW + timedelta(seconds=29))["candles"] == []
    assert engine.closed_bars("missing", NOW)["candles"] == []


def test_gap_reset_does_not_join_cvd_or_vwap_across_missing_path():
    engine = AdvancedFlow()
    trade(engine, 1, 100, 10, session_id="a")
    trade(engine, 31, 200, 1, session_id="b")
    result = summary(engine, 31)
    assert result["vwap"]["session"] == 200
    assert result["vwap"]["rolling"] == 200
    assert result["cvd"]["session_net_delta"] == 1
    assert result["reset_count"] == 1
    assert engine.closed_bars("spot", NOW + timedelta(seconds=31))["candles"] == []
    # Exchange replays of a previously observed ID are not recounted.
    assert not engine.observe(
        dict(
            source="spot",
            price=100,
            volume=10,
            aggressor="buy",
            kind="trade",
            trade_id="1",
            event_at=NOW + timedelta(seconds=32),
            received_at=NOW + timedelta(seconds=32),
            session_id="b",
        )
    )


def test_sequence_gap_requires_snapshot_for_incremental_messages():
    engine = AdvancedFlow()
    quote(engine, 0, sequence=1, snapshot=True)
    assert not quote(engine, 1, sequence=3, incremental=True)
    assert not quote(engine, 2, sequence=4, incremental=True)
    assert quote(engine, 3, sequence=5, snapshot=True)
    assert summary(engine, 3)["reset_count"] == 1


def test_stale_books_are_not_freshened_by_new_trade():
    engine = AdvancedFlow()
    quote(engine, 0, bid_depth=2, ask_depth=1, depth_levels=1)
    trade(engine, 6)
    result = summary(engine, 6)
    assert result["status"] == "READY"
    assert result["order_book"]["status"] == "STALE"
    assert result["order_book"].get("microprice") is None
    assert summary(engine, 12)["vwap"]["status"] == "STALE"


def test_bounded_records_disclose_truncated_rolling_volume_without_corrupting_session():
    engine = AdvancedFlow(max_records=100)
    for i in range(101):
        trade(engine, i / 10)
    result = summary(engine, 10)
    assert result["retained_trade_records"] == 100
    assert result["truncation_count"] == 1
    assert result["vwap"]["rolling"] is None
    assert result["vwap"]["session"] == 100
    assert result["volume_profile"]["status"] == "UNAVAILABLE"
    assert result["cvd"]["session_net_delta"] == 101


@pytest.mark.parametrize(
    "changes",
    [
        dict(volume=-1),
        dict(aggressor="unknown"),
        dict(event_at=NOW + timedelta(seconds=1)),
        dict(received_at=NOW.replace(tzinfo=None)),
    ],
)
def test_invalid_data_rejected(changes):
    engine = AdvancedFlow()
    data = dict(
        source="spot",
        price=100.0,
        kind="trade",
        volume=1.0,
        aggressor="buy",
        trade_id="a",
        event_at=NOW,
        received_at=NOW,
    )
    data.update(changes)
    with pytest.raises((ValueError, TypeError)):
        engine.observe(data)


def test_book_mismatch_and_invalid_source_bounds_rejected():
    engine = AdvancedFlow(max_sources=1)
    with pytest.raises(ValueError, match="disagree"):
        quote(engine, 0, bids=[[98, 1]], asks=[[101, 1]])
    trade(engine, 0)
    with pytest.raises(ValueError, match="source"):
        trade(engine, 1, source="other")


def test_quote_does_not_freshen_stale_trade_indicators_or_receipt_proof():
    engine = AdvancedFlow()
    trade(engine, 0)
    quote(engine, 10, bid=109, ask=111)
    result = summary(engine, 10)
    assert result["status"] == "READY"
    assert result["vwap"]["status"] == "STALE"
    assert result["vwap"]["age_seconds"] == 10
    assert result["candidate_features"]["advanced_vwap_deviation_fraction"] is None
    assert result["order_book"]["age_seconds"] == 0
    fresh = AdvancedFlow()
    trade(fresh, 0)
    quote(fresh, 1, bid=109, ask=111)
    proof = summary(fresh, 1)["candidate_feature_sources"]["advanced_vwap_deviation_fraction"]
    assert proof["received_at"] == (NOW + timedelta(seconds=1)).isoformat()
    assert proof["source"] == "spot"


def test_gap_with_no_new_snapshot_withholds_old_fresh_book():
    engine = AdvancedFlow()
    quote(engine, 0, sequence=1, snapshot=True)
    assert not quote(engine, 1, sequence=3, incremental=True)
    assert summary(engine, 1)["status"] == "WARMING UP"
    # Historical cutoff still sees the original valid snapshot.
    assert summary(engine, 0)["status"] == "READY"


def test_bar_close_needs_actual_postclose_message_not_wall_clock():
    engine = AdvancedFlow()
    trade(engine, 1)
    # The clock alone cannot prove no trades were missed before the close.
    assert engine.closed_bars("spot", NOW + timedelta(seconds=60))["candles"] == []
    quote(engine, 31)
    assert engine.closed_bars("spot", NOW + timedelta(seconds=31))["candles"] == []
    trade(engine, 32)
    result = engine.closed_bars("spot", NOW + timedelta(seconds=32))
    assert result["received_at"] == (NOW + timedelta(seconds=32)).isoformat()
    assert result["candles"][0]["first_received_at"] == result["received_at"]
    assert engine.closed_bars("spot", NOW + timedelta(seconds=30))["candles"] == []


def test_delayed_trade_on_independent_channel_updates_cvd_without_rewinding_quote():
    engine = AdvancedFlow()
    trade(engine, 1, price=100, volume=1)
    quote(engine, 10, bid=119, ask=121)
    engine.observe(
        dict(
            source="spot",
            kind="trade",
            price=110,
            volume=2,
            aggressor="buy",
            trade_id="delayed",
            event_at=NOW + timedelta(seconds=9.9),
            received_at=NOW + timedelta(seconds=10.1),
        )
    )
    result = summary(engine, 10.1)
    assert result["cvd"]["session_net_delta"] == 3
    assert result["vwap"]["rolling"] == pytest.approx(320 / 3)
    assert result["vwap"]["deviation"] == pytest.approx(120 - 320 / 3)
    assert result["latest_price"] == 120
    assert result["price_event_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert result["price_received_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert result["received_at"] == (NOW + timedelta(seconds=10.1)).isoformat()
    proof = result["candidate_feature_sources"]["advanced_vwap_deviation_fraction"]
    assert proof["received_at"] == (NOW + timedelta(seconds=10.1)).isoformat()
    assert result["order_book"]["as_of"] == (NOW + timedelta(seconds=10)).isoformat()
    assert result["order_book"]["age_seconds"] == pytest.approx(0.1)
    # Same-channel retrograde events are rejected explicitly, not silently lost.
    with pytest.raises(ValueError, match="within observation kind"):
        engine.observe(
            dict(
                source="spot",
                kind="trade",
                price=109,
                volume=2,
                aggressor="sell",
                trade_id="retrograde",
                event_at=NOW + timedelta(seconds=9.8),
                received_at=NOW + timedelta(seconds=10.2),
            )
        )


def test_quote_crossing_bar_close_does_not_publish_bar_before_delayed_trade_arrives():
    engine = AdvancedFlow()
    trade(engine, 1, price=100, volume=1)
    quote(engine, 30, bid=119, ask=121)
    assert engine.closed_bars("spot", NOW + timedelta(seconds=30))["candles"] == []
    engine.observe(
        dict(
            source="spot",
            kind="trade",
            price=110,
            volume=2,
            aggressor="buy",
            trade_id="last-before-close",
            event_at=NOW + timedelta(seconds=29.9),
            received_at=NOW + timedelta(seconds=30.1),
        )
    )
    assert engine.closed_bars("spot", NOW + timedelta(seconds=30.1))["candles"] == []
    trade(engine, 31, price=120)
    completed = engine.closed_bars("spot", NOW + timedelta(seconds=31))
    assert len(completed["candles"]) == 1
    assert completed["candles"][0]["close"] == 110
    assert completed["candles"][0]["volume"] == 3
    assert completed["candles"][0]["first_received_at"] == (NOW + timedelta(seconds=31)).isoformat()
    quote(engine, 32, bid=129, ask=131)
    assert engine.closed_bars("spot", NOW + timedelta(seconds=32)) == completed


def test_trade_and_quote_sequence_numbers_are_independent():
    engine = AdvancedFlow()
    quote(engine, 0, sequence=100, snapshot=True)
    trade(engine, 1, sequence=1)
    quote(engine, 2, sequence=101)
    trade(engine, 3, sequence=2)
    result = summary(engine, 3)
    assert result["reset_count"] == 0
    assert result["cvd"]["session_net_delta"] == 2
