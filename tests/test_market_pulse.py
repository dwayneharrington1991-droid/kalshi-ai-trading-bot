"""Market pulse research indicators: honest labels, stale data never used, edge never claimed unvalidated."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.signals_display import market_pulse as mp

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 22, 5, 0, tzinfo=timezone.utc)


def _src(book=0.3, flow=0.25, age=0.5, status="READY"):
    return {"order_book": {"status": status, "age_seconds": age, "multi_level_imbalance": book, "bid_depth": 3,
                           "ask_depth": 2, "spread": 0.1, "top_of_book_imbalance": 0.1, "depth_levels": 20},
            "microstructure": {"bid_ask_depth_change": -1, "liquidity_deterioration_fraction": 0.2},
            "order_flow": {"status": status, "age_seconds": age, "imbalance": flow, "aggressive_buy_volume": 5,
                           "aggressive_sell_volume": 3, "large_trade_count": 1, "trades_per_second": 1.5,
                           "short_term_momentum": 0.4},
            "cvd": {"window_net_delta": 2, "slope_per_second": 0.01, "acceleration_per_second_squared": 0.0},
            "latest_price": 60000.0, "rejected": 10}


def _snap(**k):
    return {"advanced_intelligence": {"primary_source": "Coinbase BTC/USD stream",
                                      "sources": {"Coinbase BTC/USD stream": _src(**k), "Kraken BTC/USD stream": _src(**k)},
                                      "candidate_features": {"advanced_30s_macd_standard_histogram": 0.5,
                                                             "advanced_1m_macd_standard_histogram": 0.2,
                                                             "advanced_1m_ema9_distance": 0.001, "advanced_cvd_slope": 0.01}},
            "scalping_monitor": {"ticker": "KX", "remaining_seconds": 600, "data_ages": {"quote_seconds": 1.0},
                                 "sides": {"up": {"bid": 0.50, "ask": 0.52, "spread": 0.02, "ask_size": 100, "bid_size": 80,
                                                  "entry_fee": 0.02, "slippage": 0.01},
                                           "down": {"bid": 0.47, "ask": 0.49, "spread": 0.02, "ask_size": 90, "bid_size": 70,
                                                    "entry_fee": 0.02, "slippage": 0.01}}},
            "prediction_engine": {"initial": {"up": 0.70}}}


def test_live_measured_indicators_and_cross_exchange_confirmation():
    r = mp.build(_snap())
    assert r["order_book"]["indicator"] == "BUYING PRESSURE" and r["order_book"]["cross_exchange"] == "CONFIRMED"
    assert r["trade_flow"]["indicator"] == "AGGRESSIVE BUYING" and r["momentum"]["indicator"] == "BULLISH"
    assert r["order_book"]["label"].startswith("LIVE MEASURED") and "not a guaranteed" in r["momentum"]["note"]


def test_stale_sources_are_reported_not_used():
    r = mp.build(_snap(age=9.0))
    assert r["order_book"]["indicator"] == "UNAVAILABLE" and r["trade_flow"]["indicator"] == "UNAVAILABLE"
    assert all(v["status"] == "STALE OR UNAVAILABLE" for v in r["order_book"]["venues"].values())


def test_edge_is_never_positive_without_a_validated_probability():
    r = mp.build(_snap())                               # model says 70% UP vs 55c all-in cost: arithmetic EV > 0
    up = r["contract"]["sides"]["up"]
    assert abs(up["break_even_probability"] - 0.55) < 1e-9 and up["expected_value_per_contract"] > 0
    assert r["contract"]["estimated_edge"] == "UNVERIFIED"
    assert mp.build(_snap(), validated_probability=True)["contract"]["estimated_edge"] == "POSITIVE"


def test_stale_kalshi_quotes_keep_edge_unverified():
    s = _snap()
    s["scalping_monitor"]["data_ages"]["quote_seconds"] = 30
    assert mp.contract(s, validated_probability=True)["estimated_edge"] == "UNVERIFIED"


def test_horizon_forecasts_are_scored_only_by_later_prices_and_stay_unvalidated():
    h = mp.HorizonForecasts(horizons=(5,))
    for i in range(20):                                  # price rises 1/s, pressure always positive
        h.observe(T0 + timedelta(seconds=i), 60000 + i, 0.8)
    rep = h.report()["horizons"]["5s"]
    assert rep["scored"] == 15 and rep["correct"] == 15  # the last 5 are still pending (no future price yet)
    assert rep["status"] == "NOT VALIDATED" and rep["latest_forecast"] is None
    h.observe(T0 + timedelta(seconds=100), None, 0.8)    # missing price never scores anything
    assert h.report()["horizons"]["5s"]["scored"] >= 15


def test_horizon_reaches_supported_only_with_enough_evidence():
    h = mp.HorizonForecasts(horizons=(5,))
    for i in range(mp.VALIDATION_MIN + 50):
        h.observe(T0 + timedelta(seconds=i), 60000 + i, 0.8)
    rep = h.report()["horizons"]["5s"]
    assert rep["status"].startswith("PROSPECTIVELY SUPPORTED") and rep["latest_forecast"]["direction"] == "UP"


def test_real_production_state_shape(tmp_path):
    import json
    sample = ROOT.parent / "research" / "stfull.json"
    if not sample.exists():
        return
    r = mp.build(json.loads(sample.read_text()))
    assert r["order_book"]["indicator"] in {"BUYING PRESSURE", "SELLING PRESSURE", "NEUTRAL", "UNAVAILABLE"}
    assert r["contract"]["estimated_edge"] == "UNVERIFIED"


def test_dashboard_panel_is_visible_text_only_and_labelled():
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    assert "main>#market-pulse-card{display:block!important" in html
    start = html.index("Market pulse panel: reads only /api/market-pulse")
    block = html[start:html.index("</script>", start)]
    assert "innerHTML" not in block and "EXPERIMENTAL" in block and "LIVE MEASURED" in block
    src = (ROOT / "src/signals_display/server.py").read_text(encoding="utf-8")
    assert 'route == "/api/market-pulse"' in src and "validated_probability=False" in src


def test_top_of_book_only_venues_are_shown_but_not_used():
    s = _snap()
    s["advanced_intelligence"]["sources"]["Coinbase BTC/USD stream"]["order_book"].update(depth_levels=1, multi_level_imbalance=-0.99)
    r = mp.order_book(s)
    assert r["venues"]["Coinbase BTC/USD stream"]["status"].startswith("TOP OF BOOK ONLY")
    assert r["indicator"] == "BUYING PRESSURE" and r["cross_exchange"] == "SINGLE SOURCE"
