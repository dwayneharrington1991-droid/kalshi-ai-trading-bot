"""Why are 30-second Kalshi moves (un)predictable? TRAIN+VALIDATION only.

Builds a 5-second-sampled dataset of causal features plus FUTURE outcomes
(future values are used only as analysis labels, never as trading inputs):
mid changes at several horizons, executable round-trip P&L, and the
contemporaneous BTC fair-value change.

    python -m src.micro_scalp.research.investigate --historical data/micro_scalp/historical \
        --btc data/micro_scalp/btc/btc_1s.json --out data/micro_scalp/research/investigation.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from .. import install_guard
from ..events import iter_historical
from ..risk import taker_fee
from .candidates import IDX, Candidate, ResearchEngine, btc_events, merge
from .protocol import splits

HORIZONS = (5, 10, 30, 60, 120)
COLS = ("ret_10s", "ret_30s", "ret_60s", "realized_vol_60s", "flow_imbalance_30s", "flow_imbalance_60s", "log_volume_60s", "spread", "time_left_frac", "mid_dev")


class Probe(ResearchEngine):
    def __init__(self):
        super().__init__(Candidate("mean_reversion", k=0.0))
        self.rows = []
        self.mid_series = {}  # ticker -> {sec: (mid, up_bid, up_ask)}

    def tick(self, t):
        m = self._active(t)
        if m is not None and m.quote_age(t) <= self.s.max_quote_age_seconds and m.mid() is not None:
            self.mid_series.setdefault(m.ticker, {})[int(t)] = (m.mid(), m.up_bid, m.up_ask)
        super().tick(t)

    def _policy_prediction(self, m, t, x, info):
        if int(t) % 5 == 0:
            p = self.p_fair(m, t)
            s_now = self.btc_price(t)
            past = {w: next((pr for at, pr in reversed(self.btc) if at <= t - w), None) for w in (10, 30)}
            self.rows.append({
                "ticker": m.ticker, "t": int(t), "mid": info["mid"], "p_fair": p,
                "btc_ret_10s": math.log(s_now / past[10]) if s_now and past[10] else None,
                "btc_ret_30s": math.log(s_now / past[30]) if s_now and past[30] else None,
                **{c: x[IDX[c]] for c in COLS},
            })
        return 0.0


def corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 50 or a[ok].std() == 0 or b[ok].std() == 0:
        return None
    return round(float(np.corrcoef(a[ok], b[ok])[0, 1]), 4)


def analyse(probe):
    rows = probe.rows
    outcomes = probe.outcomes
    for r in rows:
        series = probe.mid_series.get(r["ticker"], {})
        for h in HORIZONS:
            fut = series.get(r["t"] + h)
            r[f"dmid_{h}"] = fut[0] - r["mid"] if fut else None
        now, fut = series.get(r["t"]), series.get(r["t"] + 30)
        if now and fut:
            # Executable 30 s round trip, taker both legs, 1 contract (fees per 100 for rounding realism)
            r["rt_up_30"] = fut[1] - now[2] - (taker_fee(now[2], 100) + taker_fee(fut[1], 100)) / 100
            r["rt_down_30"] = (1 - fut[2]) - (1 - now[1]) - (taker_fee(1 - now[1], 100) + taker_fee(1 - fut[2], 100)) / 100
    by_key = {(r["ticker"], r["t"]): r for r in rows}
    for r in rows:
        nxt = by_key.get((r["ticker"], r["t"] + 30))
        r["dfair_30"] = (nxt["p_fair"] - r["p_fair"]) if nxt and nxt["p_fair"] is not None and r["p_fair"] is not None else None
    col = lambda k: [r.get(k) if r.get(k) is not None else np.nan for r in rows]
    out = {"samples": len(rows), "contracts": len({r["ticker"] for r in rows})}
    out["label_stats_cents"] = {
        f"{h}s": {"std": round(100 * float(np.nanstd(col(f"dmid_{h}"))), 3),
                  "mean_abs": round(100 * float(np.nanmean(np.abs(col(f"dmid_{h}")))), 3),
                  "share_zero": round(float(np.nanmean(np.array(col(f"dmid_{h}")) == 0)), 3)} for h in HORIZONS}
    out["feature_correlation_with_future_mid_change"] = {
        c: {f"{h}s": corr(col(c), col(f"dmid_{h}")) for h in HORIZONS}
        for c in COLS + ("btc_ret_10s", "btc_ret_30s")}
    gap = [r["p_fair"] - r["mid"] if r["p_fair"] is not None else np.nan for r in rows]
    out["fair_gap_correlation_with_future_mid_change"] = {f"{h}s": corr(gap, col(f"dmid_{h}")) for h in HORIZONS}
    out["contemporaneous_btc_fair_change_vs_mid_change_30s"] = {
        "corr": corr(col("dfair_30"), col("dmid_30")),
        "r2": (lambda c: round(c * c, 4) if c is not None else None)(corr(col("dfair_30"), col("dmid_30"))),
        "meaning": "share of 30s Kalshi mid variance explained by the SAME-window change in BTC fair value (not tradeable; shows what drives prices)"}
    rt_up, rt_dn = np.array(col("rt_up_30")), np.array(col("rt_down_30"))
    out["executable_30s_round_trip_cents"] = {
        "mean_up": round(100 * float(np.nanmean(rt_up)), 3), "mean_down": round(100 * float(np.nanmean(rt_dn)), 3),
        "share_profitable_up": round(float(np.nanmean(rt_up > 0)), 4), "share_profitable_down": round(float(np.nanmean(rt_dn > 0)), 4),
        "perfect_foresight_best_side_mean": round(100 * float(np.nanmean(np.fmax(rt_up, rt_dn))), 3),
        "meaning": "random-entry taker round trip after spread+fees; perfect_foresight = choosing the better side with hindsight"}
    # Calibration of settlement forecasts vs official outcomes
    cal = [(r["p_fair"], r["mid"], outcomes.get(r["ticker"])) for r in rows if r["p_fair"] is not None and outcomes.get(r["ticker"]) and r["t"] % 30 == 0]
    if cal:
        y = np.array([1.0 if o == "up" else 0.0 for *_, o in cal])
        pf, pm = np.array([c[0] for c in cal]), np.array([c[1] for c in cal])
        bins = []
        for lo in np.arange(0, 1, 0.1):
            sel = (pm >= lo) & (pm < lo + 0.1)
            if sel.sum() >= 20:
                bins.append({"market_bin": f"{lo:.1f}-{lo+0.1:.1f}", "n": int(sel.sum()), "market_mean": round(float(pm[sel].mean()), 3),
                             "btc_fair_mean": round(float(pf[sel].mean()), 3), "realized_up": round(float(y[sel].mean()), 3)})
        out["settlement_calibration"] = {"samples": len(cal), "contracts": len({r['ticker'] for r in rows if r['p_fair'] is not None}),
                                         "brier_market_mid": round(float(((pm - y) ** 2).mean()), 4),
                                         "brier_btc_fair_value": round(float(((pf - y) ** 2).mean()), 4),
                                         "brier_blend_50_50": round(float((((pm + pf) / 2 - y) ** 2).mean()), 4),
                                         "reliability_by_market_price": bins}
    vols = sorted(r["realized_vol_60s"] for r in rows if r["realized_vol_60s"] is not None)
    out["train_vol_terciles"] = [vols[len(vols) // 3], vols[2 * len(vols) // 3]] if vols else None
    return out


def main():
    install_guard()
    p = argparse.ArgumentParser()
    p.add_argument("--historical", type=Path, required=True)
    p.add_argument("--btc", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--splits", default="train", help="comma list from train,validation (never test)")
    a = p.parse_args()
    chosen = [s for s in a.splits.split(",") if s]
    if "test" in chosen:
        raise SystemExit("The test split is reserved for one-time evaluation; not used for investigation.")
    sp = splits(a.historical)
    tickers = [t for s in chosen for t in sp[s]]
    bars = json.loads(a.btc.read_text())["bars"]
    probe = Probe()
    probe.run(merge(iter_historical(a.historical, tickers), btc_events(bars)))
    res = analyse(probe)
    res["splits_used"] = chosen
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
