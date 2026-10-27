"""Offline integration of real-volume descriptors with the existing dashboard.

All market/account inputs are synthetic. Tests run local HTTP only and never
create a trading client, load credentials, or contact an exchange.
"""
from copy import deepcopy
from datetime import timedelta
from http.server import ThreadingHTTPServer
import json
import math
import threading
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from src.signals_display.advanced import AdvancedIntelligence, attach_reliability, refresh_status
from src.signals_display.features import extract
from src.signals_display.model import timestamp
from src.signals_display.monitor import Journal, PublicFeed, analyze
from src.signals_display import prediction_engine as engine
from src.signals_display.server import Monitor, make_handler
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame
from tests.test_signals_prediction_engine import history, market, points


FAMILIES = {
    "rsi",
    "macd",
    "vwap",
    "bollinger",
    "ema",
    "volume_profile",
    "order_flow",
    "order_book",
    "cvd",
    "microstructure",
    "ensemble",
    "calibration",
}
SOURCE = "Coinbase BTC/USD stream"


def observations(end=NOW, seconds=900, *, source=SOURCE):
    result = []
    for offset in range(-seconds, 1):
        at = end + timedelta(seconds=offset)
        price = 100000 + 2 * math.sin(offset / 20) + offset / 100
        shared = dict(
            source=source,
            event_at=at.isoformat(),
            received_at=at.isoformat(),
            price=price,
            session_id="mock-capture",
            quantity_unit="BTC",
        )
        result.extend(
            [
                dict(
                    shared,
                    kind="quote",
                    bid=price - 1,
                    ask=price + 1,
                    bid_depth=3,
                    ask_depth=2,
                    depth_levels=1,
                ),
                dict(
                    shared,
                    kind="trade",
                    volume=1 + offset % 3,
                    aggressor="buy" if offset % 4 else "sell",
                    trade_id="mock-" + str(offset),
                ),
            ]
        )
    return result


def close_monitor(monitor):
    for name in ("engine_report", "research_report", "scalp_paper_report", "scheduled_learning"):
        report = getattr(monitor, name, None)
        if report is not None:
            report.close()
    for name in ("account_pool", "manual_pool", "maintenance_pool", "readiness_pool"):
        getattr(monitor, name).shutdown(wait=True, cancel_futures=True)


def integrated_frame():
    payload = frame()
    payload["markets"] = [market()]
    payload["btc_candles"] = history(NOW, NOW)
    payload["reference"].update(
        source="Coinbase BTC/USD proxy", transport="public WebSocket", official=False
    )
    payload["stream_observations"] = observations()
    return payload


def test_advanced_assembler_connects_all_twelve_families_and_real_thirty_second_bars():
    advanced = AdvancedIntelligence()
    for observation in observations():
        advanced.observe(observation)
    result = advanced.summarize(NOW, candles=history(), primary_source=SOURCE, threshold=100000)
    assert set(result["families"]) == FAMILIES
    assert result["families"]["rsi"]["reading"]["30s"]["status"] == "READY"
    assert result["candidate_features"]["advanced_30s_wilder_rsi14"] is not None
    actual_trades = [row for row in observations() if row["kind"] == "trade"]
    expected_vwap = sum(row["price"] * row["volume"] for row in actual_trades) / sum(
        row["volume"] for row in actual_trades
    )
    assert result["families"]["vwap"]["session"] == pytest.approx(expected_vwap)
    assert result["families"]["volume_profile"]["status"] == "READY"
    decorated = advanced.decorate(result, {}, NOW, probability=0.5)
    assert not any(family["contributing"] for family in decorated["families"].values())
    assert decorated["strongest_validated_factors"] == []
    assert all(
        timestamp(proof["received_at"]) <= NOW
        for proof in result["candidate_feature_sources"].values()
    )


