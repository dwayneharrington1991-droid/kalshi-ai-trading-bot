"""Event-specific forecasts: frozen opening inputs, revisions, and no backfill."""
from copy import deepcopy
from datetime import timedelta
import math
import pytest
from src.signals_display import prediction_engine as engine
from src.signals_display.market_health import diagnose, selection_summary
from src.signals_display.monitor import Journal, analyze
from tests.test_signals_monitor import frame
from tests.test_signals_display import NOW


def history(end=NOW, received=None):
    last = int(end.timestamp()) // 60 * 60
    bars = []
    for i in range(1440):
        p = 100000 + 5 * math.sin(i * 0.1)
        bars.append(
            dict(
                start=last - 86400 + 60 * i,
                open=p,
                high=p + 2,
                low=p - 2,
                close=p + math.sin(i),
                volume=1 + i % 20,
            )
        )
    return {
        "source": "Coinbase synthetic proxy",
        "received_at": (received or end).isoformat(),
        "candles": bars,
    }


def market():
    m = frame()["markets"][0]
    m.update(
        opens_at=NOW.isoformat(),
        closes_at=(NOW + timedelta(minutes=15)).isoformat(),
        mapping_verified=True,
        averaging_rule_verified=True,
        comparison_operator=">=",
    )
    return m


def points():
    return [(NOW - timedelta(seconds=65 - i), 100000 + 0.5 * math.sin(i * 0.2)) for i in range(65)]


def test_24h_context_aligns_four_timeframes_and_ignores_future_bars():
    data = history()
    a = engine.context24(data, NOW)
    assert (
        a["status"] == "READY"
        and a["closed_bars"] == 1440
        and a["timeframes"]["60m"]["closed_bars"] == 23  # partial boundary hours excluded
    )
    assert 0 <= a["features"]["rsi_14"] <= 100 and a["vwap"] > 0
    data["candles"].append(
        dict(start=int(NOW.timestamp()), open=1, high=1, low=1, close=1, volume=100)
    )
    assert engine.context24(data, NOW) == a
    data["candles"].pop(500)
    assert engine.context24(data, NOW)["status"] == "INSUFFICIENT DATA"


