from datetime import datetime, timedelta, timezone

from src.signals_display.selective_evaluation import selective_report


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def rows(count, prefix="K", *, regime=None):
    return [
        {
            "ticker": f"{prefix}{i:03d}",
            "timestamp": (NOW + timedelta(minutes=15 * i)).isoformat(),
            "outcome": i % 2,
            "volatility_regime": regime,
        }
        for i in range(count)
    ]


def confident_for(records):
    return [0.9 if row["outcome"] else 0.1 for row in records]


def test_threshold_is_selected_on_validation_and_reports_test_coverage():
    validation = rows(30, "V")
    test = rows(30, "T", regime="high")
    report = selective_report(confident_for(validation), validation, confident_for(test), test)
    assert report["selected_before_test"] is True
    assert report["selected_threshold"] == 0.55
    assert report["test"]["independent_signals"] == 30
    assert report["test"]["actionable_market_coverage"] == 1
    assert report["test"]["actionable_directional_accuracy"] == 1
    assert report["test"]["accuracy_interval_95"][0] < 1
    assert report["test"]["by_regime"]["volatility"]["high"]["independent_signals"] == 30
    assert set(report["coverage_scenarios"]) == {"10%", "25%", "50%", "75%", "100%"}
    assert all(
        scenario["selected_before_test"] is True
        for scenario in report["coverage_scenarios"].values()
    )
    assert report["promotion_allowed"] is False


def test_future_test_labels_cannot_change_selected_threshold():
    validation = rows(30, "V")
    test = rows(30, "T")
    a = selective_report(confident_for(validation), validation, confident_for(test), test)
    reversed_test = [{**row, "outcome": 1 - row["outcome"]} for row in test]
    b = selective_report(confident_for(validation), validation, confident_for(test), reversed_test)
    assert a["selected_threshold"] == b["selected_threshold"]
    assert a["validation"] == b["validation"]
    assert {
        name: item["selected_threshold"] for name, item in a["coverage_scenarios"].items()
    } == {
        name: item["selected_threshold"] for name, item in b["coverage_scenarios"].items()
    }
    assert a["test"]["actionable_directional_accuracy"] != b["test"]["actionable_directional_accuracy"]


def test_repeated_rows_count_once_per_contract_and_wait_is_preserved():
    validation = rows(20, "V")
    test = rows(4, "T")
    duplicated = [item for row in test for item in (row, {**row, "timestamp": (datetime.fromisoformat(row["timestamp"]) + timedelta(seconds=1)).isoformat()})]
    report = selective_report([0.5] * len(validation), validation, [0.9, 0.8] * 4, duplicated)
    assert report["status"] == "INSUFFICIENT_VALIDATION_SIGNALS"
    assert report["test"] is None


def test_costs_are_unavailable_not_zero_and_quote_costs_are_reported():
    validation = rows(30, "V")
    test = rows(30, "T")
    unavailable = selective_report(confident_for(validation), validation, confident_for(test), test)
    assert unavailable["test"]["after_cost_evidence"]["status"] == "UNAVAILABLE"
    priced = [
        {
            **row,
            "executable_quotes": {
                "up": {"ask": 0.6},
                "down": {"ask": 0.6},
            },
            "fee_multiplier": 0.07,
        }
        for row in test
    ]
    available = selective_report(confident_for(validation), validation, confident_for(priced), priced)
    evidence = available["test"]["after_cost_evidence"]
    assert evidence["status"] == "AVAILABLE"
    assert evidence["quote_time_simulated_fills"] == 30
    assert evidence["simulated_net_pnl"] > 0
    assert available["test"]["actual_realized_results"] is None
