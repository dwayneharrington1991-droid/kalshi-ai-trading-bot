#!/usr/bin/env python3
"""Read-only production AI readiness audit; never trains, promotes, or writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def read_json(path):
    try:
        if path.is_file() and path.stat().st_size <= 20_000_000:
            return json.loads(path.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def latest(paths):
    values = [path for path in paths if path.is_file()]
    return max(values, key=lambda path: path.stat().st_mtime, default=None)


def metric_summary(report):
    candidate = ((report or {}).get("test") or {}).get("selected_candidate") or {}
    return {
        key: candidate.get(key)
        for key in (
            "samples", "actionable_samples", "accuracy", "coverage", "brier",
            "log_loss", "expected_calibration_error", "net_expected_value_after_costs",
        )
        if key in candidate
    }


def model_row(name, report, labeled, artifact_loaded, artifact_status):
    report = report or {}
    forward = report.get("unseen_forward_contracts")
    required = report.get("required_unseen_forward_contracts", 30)
    additional = report.get("additional_unseen_contracts_needed")
    if additional is None and isinstance(forward, int):
        additional = max(0, required - forward)
    promotion = report.get("promotion_allowed") is True
    status = "READY FOR ACTIVATION" if promotion else "SHADOW"
    reasons = []
    if not promotion:
        reasons.append(str(report.get("status") or "no qualifying evaluation report"))
    if isinstance(additional, int) and additional:
        reasons.append(f"needs {additional} additional untouched contracts")
    if not artifact_loaded:
        reasons.append("artifact " + artifact_status)
    return {
        "model": name,
        "labeled_markets": labeled,
        "forward_validation": forward,
        "required_forward": required,
        "additional_needed": additional,
        "performance": metric_summary(report),
        "artifact_status": ("LOADED: " if artifact_loaded else "NOT_LOADED: ")
        + artifact_status,
        "status": status,
        "what_is_missing": "; ".join(reasons) or "reviewed explicit activation",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--deploy-root", type=Path, required=True)
    parser.add_argument("--model-registry", type=Path, required=True)
    parser.add_argument("--settlement-artifact", type=Path, required=True)
    parser.add_argument("--opening-artifact", type=Path, required=True)
    args = parser.parse_args()

    uri = args.journal.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=5000")
    try:
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        recorded = db.execute(
            "SELECT COUNT(*) FROM btc_engine_contracts WHERE mode='PUBLIC MARKET DATA'"
        ).fetchone()[0] if "btc_engine_contracts" in tables else None
        settled = db.execute(
            "SELECT COUNT(DISTINCT ticker) FROM forecast_outcomes WHERE origin='official'"
        ).fetchone()[0] if "forecast_outcomes" in tables else None
        from src.signals_display.opening_models import dataset_coverage

        opening_coverage = dataset_coverage(db)
    finally:
        db.close()

    evaluation_root = args.deploy_root / "evaluations"
    opening_report_path = latest(evaluation_root.glob("*/opening.report.json"))
    settlement_report_path = latest(evaluation_root.glob("*/settlement.report.json"))
    opening_report = read_json(opening_report_path) if opening_report_path else None
    settlement_report = read_json(settlement_report_path) if settlement_report_path else None

    from src.signals_display.learning import load_artifact as load_settlement
    from src.signals_display.opening_models import load_artifact as load_opening

    settlement_artifact, settlement_artifact_status = load_settlement(
        args.settlement_artifact
    )
    opening_artifact, opening_artifact_status = load_opening(args.opening_artifact)

    scalp_path = args.model_registry / "latest-scalp-evaluation.json"
    scalp = read_json(scalp_path) or {}
    scalp_test = scalp.get("test") or {}
    scalp_status = "SHADOW"
    scalp_row = {
        "model": "SCALP",
        "labeled_markets": scalp.get("contracts"),
        "forward_validation": (scalp.get("split_contracts") or [None, None, None])[-1],
        "required_forward": None,
        "additional_needed": None,
        "performance": {
            key: scalp_test.get(key)
            for key in (
                "observed_complete_trades", "directional_accuracy", "coverage",
                "net_simulated_pnl", "mean_net_pnl_approximate_interval",
            )
            if key in scalp_test
        },
        "artifact_status": "SHADOW_REPORT_PRESENT" if scalp else "UNAVAILABLE",
        "status": scalp_status,
        "what_is_missing": str(
            scalp.get("status") or "no reviewed scalp promotion protocol/report"
        ),
    }

    opening_labeled = opening_coverage.get("eligible_frozen_openings")
    settlement_labeled = (settlement_report or {}).get("available_eligible_contracts")
    rows = [
        model_row(
            "OPENING", opening_report, opening_labeled,
            opening_artifact is not None, opening_artifact_status,
        ),
        model_row(
            "SETTLEMENT", settlement_report, settlement_labeled,
            settlement_artifact is not None, settlement_artifact_status,
        ),
        scalp_row,
    ]

    log_path = latest((args.deploy_root / "evaluation-logs").glob("*.log"))
    log_tail = ""
    if log_path and log_path.stat().st_size <= 20_000_000:
        log_tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-120:])
    evaluation_status = (
        "COMPLETE" if "ISOLATED_EVALUATION_STATUS=COMPLETE" in log_tail
        else "PARTIAL_OR_FAILED"
        if "ISOLATED_EVALUATION_STATUS=PARTIAL_OR_FAILED" in log_tail
        else "RUNNING_OR_UNVERIFIED"
    )

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "automated_trading": "DISABLED",
        "journal": str(args.journal),
        "distinct_recorded_markets": recorded,
        "officially_settled_markets": settled,
        "opening_dataset_coverage": opening_coverage,
        "latest_opening_report": str(opening_report_path) if opening_report_path else None,
        "latest_settlement_report": str(settlement_report_path)
        if settlement_report_path else None,
        "latest_scalp_report": str(scalp_path) if scalp_path.is_file() else None,
        "latest_evaluation_log": str(log_path) if log_path else None,
        "latest_isolated_evaluation_status": evaluation_status,
        "models": rows,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    print()
    print("MODEL | LABELED MARKETS | FORWARD VALIDATION | PERFORMANCE | STATUS | WHAT'S MISSING")
    for row in rows:
        forward = (
            f"{row['forward_validation']}/{row['required_forward']}"
            if row["required_forward"] is not None else str(row["forward_validation"])
        )
        print(
            f"{row['model']} | {row['labeled_markets']} | {forward} | "
            f"{json.dumps(row['performance'], sort_keys=True)} | {row['status']} | "
            f"{row['what_is_missing']}"
        )


if __name__ == "__main__":
    main()
