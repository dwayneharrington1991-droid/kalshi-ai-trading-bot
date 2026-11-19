"""Performance statistics with honest uncertainty.

Trades inside one 15-minute contract share the same price path, so the
bootstrap resamples whole contracts (cluster bootstrap), not single trades.
The no-trade baseline is exactly $0 P&L with $0 fees and zero drawdown.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import random


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def cluster_bootstrap(trades, reps=4000, seed=7):
    by = defaultdict(list)
    for t in trades:
        by[t["ticker"]].append(t["net_pnl"])
    clusters = [sum(v) for v in by.values()]
    counts = [len(v) for v in by.values()]
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    totals, means = [], []
    k = len(clusters)
    for _ in range(reps):
        idx = [rng.randrange(k) for _ in range(k)]
        tot = sum(clusters[i] for i in idx)
        n = sum(counts[i] for i in idx)
        totals.append(tot)
        means.append(tot / n if n else 0.0)
    totals.sort()
    means.sort()

    def q(a, p):
        return round(a[min(len(a) - 1, int(p * len(a)))], 4)

    return {
        "method": "cluster bootstrap over contracts (trades within a contract are dependent)",
        "contracts": k,
        "net_pnl_95ci": [q(totals, 0.025), q(totals, 0.975)],
        "mean_net_per_trade_95ci": [q(means, 0.025), q(means, 0.975)],
        "probability_net_pnl_not_above_zero": round(sum(1 for v in totals if v <= 0) / len(totals), 4),
    }


def _bucket(values, trades, key, labels=("low", "mid", "high")):
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 3:
        return {}
    cuts = [vals[len(vals) // 3], vals[2 * len(vals) // 3]]
    groups = defaultdict(list)
    for t in trades:
        v = t.get(key)
        if v is None:
            continue
        groups[labels[0] if v < cuts[0] else labels[1] if v < cuts[1] else labels[2]].append(t)
    return {k: _mini(g) for k, g in groups.items()}


def _mini(trades):
    pnl = [t["net_pnl"] for t in trades]
    wins = sum(p > 0 for p in pnl)
    return {"trades": len(pnl), "net_pnl": round(sum(pnl), 2), "win_rate": round(wins / len(pnl), 3) if pnl else None,
            "fees": round(sum(t["fees"] for t in trades), 2)}


def summarize(engine, label=None):
    trades = engine.trades
    orders = engine.exchange.done + engine.exchange.pending
    pnl = [t["net_pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    status = Counter(o.status for o in orders)
    start = engine.rc.starting_balance
    end_equity = engine.equity(engine.next_tick or 0)
    out = {
        "label": label or engine.name,
        "paper_trading_only": True,
        "starting_balance": start,
        "ending_equity": round(end_equity, 2),
        "net_pnl": round(sum(pnl), 2),
        "return_pct": round(100 * (end_equity - start) / start, 3),
        "completed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
        "win_rate_95ci": wilson(len(wins), len(pnl)),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if not wins else float("inf")),
        "gross_pnl_before_fees": round(sum(t["gross_pnl"] for t in trades), 2),
        "total_fees": round(sum(t["fees"] for t in trades), 2),
        "average_net_per_trade": round(sum(pnl) / len(pnl), 4) if pnl else None,
        "largest_loss": round(min(pnl), 2) if pnl else None,
        "largest_win": round(max(pnl), 2) if pnl else None,
        "max_drawdown": round(engine.max_drawdown, 2),
        "max_drawdown_pct": round(100 * engine.max_drawdown_pct, 3),
        "open_positions_at_end": len(engine.positions),
        "orders": {
            "submitted": len(orders),
            "filled": status.get("FILLED", 0),
            "partial": status.get("PARTIAL", 0),
            "unfilled": status.get("UNFILLED", 0) + status.get("CANCELLED_MARKET_CLOSED", 0),
            "fill_rate": round((status.get("FILLED", 0) + status.get("PARTIAL", 0)) / len(orders), 4) if orders else None,
            "note": "Unfilled orders are never counted as trades and carry no P&L.",
        },
        "exit_reasons": Counter(_reason(t["exit_reason"]) for t in trades),
        "settled_trades": sum(t["settled"] for t in trades),
        "decisions": dict(engine.decision_counts),
        "top_wait_reasons": engine.wait_reasons.most_common(8),
        "risk_events": [(round(t), e, d) for t, e, d in engine.risk.events],
        "model_out_of_sample": engine.model.skill(),
        "activity": _activity(engine),
        "vs_no_trade_baseline": {
            "no_trade_net_pnl": 0.0,
            "difference": round(sum(pnl), 2),
            "uncertainty": cluster_bootstrap(trades),
        },
        "by_condition": {
            "volatility_60s": _bucket([t.get("entry_realized_vol_60s") for t in trades], trades, "entry_realized_vol_60s"),
            "time_left": _bucket([t.get("entry_time_left") for t in trades], trades, "entry_time_left", ("late", "middle", "early")),
            "entry_price": _bucket([t.get("entry_price") for t in trades], trades, "entry_price", ("cheap", "mid", "expensive")),
            "side": {s: _mini([t for t in trades if t["side"] == s]) for s in ("up", "down") if any(t["side"] == s for t in trades)},
        },
    }
    return out


def trade_stats(trades):
    """Compact statistics for a subset of trades (e.g. development vs holdout)."""
    pnl = [t["net_pnl"] for t in trades]
    wins = [p for p in pnl if p > 0]
    loss = -sum(p for p in pnl if p <= 0)
    curve = peak = dd = 0.0
    for t in sorted(trades, key=lambda t: t["exit_time"]):
        curve += t["net_pnl"]
        peak = max(peak, curve)
        dd = max(dd, peak - curve)
    return {
        "completed_trades": len(pnl),
        "net_pnl": round(sum(pnl), 2),
        "total_fees": round(sum(t["fees"] for t in trades), 2),
        "gross_pnl_before_fees": round(sum(t["gross_pnl"] for t in trades), 2),
        "win_rate": round(len(wins) / len(pnl), 4) if pnl else None,
        "win_rate_95ci": wilson(len(wins), len(pnl)),
        "profit_factor": round(sum(wins) / loss, 3) if loss > 0 else None,
        "closed_trade_max_drawdown": round(dd, 2),
        "vs_no_trade": cluster_bootstrap(trades),
    }


def _activity(engine):
    hours = engine.active_seconds / 3600
    buys = sum(v for k, v in engine.decision_counts.items() if k.startswith("BUY"))
    per = lambda v: round(v / hours, 2) if hours else None
    return {
        "active_market_hours": round(hours, 2),
        "edge_candidates_per_hour": per(engine.edge_candidates),
        "buy_signals_per_hour": per(buys),
        "completed_trades_per_hour": per(len(engine.trades)),
        "edge_candidates": engine.edge_candidates,
        "gate_blocked_candidates": engine.gate_blocked_candidates,
        "gate_violations": engine.gate_violations,
        "definitions": "edge candidate = forecast clears +1c/contract after spread, slippage and fees (before skill gate, spread, price and risk filters); buy signal = BUY issued after all filters",
    }


def _reason(text):
    text = text or ""
    for key in ("profit target", "stop-loss", "time limit", "about to close", "model now expects", "risk limit", "official settlement"):
        if key in text:
            return key
    return "other"


def ai_signal_evaluation(engine):
    """Brier score of the EXISTING live AI probability vs official outcomes,
    compared with the market's own mid price as a forecast. Samples every 30s."""
    rows = [(tk, p, mid, engine.outcomes.get(tk)) for tk, _, p, mid in engine.ai_samples if engine.outcomes.get(tk)]
    if not rows:
        return {"status": "NOT_MEASURABLE", "reason": "No existing-AI probabilities with official outcomes in this dataset."}
    y = [1.0 if o == "up" else 0.0 for *_, o in rows]
    ai = [min(1, max(0, p)) for _, p, _, _ in rows]
    mk = [m for _, _, m, _ in rows]
    brier = lambda f: sum((a - b) ** 2 for a, b in zip(f, y)) / len(y)
    contracts = len({r[0] for r in rows})
    return {
        "status": "MEASURED" if contracts >= 30 else "INSUFFICIENT_SAMPLE",
        "samples": len(rows),
        "contracts": contracts,
        "brier_existing_ai_live": round(brier(ai), 4),
        "brier_market_mid": round(brier(mk), 4),
        "note": "Lower is better. Samples within a contract share one outcome, so the effective sample size is the number of contracts. Confidence is not inferred from this unless contracts >= 30.",
    }
