"""Chronological selective-signal reporting with abstention.

The decision threshold is selected exclusively on validation data.  The test
block is then scored once, with at most one independently counted signal per
contract.  This module reports evidence; it never promotes a model or changes
live signal policy.
"""
from __future__ import annotations

import math

from .live_model import wilson
from .model import timestamp
from .scores import horizon, score


PROTOCOL = "validation-selected-abstention-forward-test-v1"
THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
TARGET_ACCURACY = 0.80
MIN_VALIDATION_SIGNALS = 15
COVERAGE_TARGETS = (0.10, 0.25, 0.50, 0.75, 1.0)


def _direction(probability, threshold):
    if probability >= threshold:
        return 1
    if probability <= 1 - threshold:
        return 0
    return None


def _independent(probabilities, rows, threshold):
    """Return the first chronological qualifying signal for each contract."""
    candidates = sorted(
        zip(probabilities, rows),
        key=lambda item: (timestamp(item[1]["timestamp"]), item[1]["ticker"]),
    )
    selected = {}
    for probability, row in candidates:
        if (
            type(probability) not in (int, float)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            continue
        direction = _direction(probability, threshold)
        if direction is not None:
            selected.setdefault(row["ticker"], (probability, direction, row))
    return list(selected.values())


def _metrics(probabilities, rows, threshold):
    contracts = {row["ticker"] for row in rows}
    signals = _independent(probabilities, rows, threshold)
    correct = sum(direction == row["outcome"] for _, direction, row in signals)
    count = len(signals)
    up = [(direction, row) for _, direction, row in signals if direction == 1]
    down = [(direction, row) for _, direction, row in signals if direction == 0]
    probabilities_selected = [probability for probability, _, _ in signals]
    outcomes = [row["outcome"] for _, _, row in signals]
    probabilistic = score(probabilities_selected, outcomes)
    cost_evidence = _cost_evidence(signals)
    return {
        "independent_signals": count,
        "eligible_markets": len(contracts),
        "wait_markets": max(0, len(contracts) - count),
        "actionable_market_coverage": count / len(contracts) if contracts else 0,
        "actionable_directional_accuracy": correct / count if count else None,
        "up_precision": sum(row["outcome"] == 1 for _, row in up) / len(up) if up else None,
        "up_signals": len(up),
        "down_precision": sum(row["outcome"] == 0 for _, row in down) / len(down) if down else None,
        "down_signals": len(down),
        "accuracy_interval_95": wilson(correct, count) if count else None,
        "brier": probabilistic.get("brier"),
        "log_loss": probabilistic.get("log_loss"),
        "calibration": probabilistic.get("calibration", []),
        "after_cost_evidence": cost_evidence,
        "actual_realized_results": None,
        "actual_realized_results_reason": "Model holdout outcomes are not manual account fills",
    }


def _cost_evidence(signals, slippage=0.01):
    """Quote-time counterfactual; unavailable inputs never become zero costs."""
    from .edge_scanner import quantity_fee

    observations = []
    for probability_up, direction, row in signals:
        quotes = row.get("executable_quotes") or {}
        side = "up" if direction else "down"
        ask = (quotes.get(side) or {}).get("ask")
        multiplier = row.get("fee_multiplier")
        if (
            type(ask) not in (int, float)
            or not math.isfinite(ask)
            or not 0 < ask < 1
            or type(multiplier) not in (int, float)
            or not math.isfinite(multiplier)
        ):
            continue
        fee = quantity_fee(ask, 1, multiplier)
        cost = ask + slippage + fee
        if cost >= 1:
            continue
        probability = probability_up if direction else 1 - probability_up
        won = direction == row["outcome"]
        observations.append(
            {
                "expected_value": probability - cost,
                "simulated_net_pnl": (1 if won else 0) - cost,
            }
        )
    return {
        "status": "AVAILABLE" if observations else "UNAVAILABLE",
        "quote_time_simulated_fills": len(observations),
        "mean_expected_value_after_costs": (
            sum(item["expected_value"] for item in observations) / len(observations)
            if observations
            else None
        ),
        "simulated_net_pnl": sum(item["simulated_net_pnl"] for item in observations)
        if observations
        else None,
        "slippage_per_contract": slippage if observations else None,
        "note": "Executable quote-time counterfactual, not a real fill; missing price or fee inputs are excluded, never treated as zero.",
    }


def _regime_reports(probabilities, rows, threshold):
    dimensions = {
        "volatility": lambda row: row.get("volatility_regime")
        or (row.get("features") or {}).get("volatility_regime"),
        "trend": lambda row: row.get("trend_regime")
        or (row.get("features") or {}).get("trend_regime"),
        "time_remaining": lambda row: row.get("horizon")
        or (horizon(row["remaining"]) if type(row.get("remaining")) in (int, float) else None),
        "liquidity": lambda row: row.get("liquidity_regime"),
    }
    result = {}
    for name, getter in dimensions.items():
        buckets = {}
        for probability, row in zip(probabilities, rows):
            value = getter(row)
            if value not in (None, ""):
                buckets.setdefault(str(value), ([], []))
                buckets[str(value)][0].append(probability)
                buckets[str(value)][1].append(row)
        if buckets:
            result[name] = {
                value: _metrics(ps, subset, threshold) for value, (ps, subset) in buckets.items()
            }
    return result


def selective_report(validation_probabilities, validation_rows, test_probabilities, test_rows):
    """Freeze an abstention threshold on validation, then score untouched test.

    Meeting the 80% objective on validation only chooses among a small,
    predeclared threshold grid.  It is never itself evidence that the objective
    was achieved; only the separately returned test report can provide that
    evidence, with its sample count and uncertainty interval.
    """
    candidates = []
    for threshold in THRESHOLDS:
        metrics = _metrics(validation_probabilities, validation_rows, threshold)
        accuracy = metrics["actionable_directional_accuracy"]
        candidates.append({"threshold": threshold, **metrics})
    eligible = [
        item
        for item in candidates
        if item["independent_signals"] >= MIN_VALIDATION_SIGNALS
        and item["actionable_directional_accuracy"] is not None
    ]
    meeting_target = [
        item for item in eligible if item["actionable_directional_accuracy"] >= TARGET_ACCURACY
    ]
    pool = meeting_target or eligible
    if not pool:
        return {
            "protocol": PROTOCOL,
            "status": "INSUFFICIENT_VALIDATION_SIGNALS",
            "target_accuracy": TARGET_ACCURACY,
            "validation_candidates": candidates,
            "test": None,
            "promotion_allowed": False,
        }
    # Coverage is optimized only after satisfying the validation objective. If
    # none meets it, retain the most accurate diagnostic threshold. Test data
    # never participates in this choice.
    if meeting_target:
        chosen = max(
            pool, key=lambda item: (item["actionable_market_coverage"], item["independent_signals"])
        )
        selection_reason = "validation target met; highest validation coverage"
    else:
        chosen = max(
            pool,
            key=lambda item: (item["actionable_directional_accuracy"], item["independent_signals"]),
        )
        selection_reason = "validation target not met; diagnostic best validation accuracy"
    threshold = chosen["threshold"]
    test = _metrics(test_probabilities, test_rows, threshold)
    test["by_regime"] = _regime_reports(test_probabilities, test_rows, threshold)
    test["target_observed"] = bool(
        test["independent_signals"] >= MIN_VALIDATION_SIGNALS
        and test["actionable_directional_accuracy"] is not None
        and test["actionable_directional_accuracy"] >= TARGET_ACCURACY
    )
    coverage_scenarios = {}
    for target in COVERAGE_TARGETS:
        # The threshold is chosen using validation coverage only.  Test
        # outcomes never select a coverage scenario or alter its threshold.
        validation_choice = min(
            candidates,
            key=lambda item: (
                abs(item["actionable_market_coverage"] - target),
                -item["independent_signals"],
            ),
        )
        coverage_scenarios[f"{int(target * 100)}%"] = {
            "target_coverage": target,
            "selected_threshold": validation_choice["threshold"],
            "validation": validation_choice,
            "test": _metrics(
                test_probabilities, test_rows, validation_choice["threshold"]
            ),
            "selected_before_test": True,
        }
    return {
        "protocol": PROTOCOL,
        "status": "UNTOUCHED_TEST_REPORTED",
        "target_accuracy": TARGET_ACCURACY,
        "selected_threshold": threshold,
        "selected_before_test": True,
        "selection_reason": selection_reason,
        "validation": chosen,
        "validation_candidates": candidates,
        "test": test,
        "coverage_scenarios": coverage_scenarios,
        "promotion_allowed": False,
        "note": "An accuracy objective is not a profitability claim. Coverage, calibration, costs and independent forward sample uncertainty remain required.",
    }
