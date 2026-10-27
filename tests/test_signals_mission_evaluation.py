"""Forward-only quality/horizon scoring and bounded, timestamped chart history."""
from copy import deepcopy
from datetime import timedelta
import json
import math

import pytest

from src.signals_display import prediction_engine as engine
from src.signals_display.monitor import Journal, analyze
from src.signals_display.scores import score
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame
from tests.test_signals_prediction_engine import market


def revision(journal, seconds, p, ticker=None, quality="SUPPORTED PROXY INPUTS"):
    at = NOW + timedelta(seconds=seconds)
    m = market()
    if ticker:
        m["ticker"] = ticker
    data = frame(seconds)
    data["markets"] = [m]
    state = analyze(data, at)
    state.update(
        mode="PUBLIC MARKET DATA",
        stale=False,
        estimate={"settlement_up": p},
        model={
            "version": "mock-v1",
            "baseline_probability_up": 0.5,
            "analysis_24h": {
                "status": "READY",
                "regime": "rising volatility",
                "volatility_ratio": 1.7,
            },
        },
        market_comparison={"market_implied_up": 0.55},
    )
    initial = journal.opening_record(m["ticker"], "PUBLIC MARKET DATA")
    state["prediction_engine"] = engine.view(state, opening=initial, generated_at=at)
    state["prediction_engine"]["data_quality"]["status"] = quality
    with journal.connect() as db:
        engine.record(db, state, at)
    return state


def test_scores_report_calibration_uncertainty_and_refuse_invalid_labels():
    report = score([0.2, 0.8], [0, 1])
    assert report["samples"] == 2 and report["accuracy"] == 1
    assert 0 < report["accuracy_interval"][0] < report["accuracy_interval"][1] <= 1
    assert report["expected_calibration_error"] == pytest.approx(0.2)
    assert report["brier"] == pytest.approx(0.04)
    assert report["calibration"][2]["outcome_rate_interval"] is not None
    assert "Correlated" in report["uncertainty_note"]
    assert score([0.5], [1])["directional_accuracy"] is None
    assert score([], [])["accuracy_interval"] is None
    for ps, ys in [([0.2], []), ([math.nan], [0]), ([1.1], [1]), ([0.2], [2])]:
        with pytest.raises(ValueError):
            score(ps, ys)


def test_live_horizon_summaries_are_incremental_separate_and_official_only(tmp_path):
    journal = Journal(tmp_path / "j.db")
    for seconds, p in [(10, 0.6), (20, 0.7), (400, 0.8), (650, 0.9), (850, 0.4)]:
        revision(journal, seconds, p)
    with journal.read() as db:
        assert db.execute("SELECT count(*) FROM btc_engine_predictions").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM btc_engine_horizons").fetchone()[0] == 4
        before = engine.performance(db, NOW + timedelta(minutes=17))
        assert before["by_mode"]["PUBLIC MARKET DATA"]["all"]["settled_markets"] == 0
    journal.settle(
        market()["ticker"],
        "yes",
        "PUBLIC MARKET DATA",
        observed_at=(NOW + timedelta(minutes=16)).isoformat(),
    )
    with journal.read() as db:
        queries = []
        db.set_trace_callback(queries.append)
        report = engine.performance(db, NOW + timedelta(minutes=17))
    assert len([q for q in queries if q.lstrip().upper().startswith("SELECT")]) == 3
    live = report["by_mode"]["PUBLIC MARKET DATA"]["all"]
    assert live["latest"]["accuracy"] == 0 and live["latest"]["samples"] == 1
    assert live["by_time_remaining"][">10m"]["brier"] == pytest.approx(0.09)
    assert live["by_time_remaining"]["<=2m"]["brier"] == pytest.approx(0.36)
    assert live["by_time_remaining"]["2-5m"]["baselines"]["market_implied_up"]["samples"] == 1
    assert live["latest"]["by_data_quality"]["SUPPORTED PROXY INPUTS"]["samples"] == 1
    assert live["latest"]["by_volatility_regime"]["rising volatility"]["samples"] == 1
    assert report["archive"][0]["latest_correct"] is False
    assert report["archive"][0]["latest_data_quality"]["status"] == "SUPPORTED PROXY INPUTS"
    assert report["archive"][0]["threshold"] == market()["strike"]


