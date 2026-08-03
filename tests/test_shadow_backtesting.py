import math

import pytest

from src.agents.calibration import calibration_report
from src.agents.performance import recommend_weights
from src.backtesting.costs import CostModel
from src.backtesting.metrics import brier_score, log_loss, maximum_drawdown, summarize
from src.backtesting.replay import ReplayEngine, ReplaySnapshot
from src.backtesting.report import csv_report, json_report


def snapshots():
    return [
        ReplaySnapshot("1", "A", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", .8, .6, .02, 10, 1, "BUY_YES"),
        ReplaySnapshot("2", "B", "2026-01-01T01:00:00+00:00", "2026-01-02T00:00:00+00:00", .2, .4, .03, 10, 1, "BUY_NO"),
        ReplaySnapshot("3", "C", "2026-01-01T02:00:00+00:00", "2026-01-02T00:00:00+00:00", .5, .5, .01, 0, 0, "HOLD"),
    ]


def test_metrics_costs_abstention_and_drawdown():
    values = [(.8, 1), (.2, 0)]
    assert brier_score(values) == pytest.approx(.04)
    assert math.isfinite(log_loss([(0, 0), (1, 1)]))
    assert maximum_drawdown([2, -1, -3, 4]) == 4
    result = ReplayEngine(CostModel(fee_rate=.01, slippage=.01), seed=7).run(snapshots())
    assert result == ReplayEngine(CostModel(fee_rate=.01, slippage=.01), seed=7).run(snapshots())
    with pytest.raises(ValueError, match="chronological"):
        ReplayEngine(CostModel(fee_rate=.01, slippage=.01), seed=7).run(reversed(snapshots()))
    assert result["metrics"]["coverage_rate"] == pytest.approx(2 / 3)
    assert result["metrics"]["abstention_rate"] == pytest.approx(1 / 3)
    assert result["metrics"]["simulated_pnl"] < 1


def test_replay_refuses_missing_duplicate_and_lookahead():
    engine = ReplayEngine(CostModel())
    with pytest.raises(ValueError, match="required"):
        engine.run([])
    with pytest.raises(ValueError, match="duplicate"):
        engine.run([snapshots()[0], snapshots()[0]])
    row = snapshots()[0]
    invalid = ReplaySnapshot(row.snapshot_id, row.market_id, row.settlement_timestamp, row.timestamp, .5, .5, 0, 1, 1, "HOLD")
    with pytest.raises(ValueError, match="look-ahead"):
        engine.run([invalid])


def test_calibration_buckets_and_sample_warning():
    report = calibration_report([(0.0, 0), (.1, 0), (.71, 1), (.72, 0), (1.0, 1)], minimum_samples=3)
    bucket = next(item for item in report if item.count)
    assert bucket.status == "INSUFFICIENT_SAMPLE"
    assert bucket.confidence_interval is not None
    assert report[0].count == 1
    assert report[1].count == 1
    assert report[-1].count == 1


def test_weight_learning_is_bounded_reproducible_and_never_applied():
    current = {"a": .5, "b": .5}
    insufficient = recommend_weights({"a": (10, .1), "b": (10, .2)}, current)
    assert insufficient.weights == current and not insufficient.applied
    first = recommend_weights({"a": (200, .1), "b": (200, .4)}, current, maximum_change=.05, maximum_weight=.9)
    second = recommend_weights({"a": (200, .1), "b": (200, .4)}, current, maximum_change=.05, maximum_weight=.9, auto_apply=True)
    assert first == second
    assert not first.applied
    assert all(abs(first.weights[name] - current[name]) <= .051 for name in current)


def test_reports_are_machine_readable():
    result = ReplayEngine(CostModel(), seed=1).run(snapshots())
    assert '"seed": 1' in json_report(result)
    assert "market_id" in csv_report(result["rows"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_metrics_and_costs_reject_invalid_numeric_values(value):
    with pytest.raises(ValueError):
        brier_score([(value, 1)])
    with pytest.raises(ValueError):
        ReplayEngine(CostModel()).run([
            ReplaySnapshot("bad", "M", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", value, .5, 0, 1, 1, "HOLD")
        ])
    with pytest.raises(ValueError):
        CostModel(slippage=value)


def test_reports_redact_secret_shaped_fields_and_values():
    secret = "sk-SUPER_SECRET_VALUE"
    rendered_json = json_report({"api_key": secret, "value": secret, "private_path": "C:/secret.pem"})
    rendered_csv = csv_report([{"authorization": secret, "value": secret}])
    assert secret not in rendered_json
    assert secret not in rendered_csv
    assert ".pem" not in rendered_json
