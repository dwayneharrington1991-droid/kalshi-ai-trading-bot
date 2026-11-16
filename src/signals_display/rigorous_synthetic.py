"""Synthetic opening rows for testing evaluation protocols. SYNTHETIC ONLY.

Nothing produced here is market data or evidence of live accuracy. The
generator lets tests measure false-approval rates (labels drawn from the
calibrated baseline itself, so no candidate can genuinely beat it) and power
(labels carry a known extra signal on one compact feature).
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import random

from . import learning, opening_models, rigorous_evaluation
from .features import FEATURES

EXTRA_NOISE_FEATURES = tuple(
    name for name in FEATURES if name not in rigorous_evaluation.COMPACT_FEATURES
)[:20]


def opening_rows(n, *, seed, signal=0.0, start=None, noise_features=EXTRA_NOISE_FEATURES):
    """``signal`` is the true log-odds coefficient on standardized return_5m."""
    rng = random.Random(seed)
    start = start or datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=(seed % 300))
    rows = []
    for i in range(n):
        r5 = rng.gauss(0, 1)
        features = {
            "return_1m": 0.0004 * (0.6 * r5 + 0.8 * rng.gauss(0, 1)),
            "return_5m": 0.0009 * r5,
            "return_15m": 0.0015 * (0.5 * r5 + rng.gauss(0, 1)),
            "return_60m": 0.003 * rng.gauss(0, 1),
            "return_24h": 0.02 * rng.gauss(0, 1),
            "acceleration": 0.0003 * rng.gauss(0, 1),
            "body_fraction": max(-1.0, min(1.0, rng.gauss(0, 0.4))),
            "breakout_distance": 0.001 * rng.gauss(0, 1),
            "volatility_change": rng.lognormvariate(0, 0.4),
            "relative_volume": rng.lognormvariate(0, 0.5),
        }
        if rng.random() < 0.7:
            features["spot_depth_imbalance"] = max(-1.0, min(1.0, rng.gauss(0, 0.3)))
        if rng.random() < 0.8:
            features["derivatives_basis"] = 0.0003 * rng.gauss(0, 1)
        for name in noise_features:
            features[name] = rng.gauss(0, 1)
        # Calibrated opening baseline with a realistic ~4% median tilt.
        baseline_logit = rng.gauss(0, 0.16)
        baseline = learning.sigmoid(baseline_logit)
        outcome = int(rng.random() < learning.sigmoid(baseline_logit + signal * r5))
        opened = start + timedelta(minutes=15 * i)
        closed = opened + timedelta(minutes=15)
        rows.append(dict(
            ticker=f"KXBTC15M-SYN{seed:04d}-{i:05d}",
            timestamp=opened.isoformat(), opens_at=opened.isoformat(),
            closes_at=closed.isoformat(),
            label_available_at=(closed + timedelta(seconds=60)).isoformat(),
            features=features, outcome=outcome, baseline=baseline, market=None,
            remaining=900.0, opening_delay_seconds=0.0, regime="synthetic",
        ))
    return rows


def existing_gate_passed(report):
    return report.get("status") in {
        "SHADOW_CANDIDATE_PASSED_STATISTICAL_GATES",
        "SELECTIVE_WAIT_POLICY_UNAVAILABLE",
    }


def trial(n, seed, signal, *, include_existing):
    rows = opening_rows(n, seed=seed, signal=signal)
    rigorous = rigorous_evaluation.evaluate(rows, used_test_contracts=[], attempt=1, include_existing=False)
    result = {
        "seed": seed,
        "rigorous_status": rigorous["status"],
        "rigorous_passed": rigorous["status"] == "SHADOW_PASSED_RIGOROUS_GATES",
        "rigorous_selected": rigorous.get("selected_before_test"),
        "rigorous_test_log_loss": (rigorous.get("test") or {}).get(rigorous.get("selected_before_test") or "", {}).get("log_loss"),
        "baseline_test_log_loss": (rigorous.get("test") or {}).get(rigorous_evaluation.BASELINE, {}).get("log_loss"),
    }
    if include_existing:
        _artifact, existing = opening_models.evaluate(rows, used_test_contracts=[])
        result["existing_status"] = existing["status"]
        result["existing_passed"] = existing_gate_passed(existing)
        selected = existing.get("test", {}).get("selected_candidate", {})
        result["existing_test_log_loss"] = selected.get("log_loss")
    return result


def band_study(n, seeds, signal):
    """Pool untouched-test predictions across trials, then tally confidence bands."""
    pooled = {}
    for seed in seeds:
        rows = opening_rows(n, seed=seed, signal=signal)
        report = rigorous_evaluation.evaluate(rows, used_test_contracts=[], attempt=1)
        if not report.get("test_exposed"):
            continue
        test = [r for r in rows if r["ticker"] in set(report["test_contracts"])]
        selected = report["selected_before_test"]
        sources = {"opening_statistical_baseline": [r["baseline"] for r in test]}
        if selected != rigorous_evaluation.BASELINE:
            sources["rigorous_selected"] = [
                rigorous_evaluation.predict(report["selected_model"], r) for r in test
            ]
        else:
            sources["rigorous_selected"] = sources["opening_statistical_baseline"]
        for name, probabilities in sources.items():
            ps, rs = pooled.setdefault(name, ([], []))
            ps.extend(probabilities)
            rs.extend(test)
        comparison = report.get("existing_pipeline_comparison")
        if comparison:
            # The unchanged existing selection's bands, as scored inside evaluate().
            existing = pooled.setdefault("existing_pipeline_bands_per_trial", [])
            existing.append(comparison["confidence_bands"])
    result = {
        name: rigorous_evaluation.confidence_band_report(ps, rs)
        for name, value in pooled.items()
        if name != "existing_pipeline_bands_per_trial"
        for ps, rs in [value]
    }
    per_trial = pooled.get("existing_pipeline_bands_per_trial", [])
    if per_trial:
        totals = {}
        for bands in per_trial:
            for label, band in bands["bands"].items():
                item = totals.setdefault(label, {"contracts": 0, "correct": 0})
                item["contracts"] += band["contracts"]
                item["correct"] += band["correct"]
        for item in totals.values():
            item["observed_accuracy"] = item["correct"] / item["contracts"] if item["contracts"] else None
        result["existing_pipeline_pooled_bands"] = totals
    return {"data": "SYNTHETIC — not market evidence", "contracts_per_trial": n,
            "trials": len(seeds), "signal": signal, "bands": result}


def main():
    parser = argparse.ArgumentParser(description="SYNTHETIC false-approval / power study")
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--contracts", type=int, default=170)
    parser.add_argument("--signal", type=float, default=0.0)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--bands", action="store_true", help="Pooled confidence-band study")
    args = parser.parse_args()
    if args.bands:
        seeds = [args.seed_offset + i for i in range(args.trials)]
        print(json.dumps(band_study(args.contracts, seeds, args.signal), indent=1))
        return
    results = [
        trial(args.contracts, args.seed_offset + i, args.signal, include_existing=not args.skip_existing)
        for i in range(args.trials)
    ]
    summary = {
        "data": "SYNTHETIC — not market evidence",
        "trials": args.trials,
        "contracts": args.contracts,
        "signal": args.signal,
        "rigorous_approvals": sum(r["rigorous_passed"] for r in results),
        "existing_approvals": None if args.skip_existing else sum(r["existing_passed"] for r in results),
    }
    print(json.dumps({"summary": summary, "trials": results}, indent=1))


if __name__ == "__main__":
    main()
