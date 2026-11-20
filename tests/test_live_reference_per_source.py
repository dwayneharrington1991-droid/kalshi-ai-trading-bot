"""The live final-minute estimate keeps one continuous series per exchange.

Found 2026-11-20 (production 3ce9071): in every final minute the live settlement
estimate failed with "MISSING_AVERAGING_HISTORY: final averaging window not covered"
(110 of 375 contracts had no final-minute live prediction; median 1 versus 21 in a
mid-contract minute). Coinbase and Kraken stream ticks arrive interleaved, and the
single live buffer was cleared every time the source changed, so it never reached
back to the start of the 60 s averaging window. Each exchange now keeps its own
series; the estimate uses only the current reference source's series, so exchanges
are still never mixed.
"""
from datetime import timedelta

from src.signals_display.live_model import LiveEstimator
from src.signals_display.model import timestamp
from tests.test_signals_display import NOW

CB, KR = "Coinbase BTC/USD stream", "Kraken BTC/USD stream"


def _tick(model, source, at, price):
    return model.observe_reference(
        {"source": source, "kind": "quote", "price": price, "received_at": at.isoformat()}, at)


def _interleaved(model, start, seconds):
    for s in range(seconds):
        at = start + timedelta(seconds=s)
        _tick(model, CB, at, 100000 + s)
        _tick(model, KR, at + timedelta(milliseconds=300), 99995 + s)


def test_interleaved_exchanges_keep_continuous_single_source_history():
    model = LiveEstimator()
    _interleaved(model, NOW, 120)
    model.observe({"reference": {"source": CB, "observed_at": (NOW + timedelta(seconds=119)).isoformat(),
                                 "price": 100119}}, NOW + timedelta(seconds=119.5))
    series = list(model.reference)
    assert model.reference_source == "Coinbase BTC/USD proxy"
    assert (series[-1][0] - series[0][0]).total_seconds() >= 110      # was 0: cleared on every switch
    assert all(p >= 100000 for _, p in series)                         # Kraken prices never mixed in


def test_final_minute_settlement_estimate_has_averaging_history():
    model = LiveEstimator()
    close = NOW + timedelta(seconds=160)
    _interleaved(model, NOW, 130)
    now = NOW + timedelta(seconds=130)
    payload = {"reference": {"source": CB, "observed_at": (now - timedelta(seconds=0.5)).isoformat(),
                             "price": 100130}, "btc_candles": {}, "markets": []}
    model.observe(payload, now)
    market = {"averaging_rule_verified": True, "strike": 100050, "closes_at": close.isoformat(),
              "opens_at": (close - timedelta(minutes=15)).isoformat(), "ticker": "KXBTC15M-T"}
    info = {"candidate_features": {"values": {}}, "version": "t", "settlement_assumptions": ""}
    reason, _ = model._settlement(payload, market, now, (close - now).total_seconds(), info)
    # Before the fix: "INSUFFICIENT_HISTORY: reference 1/61 samples" here, and in production (where candle
    # volatility is the fallback) "MISSING_AVERAGING_HISTORY: final averaging window not covered".
    assert "INSUFFICIENT_HISTORY" not in reason and "MISSING_AVERAGING_HISTORY" not in reason, reason


def test_a_real_gap_still_resets_that_source_only():
    model = LiveEstimator()
    _interleaved(model, NOW, 30)
    later = NOW + timedelta(seconds=60)                                  # 30 s Coinbase silence
    _tick(model, CB, later, 100500)
    model.observe({"reference": {"source": CB, "observed_at": later.isoformat(), "price": 100500}},
                  later + timedelta(seconds=0.2))
    assert [p for _, p in model.reference] == [100500]