def test_derivative_contract_volume_never_becomes_spot_vwap_or_spot_order_flow():
    advanced = AdvancedIntelligence()
    for observation in observations(seconds=2, source="Kraken futures stream"):
        observation["quantity_unit"] = "derivative contracts"
        advanced.observe(observation)
    result = advanced.summarize(NOW, candles=history(), threshold=100000)
    assert result["primary_source"] is None
    assert result["families"]["vwap"]["status"] == "UNAVAILABLE"
    assert result["families"]["order_flow"]["status"] == "UNAVAILABLE"
    assert result["candidate_features"].get("advanced_flow_imbalance") is None
    assert result["sources"]["Kraken futures stream"]["quantity_unit"] == "derivative contracts"
    # Futures remain separately inspectable rather than silently discarded.
    assert result["sources"]["Kraken futures stream"]["vwap"]["status"] == "READY"


def test_actual_monitor_publishes_and_journals_shadow_features_before_outcomes(tmp_path):
    payload = integrated_frame()
    # Injected getter is a hard boundary: this test must never use live APIs.
    feed = PublicFeed(lambda *_args, **_kwargs: pytest.fail("Unexpected exchange request"))
    feed.poll = lambda: deepcopy(payload)
    journal = Journal(tmp_path / "signals.db")
    monitor = Monitor(feed, journal)
    monitor.estimator.reference.extend(points())
    monitor.estimator.reference_source = payload["reference"]["source"]
    try:
        monitor.tick()
        state = monitor.state
        assert not state.get("connection_error"), state.get("warnings")
        assert set(state["advanced_intelligence"]["families"]) == FAMILIES
        values = state["model"]["candidate_features"]["values"]
        assert values["advanced_1m_wilder_rsi14"] is not None
        assert values["advanced_vwap_deviation_fraction"] is not None
        assert values["advanced_flow_imbalance"] is not None
        assert state["trade_decision"]["action"] == "WAIT"
        assert state["trade_decision"]["manual_only"]
        assert monitor.account_client is None
        with journal.read() as db:
            assert db.execute("SELECT COUNT(*) FROM forecast_outcomes").fetchone()[0] == 0
            saved = json.loads(
                db.execute("SELECT payload FROM predictions ORDER BY id DESC LIMIT 1").fetchone()[0]
            )
        assert (
            saved["model"]["candidate_features"]["values"]["advanced_1m_wilder_rsi14"]
            == values["advanced_1m_wilder_rsi14"]
        )
        assert (
            saved["model"]["candidate_features"]["values"]["advanced_flow_imbalance"]
            == values["advanced_flow_imbalance"]
        )
        opening = deepcopy(state["prediction_engine"]["initial"])
        frozen = opening["frozen_features"]
        assert frozen["values"]["advanced_1m_wilder_rsi14"] is not None
        assert timestamp(frozen["max_input_received_at"]) <= NOW
        # Next update changes live inputs, never the saved opening assessment.
        payload["clock"] = payload["now"] = (NOW + timedelta(seconds=1)).isoformat()
        payload["reference"].update(price=100100, observed_at=payload["now"])
        payload["markets"][0]["observed_at"] = payload["now"]
        payload["stream_observations"] = [
            dict(
                source=SOURCE,
                event_at=payload["now"],
                received_at=payload["now"],
                price=100100,
                volume=1,
                aggressor="buy",
                trade_id="next",
                kind="trade",
                session_id="mock-capture",
            )
        ]
        monitor.tick()
        assert monitor.state["prediction_engine"]["initial"] == opening
        # Contract rollover replaces the main identity immediately, even when
        # opening inputs for the next market are insufficient. It never copies
        # the old contract's forecast into a new panel.
        rolled_at = NOW + timedelta(minutes=15)
        payload["clock"] = payload["now"] = rolled_at.isoformat()
        payload["reference"]["observed_at"] = payload["now"]
        payload["markets"][0].update(
            ticker="KXBTC15M-MOCK-NEXT",
            opens_at=payload["now"],
            closes_at=(rolled_at + timedelta(minutes=15)).isoformat(),
            observed_at=payload["now"],
            strike=100100,
        )
        payload["btc_candles"] = history(rolled_at, rolled_at)
        payload["stream_observations"] = []
        monitor.tick()
        assert monitor.state["market"]["ticker"] == "KXBTC15M-MOCK-NEXT"
        assert monitor.state["prediction_engine"]["initial"]["ticker"] == "KXBTC15M-MOCK-NEXT"
        assert set(monitor.state["advanced_intelligence"]["families"]) == FAMILIES
        assert journal.opening_record(opening["ticker"], "REPLAY / SIMULATED") == opening
    finally:
        close_monitor(monitor)


