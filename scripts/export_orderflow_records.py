"""Export point-in-time decision records for the order-flow forward test OF1 (research only, read-only).

One JSONL record per settled market and target, in the format of scripts/research_order_flow_value.py:
  opening_final  decision = the locked opening call (alert-service openings.generated_at); label = official outcome
  first6         decision = first6.locked_at; label = BTC reference open -> +6:00 (FLAT skipped)
  minute6_final  decision = minute6.locked_at; features only up to min(lock, opens + 367 s); label = official
Baseline = that system's own recorded probability (AI #1 live P(UP), FIRST 6 MIN p_up, MINUTE-6 consensus average).
Feature families come only from collector rows (orderbook_research.sqlite3, one per venue per second) whose
timestamp is at or before the decision: "price" (Kraken mid returns), "book" (imbalances, depth, walls) and
"trades" (executed buy/sell sums and CVD change over 60 s / 300 s), each requiring both venues LIVE in that second.
Markets without a complete, fresh snapshot are written to a separate exclusions file, never silently dropped.

  python3 scripts/export_orderflow_records.py --out-dir ~/dual-ai-alerts/of1
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home() / "dual-ai-alerts"
START = datetime(2026, 11, 27, 3, 15, tzinfo=timezone.utc)     # OF1 registration: only decisions from here on
MAX_ROW_AGE_S = 2.0                                             # newest collector row must be <= 2 s before the decision
VENUES = ("coinbase", "kraken")


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class Flow:
    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def window(self, venue, end_ts, seconds):
        return self.db.execute("SELECT ts, status, mid, imb_010, imb_025, bid_025, ask_025, buy_1s, sell_1s, cvd, "
                               "top_bid_wall, top_bid_wall_px, top_ask_wall, top_ask_wall_px FROM book_seconds "
                               "WHERE venue=? AND ts>? AND ts<=? ORDER BY ts", (venue, end_ts - seconds, end_ts)).fetchall()

    def families(self, decision):
        d = decision.timestamp()
        latest, rows = {}, {}
        for v in VENUES:
            rows[v] = self.window(v, d, 310)          # 300 s lookback needs a row at or before d - 300
            if not rows[v] or rows[v][-1][1] != "LIVE" or d - rows[v][-1][0] > MAX_ROW_AGE_S:
                return None, f"{v} book not LIVE/fresh at the decision"
            latest[v] = rows[v][-1]
        received = max(r[0] for r in latest.values())
        k = rows["kraken"]
        mid_now = k[-1][2]
        def mid_back(sec):
            older = [r for r in k if r[0] <= d - sec]
            return older[-1][2] if older else None
        price = {}
        for sec in (60, 300):
            m = mid_back(sec)
            if m:
                price[f"return_{sec}s"] = mid_now / m - 1
        if len(price) < 2:
            return None, "not enough Kraken mid history before the decision"
        book, trades = {}, {}
        for v in VENUES:
            r = latest[v]
            book[f"{v}_imb_010"], book[f"{v}_imb_025"] = r[3] or 0.0, r[4] or 0.0
            depth = (r[5] or 0) + (r[6] or 0)
            book[f"{v}_depth_025_btc"] = depth
            book[f"{v}_bid_wall_btc"], book[f"{v}_ask_wall_btc"] = r[10] or 0.0, r[12] or 0.0
            book[f"{v}_bid_wall_dist"] = (r[11] / r[2] - 1) if r[11] and r[2] else 0.0
            book[f"{v}_ask_wall_dist"] = (r[13] / r[2] - 1) if r[13] and r[2] else 0.0
            for sec in (60, 300):
                w = [x for x in rows[v] if x[0] > d - sec]
                buy, sell = sum(x[7] or 0 for x in w), sum(x[8] or 0 for x in w)
                trades[f"{v}_buy_{sec}s"], trades[f"{v}_sell_{sec}s"] = buy, sell
                trades[f"{v}_imb_{sec}s"] = (buy - sell) / (buy + sell) if buy + sell else 0.0
                trades[f"{v}_cvd_change_{sec}s"] = (w[-1][9] or 0) - (w[0][9] or 0) if w else 0.0
        book["agree_imb_025"] = 1.0 if (book["coinbase_imb_025"] > 0) == (book["kraken_imb_025"] > 0) else 0.0
        trades["agree_imb_60s"] = 1.0 if (trades["coinbase_imb_60s"] > 0) == (trades["kraken_imb_60s"] > 0) else 0.0
        stamp = iso(received)
        return {"price": {"received_at": stamp, "event_at": stamp, "values": price},
                "book": {"received_at": stamp, "event_at": stamp, "values": book},
                "trades": {"received_at": stamp, "event_at": stamp, "values": trades}}, None


def export(alerts, flow_path, out_dir):
    a = sqlite3.connect(f"file:{alerts}?mode=ro", uri=True)
    fl = Flow(flow_path)
    official = {t: (o, s) for t, o, s in a.execute("SELECT ticker, outcome, seen_at FROM outcomes7")}
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    specs = {
        "opening_final": a.execute("SELECT ticker, opens_at, generated_at, live_up FROM openings WHERE kind='READY'").fetchall(),
        "first6": a.execute("SELECT f.ticker, f.opens_at, f.locked_at, f.p_up, o.outcome, o.recorded_at FROM first6 f "
                            "JOIN first6_outcomes o ON o.ticker=f.ticker WHERE o.outcome IN ('UP','DOWN')").fetchall(),
        "minute6_final": a.execute("SELECT ticker, opens_at, locked_at, payload FROM minute6").fetchall(),
    }
    for target, rows in specs.items():
        recs, excl = [], []
        for row in rows:
            ticker, opens = row[0], _ts(row[1])
            if target == "first6":
                decision, base, label, label_at = _ts(row[2]), row[3], row[4], _ts(row[5])
            elif target == "opening_final":
                if ticker not in official:
                    continue
                decision, base = _ts(row[2]), row[3]
                label, label_at = official[ticker][0], _ts(official[ticker][1])
            else:
                if ticker not in official:
                    continue
                decision = _ts(row[2])                                     # the actual lock time
                feature_cut = min(decision, opens + timedelta(seconds=367))  # learned-input cutoff
                base = (json.loads(row[3] or "{}") or {}).get("consensus_p_up_average")
                label, label_at = official[ticker][0], _ts(official[ticker][1])
            if decision < START or not isinstance(base, (int, float)):
                continue
            fams, why = fl.families(feature_cut if target == "minute6_final" else decision)
            if fams is None:
                excl.append({"sample_id": ticker, "target": target, "decision_at": decision.isoformat(), "reason": why})
                continue
            recs.append({"sample_id": ticker, "target": target, "decision_at": decision.isoformat(),
                         "label_observed_at": label_at.isoformat(), "label": label,
                         "baseline_probability": float(base), "baseline_generated_at": decision.isoformat(),
                         "families": fams})
        (out_dir / f"{target}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        (out_dir / f"{target}.exclusions.json").write_text(json.dumps(excl, indent=1))
        counts[target] = {"records": len(recs), "excluded": len(excl)}
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--alerts", default=str(HOME / "sent.sqlite3"))
    ap.add_argument("--flow", default=str(HOME / "orderbook_research.sqlite3"))
    ap.add_argument("--out-dir", default=str(HOME / "of1"))
    a = ap.parse_args(argv)
    print(json.dumps(export(a.alerts, a.flow, a.out_dir)))


if __name__ == "__main__":
    main()