def test_unavailable_completed_cycles_remain_visible_in_coverage_counts(tmp_path):
    journal = Journal(tmp_path / "j.db")
    missing = {
        "ticker": "KXBTC15M-NO-INPUTS",
        "opens_at": NOW.isoformat(),
        "closes_at": (NOW + timedelta(minutes=15)).isoformat(),
        "generated_at": NOW.isoformat(),
        "data_cutoff": NOW.isoformat(),
        "up": None,
        "status": "INSUFFICIENT DATA",
        "reason": "Missing frozen history",
    }
    journal.opening_record(missing["ticker"], "PUBLIC MARKET DATA", missing, NOW)
    revision(journal, 10, 0.6)
    for ticker in (missing["ticker"], market()["ticker"]):
        journal.settle(
            ticker,
            "yes",
            "PUBLIC MARKET DATA",
            observed_at=(NOW + timedelta(minutes=16)).isoformat(),
        )
    with journal.read() as db:
        report = engine.performance(db, NOW + timedelta(minutes=17))
    counts = report["by_mode"]["PUBLIC MARKET DATA"]["recorded_cycles"]
    assert counts["settled"] == 2 and counts["settled_without_supported_live"] == 1
    assert counts["settled_missing_initial"] == 2
    assert report["archive_total"] == 2
    assert (
        next(r for r in report["archive"] if r["ticker"] == missing["ticker"])["latest_correct"]
        is None
    )


def test_chart_samples_full_contract_without_overwriting_opening_or_using_future(tmp_path):
    journal = Journal(tmp_path / "j.db")
    for second in range(1, 900):
        revision(journal, second, 0.5 + 0.2 * math.sin(second / 50))
    with journal.read() as db:
        report = engine.probability_history(
            db, market()["ticker"], limit=20, now=NOW + timedelta(seconds=700)
        )
    points = report["points"]
    assert len(points) <= 20
    assert points[0]["timestamp"] == (NOW + timedelta(seconds=1)).isoformat()
    assert points[-1]["timestamp"] == (NOW + timedelta(seconds=700)).isoformat()
    assert [p["timestamp"] for p in points] == sorted(p["timestamp"] for p in points)
    assert all(p["up"] + p["down"] == pytest.approx(1) for p in points)
    with journal.read() as db:
        assert (
            engine.probability_history(
                db, market()["ticker"], limit=1, now=NOW + timedelta(seconds=700)
            )["points"][0]["timestamp"]
            == points[-1]["timestamp"]
        )
        with pytest.raises(ValueError):
            engine.probability_history(db, market()["ticker"], limit=181)


def test_record_rejects_future_cutoff_and_keeps_source_time_separate(tmp_path):
    journal = Journal(tmp_path / "j.db")
    state = revision(journal, 10, 0.7)
    state = deepcopy(state)
    state["prediction_engine"]["updated_at"] = (NOW + timedelta(seconds=11)).isoformat()
    state["prediction_engine"]["data_cutoff"] = (NOW + timedelta(seconds=12)).isoformat()
    with journal.connect() as db:
        engine.record(db, state, NOW + timedelta(seconds=11))
        assert db.execute("SELECT count(*) FROM btc_engine_predictions").fetchone()[0] == 1
        payload = json.loads(db.execute("SELECT payload FROM btc_engine_predictions").fetchone()[0])
        assert payload["reference_received_at"] == state["reference"]["observed_at"]
        assert payload["reference_event_at"] is None
        assert payload["horizon"] == ">10m"
        assert payload["confidence"]["status"] == "UNCALIBRATED"
