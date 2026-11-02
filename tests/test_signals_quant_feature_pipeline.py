"""Timestamp-safe quantitative features use synthetic data only."""
import math

import pandas as pd
import pytest

from src.signals_display.quant_feature_pipeline import (
    aggregate_ohlcv,
    build_feature_frame,
    candle_features,
    contract_features,
    order_book_features,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")


def minute_candles(count=330):
    rows = []
    for i in range(count):
        price = 100 + i * 0.1
        rows.append({
            "timestamp": START + pd.Timedelta(minutes=i),
            "open": price, "high": price + 1, "low": price - 1,
            "close": price + 0.5, "volume": 10 + i % 5,
        })
    return pd.DataFrame(rows)


def test_closed_15m_aggregation_rejects_forming_future_block():
    raw = minute_candles(31)
    result = aggregate_ohlcv(raw, as_of=START + pd.Timedelta(minutes=30))
    assert len(result) == 2
    assert list(result["source_rows"]) == [15, 15]
    assert result.iloc[-1]["closed_at"] == START + pd.Timedelta(minutes=30)


def test_candle_microstructure_volume_and_dynamic_atr_features():
    candles = aggregate_ohlcv(
        minute_candles(), as_of=START + pd.Timedelta(minutes=330)
    )
    result = candle_features(candles, rolling_periods=5)
    row = result.iloc[-1]
    assert row["body_to_wick_ratio"] == pytest.approx(
        abs(row["close"] - row["open"]) / (row["high"] - row["low"])
    )
    assert row["position_inside_range"] == pytest.approx(
        (row["close"] - row["low"]) / (row["high"] - row["low"])
    )
    expected_volume = row["volume"] / result["volume"].iloc[-6:-1].mean()
    assert row["volume_weighted_move"] == pytest.approx(row["candle_return"] * expected_volume)
    assert not pd.isna(row["true_range"])


def test_unusual_expansion_requires_dynamic_prior_range_zscore():
    rows = []
    for i in range(22):
        half_range = 10 if i == 21 else 1 + (i % 5) / 10
        rows.append({
            "timestamp": START + pd.Timedelta(minutes=15 * i),
            "open": 100, "high": 100 + half_range, "low": 100 - half_range,
            "close": 100, "volume": 10,
        })
    row = candle_features(pd.DataFrame(rows)).iloc[-1]
    assert row["atr_zscore_20"] > 1.5
    assert bool(row["unusual_expansion_bar"])


def books():
    rows = []
    for j in range(2):
        row = {"timestamp": START + pd.Timedelta(minutes=15 * (j + 1))}
        for i in range(1, 4):
            row.update({
                f"bid_price_{i}": 99 - 0.1 * (i - 1),
                f"ask_price_{i}": 101 + 0.1 * (i - 1),
                f"bid_size_{i}": 2 * i,
                f"ask_size_{i}": i,
            })
        rows.append(row)
    return pd.DataFrame(rows)


def test_top_three_obi_and_microprice_match_documented_math():
    result = order_book_features(books())
    row = result.iloc[0]
    bid_size, ask_size = 12, 6
    assert row["obi_top_3"] == pytest.approx((bid_size - ask_size) / (bid_size + ask_size))
    expected = (99 * ask_size + 101 * bid_size) / (bid_size + ask_size)
    assert row["micro_price"] == pytest.approx(expected)
    assert row["micro_price_deviation"] == pytest.approx((expected - 100) / 100)


def contracts():
    return pd.DataFrame([{
        "timestamp": START + pd.Timedelta(minutes=15),
        "yes_bid": 0.56, "yes_ask": 0.60, "no_bid": 0.39, "no_ask": 0.43,
        "minutes_remaining": 7.5, "fee_multiplier": 1, "slippage": 0.01,
    }])


def test_binary_contract_vig_cost_theta_and_logit_are_distinct():
    row = contract_features(contracts()).iloc[0]
    assert row["vig_stripped_yes_probability"] == pytest.approx(0.58 / (0.58 + 0.41))
    assert row["yes_entry_fee"] == 0.02
    assert row["yes_break_even_probability"] == pytest.approx(0.63)
    assert row["time_remaining_fraction"] == 0.5
    assert row["theta_decay_linear"] == 0.5
    assert row["theta_decay_exponential"] == pytest.approx(math.exp(-1.5))
    assert row["yes_logit"] == pytest.approx(math.log(0.58 / 0.42))


def test_combined_pipeline_uses_backward_only_timestamp_joins():
    result = build_feature_frame(
        minute_candles(31), order_books=books(), contracts=contracts(),
        as_of=START + pd.Timedelta(minutes=30),
    )
    assert len(result) == 2
    assert result.iloc[0]["obi_top_3"] == pytest.approx(1 / 3)
    assert result.iloc[0]["yes_break_even_probability"] == pytest.approx(0.63)
