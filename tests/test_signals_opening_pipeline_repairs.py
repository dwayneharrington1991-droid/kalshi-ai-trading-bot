"""Opening data-pipeline repairs: per-feature exclusion, candle features, as-of
order flow, executable opening quotes, fee schedule retention. No look-ahead."""
from datetime import timedelta
import json
import math
import sqlite3

import pytest

from src.signals_display import features as live_features
from src.signals_display import learning
from src.signals_display import prediction_engine as engine
from src.signals_display.monitor import Journal
from tests.test_signals_display import NOW
from tests.test_signals_opening_models import opening


def _context(**extra):
    base = {
        "cutoff": NOW.isoformat(),
        "primary_source": "Coinbase BTC/USD stream",
        "candidate_features": {},
        "sources": {
            "Kraken BTC/USD stream": {
                "status": "LIVE",
                "windows": {"5s": {"status": "READY", "features": {"depth_imbalance": 0.4},
                                   "last_received_at": (NOW - timedelta(seconds=1)).isoformat()}},
            },
        },
    }
    base.update(extra)
    return base


def test_one_late_optional_feature_no_longer_discards_the_others():
    late_advanced = {
        "cutoff": NOW.isoformat(),
        "candidate_features": {"advanced_cvd_slope": 0.3, "advanced_effective_spread": 0.0002},
        "candidate_feature_sources": {
            "advanced_cvd_slope": {"received_at": (NOW + timedelta(seconds=3)).isoformat()},  # after open
            "advanced_effective_spread": {"received_at": (NOW - timedelta(seconds=2)).isoformat()},
        },
    }
    result = opening(frozen_context=_context(advanced_features=late_advanced))
    frozen = result["frozen_features"]
    values = frozen["values"]
    assert values["advanced_cvd_slope"] is None  # excluded alone
    assert values["advanced_effective_spread"] == pytest.approx(0.0002)  # kept
    assert values["spot_depth_imbalance"] == pytest.approx(0.4)  # kept
    assert "POST_OPENING_SOURCE_RECEIPT_EXCLUDED=1" in frozen["optional_context_status"]
    assert frozen["max_input_received_at"] <= NOW.isoformat()


def test_global_post_opening_context_is_still_excluded_entirely():
    result = opening(frozen_context=_context(cutoff=(NOW + timedelta(seconds=1)).isoformat()))
    frozen = result["frozen_features"]
    assert frozen["values"]["spot_depth_imbalance"] is None
    assert frozen["optional_context_status"] == "POST_OPENING_OR_UNVERIFIABLE_CONTEXT_EXCLUDED"


def test_candle_features_at_opening_match_live_definitions_on_pre_open_bars():
    result = opening()
    values = result["frozen_features"]["values"]
    for name in ("candle_body_range_ratio", "candle_close_range_position", "candle_volume_weighted_move"):
        assert values[name] is not None, name
    # Recompute with the live extractor's formulas from the same closed pre-open bars.
    from tests.test_signals_prediction_engine import history
    data = history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1))
    bars = sorted((b for b in data["candles"] if int(b["start"]) + 60 <= NOW.timestamp()),
                  key=lambda b: int(b["start"]))[-25:]
    o, h, l, c, v = (float(bars[-1][k]) for k in ("open", "high", "low", "close", "volume"))
    scale = max(h - l, 1e-9)
    assert values["candle_body_range_ratio"] == pytest.approx(abs(c - o) / scale)
    assert values["candle_close_range_position"] == pytest.approx((c - l) / scale)
    prior = [float(b["volume"]) for b in bars[-21:-1]]
    assert values["candle_volume_weighted_move"] == pytest.approx((c / o - 1) * (v / (sum(prior) / 20)))
    ranges = [max(float(b["high"]) - float(b["low"]), abs(float(b["high"]) - float(a["close"])),
                  abs(float(b["low"]) - float(a["close"]))) for a, b in zip(bars, bars[1:])]
    window = ranges[-21:-1]
    mean_range = sum(window) / 20
    std = math.sqrt(sum((r - mean_range) ** 2 for r in window) / 20)
    expected = (ranges[-1] - mean_range) / std if std > 0 else None
    if expected is None:  # constant ranges: undefined, exactly like the live extractor
        assert values["candle_atr_zscore_20"] is None and values["candle_unusual_expansion"] is None
    else:
        assert values["candle_atr_zscore_20"] == pytest.approx(expected)


def _flow(observed):
    return {
        "spot": {"status": "LIVE", "observed_at": observed.isoformat(), "delta_fraction_60s": 0.25,
                 "passive_sell_absorption_candidate": True},
        "perpetual": {"status": "LIVE", "observed_at": observed.isoformat(), "delta_fraction_60s": -0.1},
        "spot_book": {"status": "LIVE", "observed_at": observed.isoformat(), "bid_ask_ratio": 3.0,
                      "order_book_imbalance_top3": 0.2, "micro_price_deviation": 0.001},
    }


