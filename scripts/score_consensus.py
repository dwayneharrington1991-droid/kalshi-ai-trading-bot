"""Prospective scorecard: LIVE AI vs CHALLENGER AI vs EXPERIMENTAL CONSENSUS (research only).

Sample = markets whose three calls were frozen and stored by the alert service (sent.sqlite3 `openings`,
immutable) from START onwards, joined to official settlements (journal forecast_outcomes). Nothing here is
used to train, tune or select any model; v2-v5 frozen tests are separate and untouched.

Profitability is an ESTIMATE at an assumed 50c entry: Kalshi taker fee ceil-free approximation
0.07*p*(1-p) = 1.75c plus 1c slippage, so break-even accuracy = 52.75%. Real entry prices differ.

  python scripts/score_consensus.py --alerts sent.sqlite3 --journal upgraded-live-8768.db
"""
import argparse
import json
import math
import sqlite3

START = "2026-11-22T19:45:00+00:00"           # first market scored by the permanent three-AI service
PRICE, SLIPPAGE = 0.50, 0.01
FEE = 0.07 * PRICE * (1 - PRICE)
BREAK_EVEN = PRICE + FEE + SLIPPAGE


def wilson(k, n, z=1.96):
    if not n:
        return [None, None]
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [c - h, c + h]


def direction(p, t=0.55):
    return "UP" if p >= t else "DOWN" if p <= 1 - t else "WAIT"


def card(pairs):
    """pairs: (direction, p_up, outcome 0/1)."""
    n = len(pairs)
    calls = [(d, y) for d, _p, y in pairs if d in ("UP", "DOWN")]
    k = sum((d == "UP") == (y == 1) for d, y in calls)
    brier = sum((p - y) ** 2 for _d, p, y in pairs) / n if n else None
    acc = k / len(calls) if calls else None
    return {"markets": n, "calls": len(calls), "correct": k, "accuracy": acc, "accuracy_ci95": wilson(k, len(calls)),
            "coverage": len(calls) / n if n else None, "wait": n - len(calls), "brier_of_p_up": brier,
            "est_profit_per_contract_usd": None if acc is None else acc * 1.0 - BREAK_EVEN}


def score(rows):
    """rows: (ticker, live_up, challenger_up, combined_up, combined_direction, outcome)."""
    rows = [r for r in rows if r[5] in (0, 1) and None not in r[1:4]]
    return {"start": START, "settled_markets": len(rows),
            "assumptions": {"entry_price": PRICE, "fee": round(FEE, 4), "slippage": SLIPPAGE,
                            "break_even_accuracy": round(BREAK_EVEN, 4)},
            "live": card([(direction(r[1]), r[1], r[5]) for r in rows]),
            "challenger": card([(direction(r[2]), r[2], r[5]) for r in rows]),
            "consensus": card([(r[4], r[3], r[5]) for r in rows]),
            "note": "EXPERIMENTAL, NOT VALIDATED. Small samples are not evidence; no claim of improved accuracy "
                    "without a pre-registered test."}


def load(alerts, journal):
    a = sqlite3.connect(f"file:{alerts}?mode=ro", uri=True)
    j = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    out = {r[0]: r[3] for r in j.execute("SELECT * FROM forecast_outcomes")}
    return [(t, lu, cu, ku, kd, out.get(t)) for t, lu, cu, ku, kd in a.execute(
        "SELECT ticker, live_up, challenger_up, combined_up, combined FROM openings WHERE opens_at >= ? AND kind='READY'",
        (START,))]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--alerts", required=True)
    ap.add_argument("--journal", required=True)
    a = ap.parse_args(argv)
    print(json.dumps(score(load(a.alerts, a.journal)), indent=1))


if __name__ == "__main__":
    main()
