"""Scheduled chronological scalp research; never activates signals or trades.

The candidate grid is fixed before validation. Selection uses past development
contracts, while the final chronological block is evaluated exactly once. All
fills remain counterfactual executable-book simulations from scalp_paper.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .model import timestamp
from .model_registry import atomic_json
from .scalp_paper import PaperCriteria, evaluate, load_journal


PROTOCOL = "chronological-scalp-shadow-v1"
MIN_CONTRACTS = 90
MIN_OBSERVED_TRADES = 30
GRID = tuple(
    PaperCriteria(minimum_target_probability=probability, maximum_spread=spread)
    for probability in (0.60, 0.65, 0.70)
    for spread in (0.02, 0.03)
)


def _contract(row):
    market = row.get("market") if isinstance(row, dict) else None
    return market.get("ticker") if isinstance(market, dict) else row.get("ticker")


def _available(row):
    if row.get("type") == "official_settlement":
        return timestamp(row["available_at"])
    return timestamp(row.get("available_at", row.get("recorded_at")))


def _subset(records, contracts):
    return [row for row in records if _contract(row) in contracts]


def _metrics(report):
    return (report.get("strategies") or {}).get("candidate") or {}


def _supported(metrics):
    interval = metrics.get("mean_net_pnl_approximate_interval")
    return bool(
        metrics.get("observed_complete_trades", 0) >= MIN_OBSERVED_TRADES
        and metrics.get("net_simulated_pnl") is not None
        and metrics["net_simulated_pnl"] > 0
        and isinstance(interval, list)
        and len(interval) == 2
        and interval[0] is not None
        and interval[0] > 0
    )


def evaluate_chronological(records):
    ordered = sorted(records, key=_available)
    first = {}
    for row in ordered:
        ticker = _contract(row)
        if ticker and row.get("type") != "official_settlement":
            first.setdefault(ticker, _available(row))
    contracts = sorted(first, key=first.get)
    base = {
        "protocol": PROTOCOL,
        "status": "INSUFFICIENT_FORWARD_SCALP_HISTORY",
        "contracts": len(contracts),
        "required_contracts": MIN_CONTRACTS,
        "minimum_observed_trades_per_gate": MIN_OBSERVED_TRADES,
        "actionable": False,
        "active_model_changed": False,
        "edge_status": "edge unverified",
        "method": "Chronological contract-level 60/20/20 split. A fixed criteria grid is selected on validation, then evaluated once on untouched later contracts with executable asks/bids, fees, slippage, depth and manual delay.",
        "safety": "Shadow research only. It cannot modify the live criteria, model artifact, account, risk limits, or actionable-signal gate.",
    }
    if len(contracts) < MIN_CONTRACTS:
        return base
    a, b = int(len(contracts) * 0.6), int(len(contracts) * 0.8)
    development, validation, test = map(set, (contracts[:a], contracts[a:b], contracts[b:]))
    candidates = []
    for criteria in GRID:
        # Development defines the predeclared search window; model event
        # probabilities were already generated prospectively. Criteria selection
        # is scored only on the later validation contracts.
        report = evaluate(_subset(ordered, validation), criteria)
        candidates.append(
            {
                "criteria": asdict(criteria),
                "metrics": _metrics(report),
            }
        )
    eligible = [item for item in candidates if _supported(item["metrics"])]
    chosen = max(
        eligible,
        key=lambda item: (
            item["metrics"]["net_simulated_pnl"],
            -item["metrics"]["maximum_drawdown"],
        ),
        default=None,
    )
    result = {
        **base,
        "status": "NO_VALIDATION_CANDIDATE" if chosen is None else "SHADOW_CANDIDATE_TESTED",
        "split_contracts": [len(development), len(validation), len(test)],
        "validation_candidates": candidates,
        "selected_criteria": chosen["criteria"] if chosen else None,
        "selection_frozen_before_test": True,
        "test_first_available_at": min(first[ticker] for ticker in test).isoformat(),
        "development_last_available_at": max(first[ticker] for ticker in development).isoformat(),
        "validation_last_available_at": max(first[ticker] for ticker in validation).isoformat(),
    }
    if chosen is None:
        return result
    criteria = PaperCriteria.parse(chosen["criteria"])
    test_report = evaluate(_subset(ordered, test), criteria)
    test_metrics = _metrics(test_report)
    result.update(
        test=test_metrics,
        costs_included=True,
        liquidity_and_partial_fills_included=True,
        manual_delay_included=True,
        chronological_out_of_sample=True,
        validation_passed=_supported(chosen["metrics"]),
        untouched_test_passed=_supported(test_metrics),
        status="EXPERIMENTAL_POSITIVE_OOS_RESULT"
        if _supported(test_metrics)
        else "SCALP_BASELINE_RETAINED",
        note="A positive shadow result is not automatic permission to issue a trade signal. Independent prospective review and explicit promotion remain required.",
    )
    return result


def scheduled_evaluation(journal, output, now=None, minimum_hours=24):
    now = now or datetime.now(timezone.utc)
    output = Path(output)
    if output.is_file() and output.stat().st_size <= 5_000_000:
        try:
            previous = json.loads(output.read_text())
            generated = timestamp(previous["generated_at"])
            if now < generated + timedelta(hours=minimum_hours):
                return {
                    "status": "SCALP_EVALUATION_NOT_DUE",
                    "last_completed_at": generated.isoformat(),
                    "active_model_changed": False,
                    "actionable": False,
                }
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            pass
    source = load_journal(journal, limit=10000, mode="PUBLIC MARKET DATA")
    report = evaluate_chronological(source["records"])
    report["generated_at"] = now.isoformat()
    report["journal_coverage"] = source["coverage"]
    atomic_json(output, report)
    return report
