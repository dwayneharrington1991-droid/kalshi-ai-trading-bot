"""Offline Wilder/seeded-indicator math, aggregation, availability and as-of safety."""
from datetime import datetime, timedelta, timezone
import math
import time

import pytest

from src.signals_display.advanced_indicators import (
    FEATURE_NAMES,
    _divergence,
    bollinger,
    macd,
    seeded_ema,
    technical_snapshot,
    wilder_rsi,
)

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def candles(count=1440, seconds=60, end=NOW, prices=None):
    prices = prices or [100000 + i * 0.5 + math.sin(i / 7) for i in range(count)]
    return {
        "source": "Mock BTC/USD",
        "received_at": end.isoformat(),
        "candles": [
            {
                "start": int((end - timedelta(seconds=(count - i) * seconds)).timestamp()),
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1,
                "first_received_at": (
                    end - timedelta(seconds=(count - i - 1) * seconds)
                ).isoformat(),
            }
            for i, price in enumerate(prices)
        ],
    }


def test_wilder_rsi_matches_published_seed_and_recursive_values():
    # Wilder example, rounded input prices; recursive RSI is not rolling SMA RSI.
    values = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
        46.41,
        46.22,
        45.64,
        46.21,
    ]
    result = wilder_rsi(values)
    assert all(x is None for x in result[:14])
    assert result[14] == pytest.approx(70.464135, abs=1e-5)
    assert result[15] == pytest.approx(66.249619, abs=1e-5)
    assert result[20] == pytest.approx(62.880718, abs=1e-5)
    assert wilder_rsi([10] * 15)[-1] == 50
    assert wilder_rsi(list(range(1, 16)))[-1] == 100
    assert wilder_rsi(list(range(15, 0, -1)))[-1] == 0


def test_ema_uses_sma_seed_and_correct_recursion():
    result = seeded_ema([1, 2, 3, 4, 5], 3)
    assert result == [None, None, 2, 3, 4]
    assert seeded_ema([3, 6], 3) == [None, None]
    with pytest.raises(ValueError):
        seeded_ema([1, float("nan")], 3)


def test_macd_seeds_independent_signal_after_slow_ema():
    result = macd(list(range(1, 101)))
    assert result["line"][24] is None
    assert result["line"][25] == pytest.approx(7)
    assert result["signal"][32] is None
    assert result["signal"][33] == pytest.approx(7)
    assert result["histogram"][-1] == pytest.approx(0)
    fast = macd(list(range(1, 101)), 6, 13, 5)
    assert fast["line"][12] == pytest.approx(3.5)
    with pytest.raises(ValueError):
        macd([1, 2], 26, 12)


def test_bollinger_math_width_percent_b_and_deviation_configuration():
    values = list(range(1, 21))
    result = bollinger(values)
    assert result["middle"] == 10.5
    assert result["standard_deviation"] == pytest.approx(math.sqrt(33.25))
    assert result["upper"] == pytest.approx(10.5 + 2 * math.sqrt(33.25))
    assert result["bandwidth"] == pytest.approx(4 * math.sqrt(33.25) / 10.5)
    assert result["percent_b"] == pytest.approx(
        (20 - result["lower"]) / (result["upper"] - result["lower"])
    )
    assert bollinger([10] * 20)["percent_b"] is None
    assert bollinger(values, deviations=1)["bandwidth"] == pytest.approx(result["bandwidth"] / 2)
    with pytest.raises(ValueError):
        bollinger([], deviations=0)


def test_multitimeframe_availability_and_no_invented_30_second_bars():
    result = technical_snapshot(candles(), NOW)
    assert result["status"] == "READY"
    assert result["timeframes"]["30s"]["closed_bars"] == 0
    assert result["timeframes"]["1m"]["closed_bars"] == 1440
    assert result["timeframes"]["5m"]["closed_bars"] == 288
    assert result["timeframes"]["15m"]["closed_bars"] == 96
    assert result["timeframes"]["1h"]["closed_bars"] == 24
    assert result["timeframes"]["1m"]["ema"]["periods"]["200"]["status"] == "READY"
    assert result["timeframes"]["5m"]["ema"]["periods"]["200"]["status"] == "UNAVAILABLE"
    assert result["timeframes"]["1h"]["rsi"]["status"] == "READY"
    assert result["timeframes"]["1h"]["macd"]["standard"]["status"] == "UNAVAILABLE"
    assert result["timeframes"]["1h"]["ema"]["all_periods_ready"] is False
    assert set(result["candidate_features"]) == set(FEATURE_NAMES)
    assert result["probability_adjustment"] == 0
    assert all(not family["contributing"] for family in result["families"].values())


def test_genuine_30_second_bars_are_accepted_and_warmed_independently():
    result = technical_snapshot(candles(), NOW, short_bars=candles(500, 30))
    assert result["timeframes"]["30s"]["rsi"]["status"] == "READY"
    assert result["timeframes"]["30s"]["ema"]["periods"]["200"]["status"] == "READY"
    assert result["candidate_features"]["advanced_30s_wilder_rsi14"] is not None