def test_cached_live_analysis_cannot_leak_post_opening_trades_into_opening_features():
    advanced = AdvancedIntelligence()
    for observation in observations():
        advanced.observe(observation)
    pre = advanced.summarize(NOW, candles=history(), primary_source=SOURCE)
    future = dict(
        source=SOURCE,
        event_at=(NOW + timedelta(seconds=1)).isoformat(),
        received_at=(NOW + timedelta(seconds=1)).isoformat(),
        price=200000,
        volume=100000,
        aggressor="buy",
        trade_id="future",
        kind="trade",
        session_id="mock-capture",
    )
    advanced.observe(future)
    advanced.summarize(NOW + timedelta(seconds=1), candles=history(), primary_source=SOURCE)
    frozen = advanced.summarize(NOW, candles=history(), primary_source=SOURCE)
    assert frozen["candidate_features"] == pre["candidate_features"]
    assert frozen["max_input_received_at"] <= NOW.isoformat()
    assert all(
        timestamp(proof["received_at"]) <= NOW
        for proof in frozen["candidate_feature_sources"].values()
    )
    # Defend again at the feature extraction boundary, even if a caller supplies
    # a forged feature from after this prediction's cutoff.
    payload = integrated_frame()
    payload["advanced_intelligence"] = {
        "candidate_features": {"advanced_flow_imbalance": 0.99},
        "candidate_feature_sources": {
            "advanced_flow_imbalance": {"received_at": future["received_at"]}
        },
    }
    assert extract(payload, payload["markets"][0], NOW)["values"]["advanced_flow_imbalance"] is None


def test_opening_boundary_rejects_future_advanced_proof_without_altering_probability():
    base_args = (
        market(),
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        points(),
        NOW + timedelta(seconds=2),
    )
    baseline = engine.opening_forecast(*base_args)
    advanced = {
        "cutoff": NOW.isoformat(),
        "candidate_features": {"advanced_1m_wilder_rsi14": 60},
        "candidate_feature_sources": {
            "advanced_1m_wilder_rsi14": {
                "received_at": (NOW - timedelta(seconds=1)).isoformat(),
                "event_at": (NOW - timedelta(minutes=1)).isoformat(),
            }
        },
    }
    result = engine.opening_forecast(
        *base_args, frozen_context={"cutoff": NOW.isoformat(), "advanced_features": advanced}
    )
    assert result["frozen_features"]["values"]["advanced_1m_wilder_rsi14"] == 60
    assert result["up"] == baseline["up"]
    advanced["candidate_feature_sources"]["advanced_1m_wilder_rsi14"]["received_at"] = (
        NOW + timedelta(milliseconds=1)
    ).isoformat()
    blocked = engine.opening_forecast(
        *base_args, frozen_context={"cutoff": NOW.isoformat(), "advanced_features": advanced}
    )
    assert blocked["frozen_features"]["values"]["advanced_1m_wilder_rsi14"] is None
    assert blocked["up"] == baseline["up"]


def test_calibration_panel_reads_actual_engine_performance_shape():
    empty = AdvancedIntelligence().summarize(NOW)
    metrics = {
        "samples": 7,
        "brier": 0.24,
        "log_loss": 0.68,
        "expected_calibration_error": 0.04,
        "calibration": [
            {"samples": 7, "low": 0.4, "high": 0.5, "mean_probability": 0.45, "outcome_rate": 0.5}
        ],
        "by_volatility_regime": {"normal": {"samples": 7}},
    }
    attached = attach_reliability(
        empty,
        {
            "status": "READY",
            "generated_at": NOW.isoformat(),
            "value": {
                "by_mode": {"PUBLIC MARKET DATA": {"all": {"initial": metrics, "latest": metrics}}}
            },
        },
    )
    reading = attached["families"]["calibration"]["reading"]
    assert reading["opening"]["samples"] == reading["live"]["samples"] == 7
    assert reading["live"]["brier"] == 0.24
    assert reading["live"]["calibration"][0]["samples"] == 7
    assert not attached["families"]["calibration"]["contributing"]