def test_opening_forecast_uses_only_preopen_received_data_and_reports_delay():
    data = history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=2))
    m = market()
    first = engine.opening_forecast(m, data, points(), NOW + timedelta(seconds=3))
    assert (
        first["status"] == "DELAYED"
        and first["up"] is not None
        and first["data_cutoff"] == NOW.isoformat()
    )
    assert first["opening_delay_seconds"] == 3
    later = points() + [(NOW + timedelta(seconds=1), 999999)]
    assert engine.opening_forecast(m, data, later, NOW + timedelta(seconds=3))["up"] == first["up"]
    data["received_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert engine.opening_forecast(m, data, points(), NOW + timedelta(seconds=3))["up"] is None


def test_opening_is_immutable_and_survives_restart(tmp_path):
    j = Journal(tmp_path / "j.db")
    m = market()
    opening = engine.opening_forecast(
        m,
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        points(),
        NOW + timedelta(seconds=2),
    )
    a = j.opening_record(m["ticker"], "PUBLIC MARKET DATA", opening, NOW + timedelta(seconds=2))
    b = {**opening, "up": 0.99, "generated_at": (NOW + timedelta(seconds=20)).isoformat()}
    assert j.opening_record(m["ticker"], "PUBLIC MARKET DATA", b, NOW + timedelta(seconds=20)) == a
    assert Journal(tmp_path / "j.db").opening_record(m["ticker"], "PUBLIC MARKET DATA") == a


def test_actual_contract_rules_are_required_and_no_opening_data_means_unavailable():
    m = market()
    m["averaging_rule_verified"] = False
    assert engine.opening_forecast(m, history(), points(), NOW)["up"] is None
    m = market()
    assert engine.opening_forecast(m, history(), [], NOW)["up"] is None


def test_recording_revisions_and_official_scoring_do_not_backfill_or_duplicate(tmp_path):
    j = Journal(tmp_path / "j.db")
    m = market()
    opening = engine.opening_forecast(
        m,
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        points(),
        NOW + timedelta(seconds=1),
    )
    opening["up"] = 0.7
    j.opening_record(m["ticker"], "PUBLIC MARKET DATA", opening, NOW + timedelta(seconds=1))
    data = frame()
    data["markets"] = [m]
    state = analyze(data, NOW)
    state.update(
        mode="PUBLIC MARKET DATA",
        stale=False,
        model={"analysis_24h": engine.context24(history(), NOW), "baseline_probability_up": 0.5},
        estimate={"settlement_up": 0.8},
    )
    state["prediction_engine"] = engine.view(state, None, opening, NOW + timedelta(seconds=2))
    with j.connect() as db:
        engine.record(db, state, NOW + timedelta(seconds=2))
        engine.record(db, state, NOW + timedelta(seconds=2))
        assert db.execute("SELECT count(*) FROM btc_engine_predictions").fetchone()[0] == 2
    j.settle(
        m["ticker"],
        "yes",
        "PUBLIC MARKET DATA",
        observed_at=(NOW + timedelta(minutes=16)).isoformat(),
    )
    with j.connect() as db:
        report = engine.performance(db, NOW + timedelta(minutes=17))["by_mode"][
            "PUBLIC MARKET DATA"
        ]["all"]
        assert (
            report["settled_markets"] == 1
            and report["initial"]["correct"] == report["latest"]["correct"] == 1
        )
        assert report["initial"]["brier"] == pytest.approx(0.09) and report["latest"][
            "brier"
        ] == pytest.approx(0.04)
        engine.record(db, state, NOW + timedelta(minutes=16))
        assert db.execute("SELECT count(*) FROM btc_engine_predictions").fetchone()[0] == 2


def test_market_health_explains_expiry_and_staleness_despite_200():
    p = frame()
    s = analyze(p, NOW)
    health = diagnose(s, NOW + timedelta(seconds=6))
    assert "QUOTE_OLDER_THAN_5_SECONDS" in health["contract_quote_reasons"]
    s["market"]["closes_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert "CONTRACT_EXPIRED" in diagnose(s, NOW + timedelta(seconds=2))["contract_quote_reasons"]
    s["reference"]["observed_at"] = None
    assert "REFERENCE_TIMESTAMP_MISSING_OR_INVALID" in diagnose(s, NOW)["reference_reasons"]
    s["market"]["observed_at"] = (NOW + timedelta(seconds=5)).isoformat()
    assert "QUOTE_TIMESTAMP_IN_FUTURE" in diagnose(s, NOW)["contract_quote_reasons"]


def test_expired_contract_and_ambiguous_candidates_cannot_appear_live():
    m = market()
    s = {"market": None, "reference": {}, "discovered_contracts": selection_summary([m, m], NOW)}
    assert diagnose(s, NOW)["contract_quote_reasons"] == ["MULTIPLE_ACTIVE_CONTRACTS"]
    s["discovered_contracts"] = selection_summary([m], NOW + timedelta(minutes=16))
    assert diagnose(s, NOW + timedelta(minutes=16))["contract_quote_reasons"] == [
        "NO_ACTIVE_CONTRACT"
    ]


def test_walk_forward_has_disjoint_future_blocks_and_training_cutoffs():
    from src.signals_display.learning import walk_forward_compare

    rows = []
    for i in range(230):
        at = NOW + timedelta(minutes=15 * i)
        rows.append(
            dict(
                ticker=f"C{i:03}",
                timestamp=at.isoformat(),
                closes_at=(at + timedelta(minutes=10)).isoformat(),
                label_available_at=(at + timedelta(minutes=11)).isoformat(),
                features={"distance_scaled": (-1) ** i * 0.001, "return_1m": (-1) ** i * 0.0001},
                outcome=i % 2,
                baseline=0.5,
                market=0.5,
                remaining=600,
            )
        )
    r = walk_forward_compare(rows)
    assert len(r["folds"]) == 2
    a, b = r["folds"]
    assert set(a["test_contracts"]).isdisjoint(b["test_contracts"])
    assert (
        a["train_last_label"] < a["test_first_prediction"]
        and a["validation_last_label"] < a["test_first_prediction"]
    )
    assert b["train_last_label"] < b["test_first_prediction"]


def test_http_200_with_old_cache_age_is_not_accepted_as_fresh():
    import httpx
    from src.signals_display.monitor import PublicFeed, COINBASE

    def get(url, **kw):
        return httpx.Response(
            200, json={"data": {}}, headers={"Age": "30"}, request=httpx.Request("GET", url)
        )

    f = PublicFeed(get)
    with pytest.raises(ValueError, match="STALE_HTTP_CACHE"):
        f._read(COINBASE)
    assert (
        f.transport_diagnostics[-1]["http_status"] == 200
        and f.transport_diagnostics[-1]["status"] == "FAILED"
    )


def test_performance_endpoint_does_not_wait_for_legacy_history(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    import threading, json
    from urllib.request import urlopen
    from src.signals_display.server import Monitor, make_handler

    j = Journal(tmp_path / "j.db")
    monitor = Monitor(None, j)

    def forbidden():
        raise AssertionError("Legacy history must not be requested")

    monkeypatch.setattr(j, "history", forbidden)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import time

        deadline = time.monotonic() + 3
        while True:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/api/prediction-performance", timeout=3
            ) as r:
                data = json.load(r)
            if data.get("report_status") == "READY" or data.get("by_mode"):
                break
            assert time.monotonic() < deadline, "Async performance report did not finish"
            time.sleep(0.02)
        assert data["by_mode"]["PUBLIC MARKET DATA"]["all"]["settled_markets"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_opening_panel_reports_supported_price_and_uncalibrated_quality_without_future_inputs():
    h = history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1))
    m = market()
    opening = engine.opening_forecast(
        m, h, points(), NOW + timedelta(seconds=2), reference_source="Mock Coinbase spot"
    )
    assert opening["direction"] in ("UP", "DOWN", "UNCLEAR")
    assert opening["up"] + opening["down"] == pytest.approx(1)
    assert opening["confidence"]["status"] == "UNCALIBRATED"
    assert opening["data_quality"]["closed_history_bars"] == 1440
    assert opening["reference_source"] == "Mock Coinbase spot"
    assert opening["settlement_price_estimate"]["mean"] == pytest.approx(
        points()[-1][1] + opening["basis"]["point"]
    )
    assert "NOT calibrated" in opening["settlement_price_estimate"]["label"]
    assert engine.opening_forecast(m, h, points(), NOW - timedelta(seconds=1))["up"] is None
    assert engine.opening_forecast(m, h, points(), NOW + timedelta(minutes=15))["up"] is None
    # Receipt order must not change the result, but contradictory samples cannot
    # be silently chosen by list order.
    shuffled = engine.opening_forecast(m, h, list(reversed(points())), NOW + timedelta(seconds=2))
    assert shuffled["up"] == opening["up"]
    conflict = engine.opening_forecast(
        m, h, points() + [(points()[-1][0], 1)], NOW + timedelta(seconds=2)
    )
    assert conflict["up"] is None and "Conflicting" in conflict["reason"]


def test_live_panel_clears_current_estimate_without_overwriting_frozen_opening():
    m = market()
    opening = engine.opening_forecast(
        m, history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)), points(), NOW
    )
    data = frame()
    data["markets"] = [m]
    state = analyze(data, NOW)
    state.update(
        stale=False,
        estimate={"settlement_up": 0.6},
        model={
            "analysis_24h": engine.context24(history(), NOW),
            "price_forecast": {
                "settlement_average_mean": 100001,
                "settlement_average_conditional_interval": [99900, 100100],
                "interval_label": "Experimental",
            },
        },
    )
    live = engine.view(state, None, opening, NOW)
    assert live["direction"] == "UP" and live["settlement_price_estimate"]["mean"] == 100001
    assert live["initial"] == opening
    state["stale"] = True
    stale = engine.view(state, None, opening, NOW + timedelta(seconds=6))
    assert stale["up"] is None and stale["settlement_price_estimate"] is None
    assert stale["direction"] == "UNCLEAR" and stale["initial"] == opening


