"""Run the full paper evaluation and write a JSON report.

1. Historical (settled contracts before the fresh window), one chronological
   pass per variant, reported separately for the earlier half (development)
   and the later half (holdout). Settings are fixed in code before any run;
   nothing is tuned on the holdout or on fresh data.
2. Fresh forward data recorded live (never used during development), with
   the model warm-started from the historical pass.
3. Variants: no_trade baseline, market_only, with_existing_ai (fresh only:
   historical AI states were never recorded), plus an explicitly labelled
   cost-blind DIAGNOSTIC variant (no skill gate, no profit-after-fees
   hurdle), used only to reach a 100+ trade sample of execution costs and
   directional accuracy.

    python -m src.micro_scalp.backtest --historical data/micro_scalp/historical \
        --fresh data/micro_scalp/fresh_live.jsonl --out data/micro_scalp/reports/report.json
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path

from . import install_guard
from .engine import Engine, StrategyConfig
from .events import historical_index, iter_historical, recorded_events
from .metrics import ai_signal_evaluation, summarize, trade_stats
from .risk import RiskConfig


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None


def run_historical(directory, strategy, risk, name):
    """One chronological streaming pass (bounded memory)."""
    engine = Engine(strategy, risk, name=name).run(iter_historical(directory))
    markets = [r["opens"] for r in historical_index(directory)]
    split = markets[len(markets) // 2] if markets else 0
    dev = [t for t in engine.trades if t["entry_time"] < split]
    hold = [t for t in engine.trades if t["entry_time"] >= split]
    report = summarize(engine, name)
    report["segments"] = {
        "split_at": _iso(split),
        "development_first_half": trade_stats(dev),
        "holdout_second_half": trade_stats(hold),
    }
    return engine, report


def _historical_job(args):
    directory, strategy, risk, name = args
    engine, report = run_historical(directory, strategy, risk, name)
    return name, report, engine.model  # the model warm-starts the fresh run


def run_fresh(events, strategy, risk, name, warm_model=None):
    engine = Engine(strategy, risk, name=name)
    if warm_model is not None:
        engine.model.warm_start_from(warm_model)
    engine.run(events)
    report = summarize(engine, name)
    report["ai_signal_evaluation"] = ai_signal_evaluation(engine)
    return engine, report


def main():
    install_guard()
    p = argparse.ArgumentParser(description="Micro-scalp PAPER evaluation (no live orders)")
    p.add_argument("--historical", type=Path, required=True)
    p.add_argument("--fresh", type=Path, default=None)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--workers", type=int, default=2, help="Parallel processes (CPU safeguard; max 3)")
    a = p.parse_args()
    a.workers = max(1, min(3, a.workers))

    risk = RiskConfig(starting_balance=a.balance)
    base = StrategyConfig()
    # DIAGNOSTIC: deliberately cost-blind. Acts on the model's directional view
    # (|predicted 30s move| >= 1c) without the skill gate or the profit-after-
    # fees hurdle, to build a 100+ trade sample of real execution costs and
    # directional accuracy. It is a measurement, not a candidate strategy.
    diagnostic = replace(
        base, require_oos_skill=False, min_expected_net_edge=-1.0, min_abs_predicted_move=0.01, cooldown_seconds=60.0
    )
    report = {
        "paper_trading_only": True,
        "diagnostic_config": asdict(diagnostic),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "risk_config": risk.to_dict(),
        "strategy_config": asdict(base),
        "profitability_claim": "NONE. Simulated results under stated assumptions; not evidence of a live edge.",
    }

    index = historical_index(a.historical)
    report["historical_data"] = {
        "source": "historical",
        "markets": len(index),
        "first": _iso(index[0]["opens"]) if index else None,
        "last": _iso(index[-1]["closes"]) if index else None,
        "depth_available": False,
        "loader": "streaming (bounded memory); identical event order to the list loader",
    }
    momentum = replace(base, policy="momentum")
    momentum_blind = replace(diagnostic, policy="momentum")
    ai_direct = replace(base, policy="ai_direct")
    ai_direct_blind = replace(diagnostic, policy="ai_direct")
    jobs = [
        (a.historical, cfg, risk, name)
        for name, cfg in (
            ("market_only", base),
            ("DIAGNOSTIC_cost_blind", diagnostic),
            ("BASELINE_momentum", momentum),
            ("BASELINE_momentum_cost_blind", momentum_blind),
        )
    ]
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        hist = {name: (model, rep) for name, rep, model in pool.map(_historical_job, jobs)}
    report["historical"] = {name: rep for name, (_, rep) in hist.items()}
    report["historical"]["no_trade_baseline"] = {"net_pnl": 0.0, "total_fees": 0.0, "max_drawdown": 0.0}
    report["historical"]["with_existing_ai"] = {
        "status": "NOT_MEASURABLE",
        "reason": "The existing AI's past states were not recorded for these contracts; reconstructing them would require inventing sub-second BTC reference receipts.",
    }

    if a.fresh and a.fresh.exists():
        fev, finfo = recorded_events(a.fresh)
        report["fresh_data"] = {
            **finfo,
            "contracts": len({e["ticker"] for e in fev if e["kind"] == "market"}),
            "settled_contracts": len({e["ticker"] for e in fev if e["kind"] == "settlement"}),
            "first": _iso(fev[0]["at"]) if fev else None,
            "last": _iso(fev[-1]["at"]) if fev else None,
        }
        warm = hist["market_only"][0]
        warm_diag = hist["DIAGNOSTIC_cost_blind"][0]
        fresh = {}
        for name, cfg, w in (
            ("market_only", base, warm),
            ("with_existing_ai", replace(base, use_ai=True), warm),
            ("DIAGNOSTIC_cost_blind", diagnostic, warm_diag),
            ("DIAGNOSTIC_cost_blind_with_ai", replace(diagnostic, use_ai=True), warm_diag),
            ("AI_direct", ai_direct, None),
            ("AI_direct_cost_blind", ai_direct_blind, None),
            ("BASELINE_momentum", momentum, None),
            ("BASELINE_momentum_cost_blind", momentum_blind, None),
        ):
            _, rep = run_fresh(fev, cfg, risk, name, w)
            fresh[name] = rep
        fresh["no_trade_baseline"] = {"net_pnl": 0.0, "total_fees": 0.0, "max_drawdown": 0.0}
        report["fresh"] = fresh

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