def test_partial_aggregate_is_not_a_closed_timeframe_bar():
    data = candles(60, end=NOW + timedelta(minutes=2))
    result = technical_snapshot(data, NOW + timedelta(minutes=2))
    assert result["timeframes"]["5m"]["last_closed_at"] == NOW.isoformat()
    assert result["timeframes"]["15m"]["last_closed_at"] == NOW.isoformat()
    assert result["timeframes"]["1h"]["closed_bars"] == 0


def test_future_or_forming_bars_do_not_change_asof_snapshot():
    data = candles(100)
    before = technical_snapshot(data, NOW)
    data["candles"].append(
        {
            "start": int(NOW.timestamp()),
            "open": 1e6,
            "high": 1e6,
            "low": 1e6,
            "close": 1e6,
            "volume": 999999,
            "first_received_at": (NOW + timedelta(minutes=1)).isoformat(),
        }
    )
    assert technical_snapshot(data, NOW) == before
    data["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    after = technical_snapshot(data, NOW)
    assert after["status"] == "WARMING_UP"
    assert "after analysis cutoff" in after["timeframes"]["1m"]["continuity"]


def test_late_received_closed_bar_is_excluded_and_recursion_resets_after_gap():
    data = candles(100)
    data["candles"][50]["first_received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    result = technical_snapshot(data, NOW)
    assert result["timeframes"]["1m"]["closed_bars"] == 49
    assert "restarted" in result["timeframes"]["1m"]["continuity"]
    assert result["timeframes"]["1m"]["macd"]["standard"]["status"] == "UNAVAILABLE"


def test_duplicate_invalid_unaligned_and_stale_bars_fail_closed():
    data = candles(100)
    data["candles"].append(data["candles"][-1].copy())
    assert technical_snapshot(data, NOW)["status"] == "WARMING_UP"
    data = candles(100)
    data["candles"][0]["high"] = 1
    assert technical_snapshot(data, NOW)["status"] == "WARMING_UP"
    data = candles(100)
    data["candles"][0]["start"] += 1
    assert technical_snapshot(data, NOW)["status"] == "WARMING_UP"
    data = candles(100, end=NOW - timedelta(minutes=10))
    assert technical_snapshot(data, NOW)["status"] == "WARMING_UP"


def test_flat_series_reports_neutral_without_fake_percent_b_or_reversal():
    result = technical_snapshot(candles(500, prices=[100000] * 500), NOW)
    minute = result["timeframes"]["1m"]
    assert minute["rsi"]["interpretation"] == "NEUTRAL"
    assert minute["rsi"]["potential_reversal"] == "NONE"
    assert minute["rsi"]["divergence"] == "NONE_CONFIRMED"
    assert minute["bollinger"]["percent_b"] is None
    assert minute["ema"]["alignment"] == "NEUTRAL"
    assert minute["macd"]["standard"]["histogram"] == pytest.approx(0)


def test_macd_and_rsi_do_not_use_unconfirmed_endpoint_for_divergence():
    # Last two prices cannot be confirmed pivots because their right side is unknown.
    data = candles(100, prices=[100 + i * 0.01 for i in range(98)] + [150, 200])
    result = technical_snapshot(data, NOW)
    assert result["timeframes"]["1m"]["rsi"]["divergence"] == "NONE_CONFIRMED"
    assert result["timeframes"]["1m"]["macd"]["standard"]["divergence"] == "NONE_CONFIRMED"


def test_confirmed_pivot_divergences_compare_price_and_oscillator_independently():
    prices = [1, 2, 3, 2, 1, 2, 4, 2, 1, 2]
    oscillator = [10, 20, 80, 30, 20, 40, 60, 30, 20, 10]
    assert _divergence(prices, oscillator) == "BEARISH_DIVERGENCE"
    assert (
        _divergence([5 - x for x in prices], [100 - x for x in oscillator]) == "BULLISH_DIVERGENCE"
    )
    assert _divergence(prices, [None] * len(prices)) == "NONE_CONFIRMED"


def test_feature_receipt_proof_is_actual_source_receipt_not_analysis_clock():
    end = NOW - timedelta(seconds=30)
    result = technical_snapshot(candles(100, end=NOW), NOW + timedelta(seconds=30))
    assert result["as_of"] == (NOW + timedelta(seconds=30)).isoformat()
    assert result["max_input_received_at"] == NOW.isoformat()
    proof = result["candidate_feature_sources"]["advanced_1m_wilder_rsi14"]
    assert proof["received_at"] == NOW.isoformat()
    assert proof["event_at"] == NOW.isoformat()


def test_unavailable_or_malformed_history_does_not_fabricate_features():
    for data in (
        None,
        [],
        {},
        {"received_at": None, "candles": []},
        {"received_at": NOW.isoformat(), "candles": []},
    ):
        result = technical_snapshot(data, NOW)
        assert result["status"] == "WARMING_UP"
        assert all(value is None for value in result["candidate_features"].values())
        assert result["max_input_received_at"] is None
    data = candles(100)
    data["candles"][-1]["volume"] = True
    assert technical_snapshot(data, NOW)["status"] == "WARMING_UP"
