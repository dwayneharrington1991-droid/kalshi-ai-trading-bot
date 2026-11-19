"""Run the pre-registered candidate study (PAPER ONLY).

Stage A  every grid configuration on TRAIN and VALIDATION.
Stage B  per family: pick the best VALIDATION config among those with net > 0
         on train AND validation and >= 30 validation trades. Families with no
         such config are REJECTED and never see TEST.
Stage C  qualifying families: ONE ledger-guarded TEST evaluation each.
Stage D  EXPLORATORY checks on the first fresh recording (depth / AI families
         that cannot be tested historically, plus every family's selected
         config). Too small for conclusions; it never changes a selection.

    python -m src.micro_scalp.research.run --historical data/micro_scalp/historical \
        --btc data/micro_scalp/btc/btc_1s.json --fresh data/micro_scalp/reports/fresh_snapshot_full.jsonl \
        --out data/micro_scalp/research/study.json --ledger data/micro_scalp/research/holdout_ledger.jsonl
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import numpy as np

from .. import install_guard
from ..events import iter_historical, recorded_events
from ..metrics import _activity, trade_stats
from .candidates import Candidate, ResearchEngine, btc_events, merge
from .protocol import MIN_VALIDATION_TRADES, HoldoutLedger, credible, splits

MEMORY_LIMIT_BYTES = 6 * 1024**3
_BARS = {}


def _limit_resources():
    import resource

    os.nice(10)
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


def _bars(path):
    if path not in _BARS:
        _BARS[path] = json.loads(Path(path).read_text())["bars"] if path else []
    return _BARS[path]


def grid(vol_cuts):
    g = {
        "mean_reversion": [Candidate("mean_reversion", k=v) for v in (0.25, 0.5, 1.0)],
        "momentum_k": [Candidate("momentum_k", k=v) for v in (0.25, 0.5, 1.0)],
        "flow": [Candidate("flow", k=v) for v in (0.02, 0.05, 0.10)],
        "btc_fair_gap": [Candidate("btc_fair_gap", k=v) for v in (0.25, 0.5, 1.0)],
        "fair_value_hold": [Candidate("fair_value_hold", margin=v) for v in (0.03, 0.06, 0.10)],
    }
    if vol_cuts:
        g["vol_regime_mr"] = [
            Candidate("vol_regime_mr", k=0.5, vol_regime="low", vol_cut=vol_cuts[0]),
            Candidate("vol_regime_mr", k=0.5, vol_regime="high", vol_cut=vol_cuts[1]),
        ]
    return g


def summarize_engine(e):
    s = trade_stats(e.trades)
    s["wins"] = sum(t["net_pnl"] > 0 for t in e.trades)
    s["losses"] = s["completed_trades"] - s["wins"]
    s["max_drawdown_equity"] = round(e.max_drawdown, 2)
    s["activity"] = _activity(e)
    s["forecast_out_of_sample"] = e.model.skill()
    s["exit_reasons"] = {}
    for t in e.trades:
        key = "settlement" if t["settled"] else ("target" if "target" in t["exit_reason"] else "stop" if "stop" in t["exit_reason"] else "other")
        s["exit_reasons"][key] = s["exit_reasons"].get(key, 0) + 1
    rows = [(p, mid, e.outcomes.get(tk)) for tk, _, p, mid in e.fair_log if e.outcomes.get(tk)]
    if rows:
        y = np.array([1.0 if o == "up" else 0.0 for *_, o in rows])
        pf, pm = np.array([r[0] for r in rows]), np.array([r[1] for r in rows])
        s["calibration"] = {"samples": len(rows), "brier_btc_fair": round(float(((pf - y) ** 2).mean()), 4),
                            "brier_market_mid": round(float(((pm - y) ** 2).mean()), 4)}
    return s


def _job(args):
    kind, cand, payload, hist_dir, btc_path = args
    install_guard()
    if kind == "historical":
        stream = merge(iter_historical(hist_dir, payload), btc_events(_bars(btc_path)))
    else:
        events, _ = recorded_events(payload)
        lo, hi = (events[0]["at"], events[-1]["at"]) if events else (0, 0)
        stream = merge(iter(events), btc_events([b for b in _bars(btc_path) if lo - 3700 <= b[0] <= hi]))
    e = ResearchEngine(cand)
    e.run(stream)
    return summarize_engine(e)


def _key(c):
    return json.dumps(asdict(c), sort_keys=True)


def main():
    install_guard()
    p = argparse.ArgumentParser()
    p.add_argument("--historical", type=Path, required=True)
    p.add_argument("--btc", type=Path, required=True)
    p.add_argument("--fresh", type=Path, default=None, help="EXPLORATORY fresh recording (already seen in run 2)")
    p.add_argument("--investigation", type=Path, default=None, help="investigation.json from TRAIN (vol terciles)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--workers", type=int, default=3)
    a = p.parse_args()
    workers = max(1, min(3, a.workers))
    sp = splits(a.historical)
    vol_cuts = json.loads(a.investigation.read_text()).get("train_vol_terciles") if a.investigation else None
    g = grid(vol_cuts)
    report = {"paper_trading_only": True, "generated_at": datetime.now(timezone.utc).isoformat(),
              "protocol": "see src/micro_scalp/research/protocol.py", "split_sizes": {k: len(v) for k, v in sp.items()},
              "split_ranges": {k: [v[0], v[-1]] for k, v in sp.items()}, "families": {}}
    jobs, labels = [], []
    for fam, cands in g.items():
        for c in cands:
            for split in ("train", "validation"):
                jobs.append(("historical", c, sp[split], a.historical, str(a.btc)))
                labels.append((fam, c, split))
    with ProcessPoolExecutor(max_workers=workers, initializer=_limit_resources) as pool:
        results = list(pool.map(_job, jobs))
    table = {}
    for (fam, c, split), res in zip(labels, results):
        table.setdefault(fam, {}).setdefault(_key(c), {"config": asdict(c)})[split] = res

    ledger = HoldoutLedger(a.ledger)
    test_jobs = []
    for fam, configs in table.items():
        rows = list(configs.values())
        eligible = [r for r in rows if r["train"]["net_pnl"] > 0 and r["validation"]["net_pnl"] > 0
                    and r["validation"]["completed_trades"] >= MIN_VALIDATION_TRADES]
        best_val = max(rows, key=lambda r: r["validation"]["net_pnl"])
        entry = {"grid": rows, "best_validation_config": best_val["config"]}
        if not eligible:
            entry["status"] = "REJECTED_BEFORE_TEST"
            entry["reason"] = (f"no configuration had net > 0 on both train and validation with >= {MIN_VALIDATION_TRADES} validation trades")
        else:
            chosen = max(eligible, key=lambda r: r["validation"]["net_pnl"])
            entry["selected_config"] = chosen["config"]
            cand = Candidate(**chosen["config"])
            cached = ledger.authorize(fam, cand)
            if cached:
                entry["test"] = cached["result"]
                entry["test_source"] = "ledger (identical config evaluated earlier; not re-run)"
            else:
                test_jobs.append((fam, cand))
        report["families"][fam] = entry
    if test_jobs:
        with ProcessPoolExecutor(max_workers=workers, initializer=_limit_resources) as pool:
            outs = list(pool.map(_job, [("historical", c, sp["test"], a.historical, str(a.btc)) for _, c in test_jobs]))
        for (fam, cand), res in zip(test_jobs, outs):
            ledger.record(fam, cand, res)
            report["families"][fam]["test"] = res
    for fam, entry in report["families"].items():
        if "test" in entry:
            ok, why = credible(entry["test"])
            entry["status"] = "CREDIBLE_EDGE_PENDING_FINAL_FRESH" if ok else "REJECTED_ON_TEST"
            entry["reason"] = why

    if a.fresh and a.fresh.exists():
        explore = [Candidate(**report["families"][f].get("selected_config", report["families"][f]["best_validation_config"])) for f in report["families"]]
        explore += [Candidate("book_imbalance", k=v) for v in (0.02, 0.05)] + [Candidate("ai_gap", k=v) for v in (0.25, 0.5)]
        with ProcessPoolExecutor(max_workers=workers, initializer=_limit_resources) as pool:
            outs = list(pool.map(_job, [("fresh", c, str(a.fresh), None, str(a.btc)) for c in explore]))
        report["fresh_exploratory"] = {
            "warning": "EXPLORATORY ONLY: few contracts, already inspected in run 2; never used for selection",
            "results": [{"config": asdict(c), **res} for c, res in zip(explore, outs)]}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=1, default=str))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
