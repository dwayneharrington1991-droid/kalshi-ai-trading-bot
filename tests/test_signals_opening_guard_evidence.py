"""The fallback quality guard must judge the fallback on its own record only.

Regression for two defects: (1) candidate-model calls were pooled into the
fallback's evidence, and (2) once the guard fired, suppressed openings were
saved as UNCLEAR and stopped counting, freezing the guard on stale evidence.
"""
import json
from datetime import datetime, timedelta, timezone

from src.signals_display.monitor import Journal

START = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _insert(journal, index, *, model, up, direction, outcome):
    opened = START + timedelta(minutes=15 * index)
    ticker = f"KXBTC15M-GUARD-{index:03d}"
    payload = {"ticker": ticker, "model": model, "up": up, "direction": direction,
               "opens_at": opened.isoformat()}
    with journal.connect() as db:
        db.execute(
            "INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
            ("PUBLIC MARKET DATA", ticker, opened.isoformat(), opened.isoformat(),
             (opened + timedelta(minutes=15)).isoformat(), up, "ON_TIME", json.dumps(payload)),
        )
        db.execute(
            "INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
            (ticker, "official", (opened + timedelta(minutes=16)).isoformat(), outcome),
        )


def test_fallback_evidence_excludes_candidates_and_keeps_counting_withheld_calls(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    fallback = "opening24h-zero-drift-final-average-v1"
    # 30 fallback openings whose call was withheld (saved UNCLEAR). Their
    # recorded probability still implies a direction: 21 of 30 are correct.
    for i in range(30):
        up = 0.56 if i % 2 == 0 else 0.44
        implied_up = up > 0.5
        outcome = int(implied_up) if i < 21 else int(not implied_up)
        _insert(journal, i, model=fallback, up=up, direction="UNCLEAR", outcome=outcome)
    # 10 candidate-model calls, all wrong: must not affect the fallback guard.
    for i in range(30, 40):
        _insert(journal, i, model="opening-candidate/abc", up=0.8, direction="UP", outcome=0)
    readiness = journal.learning_readiness()
    evidence = readiness["opening_fallback_forward_accuracy"]
    assert evidence["directional_samples"] == 30
    assert evidence["correct"] == 21
    assert evidence["accuracy"] == 21 / 30
    # The pre-existing display metric (saved directions, all models) is unchanged.
    assert readiness["opening_forward_accuracy"]["directional_samples"] == 10


def test_guard_threshold_itself_is_unchanged():
    from src.signals_display import prediction_engine

    source = open(prediction_engine.__file__, encoding="utf-8").read()
    assert 'and forward["directional_samples"] >= 20' in source
    assert "and interval[1] < 0.60" in source
