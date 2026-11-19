import json
import sqlite3
from datetime import datetime, timedelta, timezone

from src.signals_display import opening_shadow as sh
from src.signals_display import opening_shadow_report as rp

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)   # a Monday, ISO week 2026-W45


def _journal(path, openings, outcomes):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE btc_engine_opening (mode TEXT, ticker TEXT, generated_at TEXT, cutoff TEXT, "
               "closes_at TEXT, up REAL, status TEXT, payload TEXT)")
    db.execute("CREATE TABLE forecast_outcomes (ticker TEXT, origin TEXT, observed_at TEXT, outcome INTEGER)")
    for ticker, opens_at, live in openings:
        payload = json.dumps({"opens_at": opens_at, "direction": live})
        db.execute("INSERT INTO btc_engine_opening VALUES ('PUBLIC MARKET DATA',?,?,?,?,?,?,?)",
                   (ticker, opens_at, opens_at, opens_at, 0.5, "ok", payload))
    for ticker, observed_at, outcome in outcomes:
        db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", (ticker, "official", observed_at, outcome))
    db.commit()
    db.close()


def _ledger(path, rows):
    sh.write_ledger(path, rows)


def _shadow(ticker, decision, scored_at, seconds=1.0, generated_at=None):
    return {"ticker": ticker, "opens_at": T0.isoformat(), "generated_at": generated_at or T0.isoformat(),
            "scored_at": scored_at, "seconds_after_open": seconds, "inputs_complete": 1,
            "probability": 0.6 if decision == "UP" else 0.4, "decision": decision, "model_sha256": "m"}


def test_accuracy_is_over_resolved_calls_and_coverage_over_all_eligible(tmp_path):
    openings = [("A", T0.isoformat(), "UP"), ("B", (T0 + timedelta(minutes=15)).isoformat(), "WAIT"),
                ("C", (T0 + timedelta(minutes=30)).isoformat(), "DOWN"), ("D", (T0 + timedelta(minutes=45)).isoformat(), "UP")]
    outcomes = [("A", (T0 + timedelta(minutes=16)).isoformat(), 1), ("C", (T0 + timedelta(minutes=31)).isoformat(), 1)]
    journal = tmp_path / "j.db"
    _journal(journal, openings, outcomes)
    ledger = tmp_path / "led.sqlite3"
    _ledger(ledger, [_shadow("A", "UP", (T0 + timedelta(seconds=30)).isoformat()),
                     _shadow("B", "UP", (T0 + timedelta(minutes=15, seconds=30)).isoformat()),
                     _shadow("C", "DOWN", (T0 + timedelta(minutes=30, seconds=30)).isoformat())])
    with sqlite3.connect(journal) as db:
        report = rp.weekly_report(db, ledger)
    live = report["weeks"]["2026-W45"]["live"]
    shadow = report["weeks"]["2026-W45"]["shadow"]
    assert live["eligible"] == 4 and live["calls_all"] == 3          # A UP, C DOWN, D UP; B WAIT
    assert live["resolved_calls"] == 2 and live["correct"] == 1       # A correct, C wrong
    assert live["accuracy"] == 0.5 and live["coverage"] == 0.75
    assert shadow["calls_all"] == 3 and shadow["correct"] == 1 and shadow["resolved_calls"] == 2
    assert report["weeks"]["2026-W45"]["shadow_unscored_openings"] == 1   # D was never scored


def test_prediction_recorded_after_its_outcome_is_flagged(tmp_path):
    openings = [("A", T0.isoformat(), "WAIT")]
    outcomes = [("A", (T0 + timedelta(seconds=10)).isoformat(), 1)]
    journal = tmp_path / "j.db"
    _journal(journal, openings, outcomes)
    ledger = tmp_path / "led.sqlite3"
    _ledger(ledger, [_shadow("A", "UP", (T0 + timedelta(minutes=5)).isoformat())])
    with sqlite3.connect(journal) as db:
        report = rp.weekly_report(db, ledger)
    assert report["timing_checks"]["scored_after_outcome"] == 1


def test_small_samples_are_marked_and_make_no_claim(tmp_path):
    openings = [("A", T0.isoformat(), "UP")]
    outcomes = [("A", (T0 + timedelta(minutes=16)).isoformat(), 1)]
    journal = tmp_path / "j.db"
    _journal(journal, openings, outcomes)
    with sqlite3.connect(journal) as db:
        report = rp.weekly_report(db, tmp_path / "missing.sqlite3")
    assert report["weeks"]["2026-W45"]["live"]["sufficient_sample"] is False
    assert "insufficient sample" in rp.text_summary(report)


def test_report_states_the_research_targets_and_reads_read_only(tmp_path):
    journal = tmp_path / "j.db"
    _journal(journal, [], [])
    with sqlite3.connect(journal) as db:
        report = rp.weekly_report(db, tmp_path / "none.sqlite3")
    assert report["targets"]["accuracy"] == 0.85 and report["targets"]["coverage"] == 0.90
    assert "research targets" in report["targets"]["note"]


def test_a_replayed_row_scored_after_its_outcome_never_counts_as_prospective(tmp_path):
    openings = [("A", T0.isoformat(), "UP"), ("B", (T0 + timedelta(minutes=15)).isoformat(), "UP")]
    outcomes = [("A", (T0 + timedelta(minutes=16)).isoformat(), 1), ("B", (T0 + timedelta(minutes=31)).isoformat(), 1)]
    journal = tmp_path / "j.db"
    _journal(journal, openings, outcomes)
    ledger = tmp_path / "led.sqlite3"
    _ledger(ledger, [
        _shadow("A", "UP", (T0 + timedelta(seconds=30)).isoformat()),                       # prospective, correct
        _shadow("B", "UP", (T0 + timedelta(minutes=40)).isoformat(),                         # replay after outcome
                generated_at=(T0 + timedelta(minutes=15)).isoformat()),
    ])
    with sqlite3.connect(journal) as db:
        report = rp.weekly_report(db, ledger)
    assert report["timing_checks"]["excluded_non_prospective"] == 1
    assert report["shadow_by_model_version"]["m"]["resolved"] == 1          # only A counts
    assert report["shadow_by_model_version"]["m"]["correct"] == 1