def test_main_http_stays_thin_and_details_are_lazy_and_read_only(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "j.db"))
    monitor.state = analyze(frame(), NOW)
    details = AdvancedIntelligence().summarize(NOW, candles=history())
    details["families"]["volume_profile"]["details_test"] = "x" * 150000
    monitor.state["advanced_intelligence"] = details
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        started = perf_counter()
        with urlopen(base + "/api/state?view=main", timeout=2) as response:
            main = json.load(response)
        assert perf_counter() - started < 1
        assert "advanced_intelligence" not in main
        assert len(json.dumps(main)) < 100000
        with urlopen(base + "/api/details", timeout=2) as response:
            full = json.load(response)
        assert set(full["advanced_intelligence"]["families"]) == FAMILIES
        assert len(json.dumps(full)) > 150000
        # Dashboard writes remain local journal operations; it has no broker route.
        request = Request(
            base + "/api/orders",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=2)
        assert error.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        close_monitor(monitor)


def test_cached_technical_readings_expire_inside_the_same_thirty_second_bucket():
    advanced = AdvancedIntelligence()
    data = history()
    at_limit = advanced.summarize(NOW + timedelta(seconds=120), candles=data)
    assert at_limit["families"]["rsi"]["status"] == "READY"
    assert at_limit["families"]["rsi"]["age_seconds"] == 120
    # 120s and 130s share a cache bucket. Aging must still invalidate the
    # readings instead of returning an indefinitely READY cached indicator.
    expired = advanced.summarize(NOW + timedelta(seconds=130), candles=data)
    assert expired["families"]["rsi"]["status"] == "UNAVAILABLE"
    assert expired["candidate_features"]["advanced_1m_wilder_rsi14"] is None


def test_cached_snapshot_ages_advanced_diagnostics_and_withholds_contribution(
    tmp_path, monkeypatch
):
    from src.signals_display import server as server_module

    advanced = AdvancedIntelligence()
    for observation in observations(seconds=2):
        advanced.observe(observation)
    raw = advanced.summarize(NOW, candles=history(), primary_source=SOURCE)
    artifact = {
        "model": {
            "preprocessor": {"names": ["advanced_1m_wilder_rsi14", "advanced_flow_imbalance"]}
        }
    }
    fresh = advanced.decorate(raw, {}, NOW, artifact=artifact)
    assert fresh["families"]["rsi"]["contributing"]
    assert fresh["families"]["order_flow"]["contributing"]
    monitor = Monitor(None, Journal(tmp_path / "j.db"))
    monitor.state = analyze(frame(), NOW)
    monitor.state["advanced_intelligence"] = fresh
    monitor.last_tick = NOW
    monkeypatch.setattr(server_module, "utcnow", lambda: NOW + timedelta(seconds=130))
    try:
        result = monitor.snapshot()
        families = result["advanced_intelligence"]["families"]
        assert families["rsi"]["status"] == "STALE"
        assert families["rsi"]["age_seconds"] == 130
        assert families["order_flow"]["status"] == "STALE"
        assert not families["rsi"]["contributing"]
        assert not families["order_flow"]["contributing"]
        assert result["trade_decision"]["action"] == "WAIT"
        # Cached publication is immutable; freshness is a view at request time.
        assert fresh["families"]["rsi"]["status"] == "READY"
        assert fresh["families"]["rsi"]["contributing"]
        assert (
            refresh_status(fresh, NOW - timedelta(seconds=1))["families"]["rsi"]["status"]
            == "STALE"
        )
    finally:
        close_monitor(monitor)


def test_serialized_feature_name_does_not_claim_an_unavailable_family_contributes():
    advanced = AdvancedIntelligence()
    raw = advanced.summarize(NOW)
    names = ["advanced_1m_wilder_rsi14", "advanced_flow_imbalance", "advanced_book_imbalance"]
    artifact = {"model": {"preprocessor": {"names": names}}}
    result = advanced.decorate(raw, {}, NOW, artifact=artifact)
    for family in ("rsi", "order_flow", "order_book"):
        assert not result["families"][family]["contributing"]
        assert result["families"][family]["active_features"] == []