def test_contract_summary_is_incremental_idempotent_and_retains_unavailable_openings(tmp_path):
    j = Journal(tmp_path / "j.db")
    m = market()
    missing = engine.opening_forecast(m, history(), [], NOW)
    j.opening_record(m["ticker"], "PUBLIC MARKET DATA", missing, NOW)
    with j.connect() as db:
        for i in range(300):
            at = (NOW + timedelta(seconds=i + 1)).isoformat()
            args = (
                "PUBLIC MARKET DATA",
                m["ticker"],
                at,
                at,
                m["closes_at"],
                0.6,
                "similar volatility",
                engine.VERSION,
                '{"baseline_up":0.5}',
            )
            db.execute(
                "INSERT OR IGNORE INTO btc_engine_predictions VALUES (?,?,?,?,?,?,?,?,?)", args
            )
            db.execute(
                "INSERT OR IGNORE INTO btc_engine_predictions VALUES (?,?,?,?,?,?,?,?,?)", args
            )
        engine.create(db)
        summary = db.execute("SELECT * FROM btc_engine_contracts").fetchone()
        assert summary["revisions"] == 300
        queries = []
        db.set_trace_callback(queries.append)
        prior = engine.prior(db, m["ticker"], "PUBLIC MARKET DATA")
        db.set_trace_callback(None)
        assert len(queries) == 2 and all("LIMIT 1" in q for q in queries)
        assert prior["latest"]["up"] == 0.6
        report = engine.performance(db, NOW)
        assert report["archive_total"] == 1 and report["archive"][0]["revisions"] == 300
        assert report["archive"][0]["initial"]["up"] is None
    # An unavailable opening without a single supported live revision also
    # remains in the archive, separate from scored predictions.
    another = {**missing, "ticker": "KXBTC15M-UNAVAILABLE"}
    j.opening_record(another["ticker"], "PUBLIC MARKET DATA", another, NOW)
    with j.read() as db:
        report = engine.performance(db, NOW)
    assert report["archive_total"] == 2
    assert any(
        a["ticker"] == another["ticker"] and a["latest_up"] is None for a in report["archive"]
    )