def test_pre_open_order_flow_is_used_with_receipts_and_post_open_flow_is_not():
    good = opening(frozen_context=_context(order_flow=_flow(NOW - timedelta(seconds=2))))
    values = good["frozen_features"]["values"]
    assert values["spot_cvd_delta_fraction"] == pytest.approx(0.25)
    assert values["perpetual_cvd_delta_fraction"] == pytest.approx(-0.1)
    assert values["spot_book_dominance"] == pytest.approx(0.5)
    assert values["spot_book_imbalance_top3"] == pytest.approx(0.2)
    assert values["spot_absorption_candidate"] == 1
    receipts = good["frozen_features"]["sources"]["microstructure"]["features"]
    assert receipts["spot_cvd_delta_fraction"]["received_at"] <= NOW.isoformat()
    for observed, reason in ((NOW + timedelta(seconds=1), "POST_OPENING_ORDER_FLOW_EXCLUDED"),
                             (NOW - timedelta(seconds=9), "STALE_PRE_OPENING_ORDER_FLOW_EXCLUDED")):
        bad = opening(frozen_context=_context(order_flow=_flow(observed)))
        assert bad["frozen_features"]["values"]["spot_cvd_delta_fraction"] is None
        assert bad["frozen_features"]["values"]["spot_book_dominance"] is None
        assert reason in bad["frozen_features"]["optional_context_status"]
        assert bad["frozen_features"]["values"]["spot_depth_imbalance"] == pytest.approx(0.4)


def test_repaired_inputs_never_change_the_opening_probability_or_baseline():
    plain = opening()
    enriched = opening(frozen_context=_context(order_flow=_flow(NOW - timedelta(seconds=2))))
    assert enriched["up"] == plain["up"] and enriched["baseline_probability_up"] == plain["baseline_probability_up"]


def test_loaded_model_ignores_features_outside_its_trained_preprocessor():
    model = {"kind": "logistic", "weights": [0.5, 0.0, 0.1], "l2": 1.0, "calibration": [1.0, 0.0],
             "preprocessor": {"names": ["return_5m"], "means": [0.0], "scales": [0.001]}}
    before = learning.predict(model, {"return_5m": 0.0005, "candle_body_range_ratio": None})
    after = learning.predict(model, {"return_5m": 0.0005, "candle_body_range_ratio": 0.9,
                                     "spot_cvd_delta_fraction": 0.3})
    assert before == after


def _market(observed, opens=NOW, up_ask=0.52, down_ask=0.50):
    return {"ticker": "KXBTC15M-Q", "opens_at": opens.isoformat(), "observed_at": observed.isoformat(),
            "quote_event_time": observed.isoformat(), "fee_multiplier": 1.0, "fee_type": "quadratic",
            "quotes": {"up": {"bid": up_ask - 0.02, "ask": up_ask, "ask_size": 40},
                       "down": {"bid": down_ask - 0.02, "ask": down_ask, "ask_size": 30}}}


def test_opening_quotes_are_recorded_append_only_inside_the_window(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    mode = "PUBLIC MARKET DATA"
    assert journal.record_opening_quote(_market(NOW + timedelta(seconds=3)), mode, NOW + timedelta(seconds=4))
    assert not journal.record_opening_quote(_market(NOW + timedelta(seconds=3)), mode, NOW + timedelta(seconds=4))  # duplicate
    assert not journal.record_opening_quote(_market(NOW + timedelta(seconds=200)), mode, NOW + timedelta(seconds=201))  # late
    assert not journal.record_opening_quote(_market(NOW + timedelta(seconds=5)), mode, NOW + timedelta(seconds=30))  # stale
    assert not journal.record_opening_quote(_market(NOW - timedelta(seconds=1)), mode, NOW)  # before open
    assert not journal.record_opening_quote(_market(NOW + timedelta(seconds=6), up_ask=0), mode, NOW + timedelta(seconds=6))
    with sqlite3.connect(tmp_path / "journal.db") as db:
        rows = db.execute("SELECT seconds_from_open,up_ask,down_ask,fee_multiplier,fee_type FROM opening_quote_snapshots").fetchall()
        assert rows == [(3.0, 0.52, 0.5, 1.0, "quadratic")]
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE opening_quote_snapshots SET up_ask=0.1")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM opening_quote_snapshots")


def test_compact_prediction_evidence_keeps_the_fee_schedule():
    state = {"mode": "PUBLIC MARKET DATA", "now": NOW.isoformat(), "stale": False,
             "market": {"ticker": "KXBTC15M-Q", "fee_multiplier": 1.0, "fee_type": "quadratic",
                        "quotes": {"up": {"ask": 0.5}}}, "model": {}}
    compact = Journal._compact_prediction(state, state)
    assert compact["market"]["fee_multiplier"] == 1.0 and compact["market"]["fee_type"] == "quadratic"


def test_preopening_flow_cache_keeps_only_flow_received_by_the_opening():
    from src.signals_display.server import Monitor

    class Holder:
        preopening_flow = {}
    holder = Holder()
    market = {"ticker": "KXBTC15M-Q", "opens_at": NOW.isoformat(),
              "closes_at": (NOW + timedelta(minutes=15)).isoformat()}
    def payload(observed):
        return {"markets": [market], "coverage": {"Kraken public order flow": {"data": _flow(observed)}}}
    Monitor.remember_preopening_flow(holder, payload(NOW - timedelta(seconds=2)), NOW - timedelta(seconds=1))
    Monitor.remember_preopening_flow(holder, payload(NOW + timedelta(seconds=1)), NOW + timedelta(seconds=2))
    kept = holder.preopening_flow["KXBTC15M-Q"]["spot"]["observed_at"]
    assert kept == (NOW - timedelta(seconds=2)).isoformat()
