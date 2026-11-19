"""Run the full paper evaluation and write a JSON report.

1. Historical (settled contracts before the fresh window), one chronological
   pass per variant, reported separately for the earlier half (development)
   and the later half (holdout). Settings are fixed in code before any run;
   nothing is tuned on the holdout or on fresh data.
2. Fresh forward data recorded live (never used during development), with
   the model warm-started from the historical pass.
3. Variants: no_trade baseline, market_only, with_existing_ai (fresh only:
   historical AI states were never recorded), plus an explicitly labelled
   DIAGNOSTIC variant without the out-of-sample skill gate, used only to
   reach a 100+ trade sample of the execution/cost model.

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
from .events import load_historical, recorded_events
from .metrics import ai_signal_evaluation, summarize, trade_stats
from .risk import RiskConfig


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat() if t else None


def run_historical(events, info, strategy, risk, name):
    engine = Engine(strategy, risk, name=name).run(events)
    markets = sorted({e["ticker"]: e["opens"] for e in events if e["kind"] == "market"}.values())
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
    a = p.parse_args()

    risk = RiskConfig(starting_balance=a.balance)
    base = StrategyConfig()
    diagnostic = replace(base, require_oos_skill=False)
    report = {
        "paper_trading_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "risk_config": risk.to_dict(),
        "strategy_config": asdict(base),
        "profitability_claim": "NONE. Simulated results under stated assumptions; not evidence of a live edge.",
    }

    events, info = load_historical(a.historical)
    report["historical_data"] = info
    hist = {}
    for name, cfg in (("market_only", base), ("DIAGNOSTIC_no_skill_gate", diagnostic)):
        engine, rep = run_historical(events, info, cfg, risk, name)
        hist[name] = (engine, rep)
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
        warm = hist["market_only"][0].model
        warm_diag = hist["DIAGNOSTIC_no_skill_gate"][0].model
        fresh = {}
        for name, cfg, w in (
            ("market_only", base, warm),
            ("with_existing_ai", replace(base, use_ai=True), warm),
            ("DIAGNOSTIC_no_skill_gate", diagnostic, warm_diag),
            ("DIAGNOSTIC_no_skill_gate_with_ai", replace(diagnostic, use_ai=True), warm_diag),
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