def test_fresh_compatible_spot_fallback_replaces_stale_preferred_spot():
    advanced = AdvancedIntelligence()
    for observation in observations(end=NOW - timedelta(seconds=10), seconds=2):
        if observation["kind"] == "trade":
            observation["aggressor"] = "sell"
        advanced.observe(observation)
    for observation in observations(seconds=2, source="Kraken BTC/USD stream"):
        if observation["kind"] == "trade":
            observation["aggressor"] = "buy"
        advanced.observe(observation)
    result = advanced.summarize(NOW, candles=history(), primary_source=SOURCE)
    assert result["primary_source"] == "Kraken BTC/USD stream"
    assert result["families"]["order_flow"]["source"] == "Kraken BTC/USD stream"
    assert result["families"]["order_flow"]["status"] == "READY"
    assert result["candidate_features"]["advanced_flow_imbalance"] == 1
    assert result["sources"][SOURCE]["status"] == "STALE"


def test_actual_monitor_independent_consumers_keep_delayed_trade_volume(tmp_path):
    payload = integrated_frame()
    clock = NOW + timedelta(seconds=1)
    payload["now"] = payload["clock"] = clock.isoformat()
    payload["reference"]["observed_at"] = clock.isoformat()
    payload["markets"][0]["observed_at"] = clock.isoformat()
    payload["stream_observations"] = [
        dict(
            source=SOURCE,
            kind="quote",
            price=100020,
            bid=100019,
            ask=100021,
            bid_depth=3,
            ask_depth=2,
            depth_levels=1,
            session_id="channels",
            received_at=NOW.isoformat(),
            event_at=NOW.isoformat(),
        ),
        dict(
            source=SOURCE,
            kind="trade",
            price=100003,
            volume=7,
            aggressor="buy",
            trade_id="late-channel-execution",
            session_id="channels",
            received_at=(NOW + timedelta(milliseconds=100)).isoformat(),
            event_at=(NOW - timedelta(milliseconds=500)).isoformat(),
        ),
    ]
    feed = PublicFeed(lambda *_args, **_kwargs: pytest.fail("Unexpected exchange request"))
    feed.poll = lambda: deepcopy(payload)
    monitor = Monitor(feed, Journal(tmp_path / "j.db"))
    try:
        monitor.tick()
        state = monitor.state
        assert not state.get("connection_error"), state.get("warnings")
        # The legacy source-price sampler rejects the older exchange timestamp;
        # the separate executed-volume consumer must still receive that record.
        assert state["model"]["market_intelligence"]["rejected_observations_this_update"] >= 1
        families = state["advanced_intelligence"]["families"]
        assert families["vwap"]["session"] == 100003
        assert families["order_flow"]["aggressive_buy_volume"] == 7
        assert families["cvd"]["session_net_delta"] == 7
        assert families["order_book"]["bid"] == 100019
        assert state["trade_decision"]["action"] == "WAIT"
    finally:
        close_monitor(monitor)


def test_actual_effective_spread_and_profile_contribution_excludes_zero_weight_members():
    from src.signals_display.advanced import artifact_features

    advanced = AdvancedIntelligence()
    snapshot = advanced.summarize(NOW, candles=history())
    snapshot["families"]["microstructure"]["status"] = "READY"
    snapshot["families"]["volume_profile"]["status"] = "READY"
    snapshot["candidate_features"]["advanced_effective_spread"] = 0.1
    snapshot["candidate_features"]["advanced_profile_threshold_concentration"] = 0.3
    artifact = {
        "model": {
            "kind": "ensemble",
            "mixture_weights": [0, 1],
            "components": [
                {"preprocessor": {"names": ["advanced_profile_threshold_concentration"]}},
                {"preprocessor": {"names": ["advanced_effective_spread"]}},
            ],
        }
    }
    assert artifact_features(artifact) == {"advanced_effective_spread"}
    decorated = advanced.decorate(snapshot, {}, NOW, artifact=artifact)
    assert decorated["families"]["microstructure"]["contributing"]
    assert not decorated["families"]["volume_profile"]["contributing"]


def test_legacy_candle_features_explain_approximation_without_changing_values():
    context = engine.context24(history(), NOW)
    assert "not actual executed-trade VWAP" in context["vwap_label"]
    assert "simple-average" in context["feature_definitions"]["rsi_14"]
    assert context["features"]["vwap_deviation"] is not None
