"""Render a backtest JSON report as Markdown tables (no editorialising).

    python -m src.micro_scalp.report_md data/micro_scalp/reports/report.json > docs/MICRO_SCALP_RESULTS_TABLES.md
"""
from __future__ import annotations

import json
import sys

ROWS = (
    ("Completed trades", "completed_trades", "{}"),
    ("Net P&L (after fees)", "net_pnl", "${:,.2f}"),
    ("Gross P&L before fees", "gross_pnl_before_fees", "${:,.2f}"),
    ("Total fees", "total_fees", "${:,.2f}"),
    ("Win rate", "win_rate", "{:.1%}"),
    ("Profit factor", "profit_factor", "{:.2f}"),
    ("Max drawdown", "max_drawdown", "${:,.2f}"),
    ("Ending equity", "ending_equity", "${:,.2f}"),
)


def fmt(value, pattern):
    if value is None:
        return "–"
    try:
        return pattern.format(value)
    except (ValueError, TypeError):
        return str(value)


def strategy_table(section: dict, names: list[str]) -> str:
    present = [n for n in names if n in section and "completed_trades" in section[n]]
    if not present:
        return "_No runs._\n"
    out = ["| Metric | " + " | ".join(present) + " | no-trade baseline |", "|---|" + "---|" * (len(present) + 1)]
    for label, key, pattern in ROWS:
        base = {"net_pnl": "$0.00", "total_fees": "$0.00", "max_drawdown": "$0.00", "completed_trades": "0",
                "gross_pnl_before_fees": "$0.00"}.get(key, "–")
        out.append(f"| {label} | " + " | ".join(fmt(section[n].get(key), pattern) for n in present) + f" | {base} |")
    out.append("| Win rate 95% CI | " + " | ".join(_ci(section[n].get("win_rate_95ci"), pct=True) for n in present) + " | – |")
    out.append("| Net P&L 95% CI (cluster bootstrap) | " + " | ".join(_ci(((section[n].get("vs_no_trade_baseline") or {}).get("uncertainty") or {}).get("net_pnl_95ci")) for n in present) + " | $0.00 |")
    out.append("| P(net ≤ 0) (bootstrap) | " + " | ".join(fmt(((section[n].get("vs_no_trade_baseline") or {}).get("uncertainty") or {}).get("probability_net_pnl_not_above_zero"), "{:.2f}") for n in present) + " | – |")
    out.append("| Orders submitted / filled / partial / unfilled | " + " | ".join(_orders(section[n].get("orders")) for n in present) + " | 0 |")
    out.append("| Model out-of-sample R² vs no-change | " + " | ".join(fmt((section[n].get("model_out_of_sample") or {}).get("r2_vs_zero"), "{:.3f}") for n in present) + " | – |")
    out.append("| Direction hit rate (out of sample) | " + " | ".join(fmt((section[n].get("model_out_of_sample") or {}).get("direction_hit_rate"), "{:.1%}") for n in present) + " | – |")
    return "\n".join(out) + "\n"


def _ci(ci, pct=False):
    if not ci:
        return "–"
    return f"{ci[0]:.1%} – {ci[1]:.1%}" if pct else f"${ci[0]:,.2f} – ${ci[1]:,.2f}"


def _orders(o):
    if not o:
        return "–"
    return f"{o['submitted']} / {o['filled']} / {o['partial']} / {o['unfilled']}"


def segments(section):
    out = []
    for name, rep in section.items():
        seg = rep.get("segments") if isinstance(rep, dict) else None
        if not seg:
            continue
        out.append(f"**{name}** (split at {seg['split_at']})\n")
        out.append("| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |\n|---|---|---|---|---|---|---|")
        for label, key in (("Development (first half)", "development_first_half"), ("Holdout (second half)", "holdout_second_half")):
            s = seg[key]
            out.append(f"| {label} | {s['completed_trades']} | {fmt(s['net_pnl'], '${:,.2f}')} | {fmt(s['total_fees'], '${:,.2f}')} | "
                       f"{fmt(s['win_rate'], '{:.1%}')} | {fmt(s['profit_factor'], '{:.2f}')} | {_ci((s.get('vs_no_trade') or {}).get('net_pnl_95ci'))} |")
        out.append("")
    return "\n".join(out)


def conditions(rep):
    out = ["| Condition | Bucket | Trades | Net P&L | Win rate | Fees |", "|---|---|---|---|---|---|"]
    for cond, buckets in (rep.get("by_condition") or {}).items():
        for bucket, s in buckets.items():
            out.append(f"| {cond} | {bucket} | {s['trades']} | {fmt(s['net_pnl'], '${:,.2f}')} | {fmt(s['win_rate'], '{:.1%}')} | {fmt(s['fees'], '${:,.2f}')} |")
    return "\n".join(out) + "\n"


def render(report: dict) -> str:
    parts = ["## Historical (settled contracts, before the fresh window)\n",
             "Data: " + json.dumps(report.get("historical_data")) + "\n",
             strategy_table(report["historical"], ["market_only", "DIAGNOSTIC_cost_blind"]),
             "\n### Development vs holdout\n", segments(report["historical"])]
    diag = report["historical"].get("DIAGNOSTIC_cost_blind")
    if diag:
        parts += ["\n### Diagnostic trades by market condition (historical)\n", conditions(diag),
                  "\nExit reasons: " + json.dumps(diag.get("exit_reasons")) + "\n",
                  "\nRisk events: " + json.dumps(diag.get("risk_events")) + "\n"]
    if "fresh" in report:
        parts += ["\n## Fresh, unseen live recording\n", "Data: " + json.dumps(report.get("fresh_data")) + "\n",
                  strategy_table(report["fresh"], ["market_only", "with_existing_ai", "DIAGNOSTIC_cost_blind", "DIAGNOSTIC_cost_blind_with_ai"])]
        ai = report["fresh"].get("with_existing_ai", {}).get("ai_signal_evaluation")
        if ai:
            parts.append("\n### Existing AI live probability vs outcomes (fresh)\n\n" + "\n".join(f"- {k}: {v}" for k, v in ai.items()) + "\n")
    return "\n".join(parts)


if __name__ == "__main__":
    print(render(json.loads(open(sys.argv[1]).read())))