def test_scoring_selects_last_eligible_revision_without_future_recording(tmp_path):
    j = Journal(tmp_path / "j.db")
    m = market()
    opened = engine.opening_forecast(
        m,
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        points(),
        NOW + timedelta(seconds=1),
    )
    j.opening_record(m["ticker"], "PUBLIC MARKET DATA", opened, NOW + timedelta(seconds=1))
    with j.connect() as db:
        # Last inserted revision is falsely dated before recording. It must not
        # replace the last chronologically eligible forecast in accuracy stats.
        for seconds, recorded, p in [(10, 10, 0.8), (20, 16 * 60, 0.99), (30, 29, 0.01)]:
            at = (NOW + timedelta(seconds=seconds)).isoformat()
            db.execute(
                "INSERT INTO btc_engine_predictions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "PUBLIC MARKET DATA",
                    m["ticker"],
                    at,
                    (NOW + timedelta(seconds=recorded)).isoformat(),
                    m["closes_at"],
                    p,
                    "similar volatility",
                    engine.VERSION,
                    "{}",
                ),
            )
    j.settle(
        m["ticker"],
        "yes",
        "PUBLIC MARKET DATA",
        observed_at=(NOW + timedelta(minutes=16)).isoformat(),
    )
    with j.read() as db:
        queries = []
        db.set_trace_callback(queries.append)
        report = engine.performance(db, NOW + timedelta(minutes=17))
    assert report["by_mode"]["PUBLIC MARKET DATA"]["all"]["latest"]["brier"] == pytest.approx(0.04)
    # One scoring seek query plus bounded archive query and summary count;
    # there are no per-contract SELECTs inside metric loops.
    assert len([q for q in queries if q.lstrip().upper().startswith("SELECT")]) == 3
