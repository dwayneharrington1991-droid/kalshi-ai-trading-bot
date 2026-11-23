"""Read-only accuracy-stability report (research only). Bounded queries; never writes the journal.

AI #1: every locked live opening call since the opening model went live (journal btc_engine_opening +
official forecast_outcomes). COMBINED CALL / AI #2 / MINUTE-6 FINAL / FIRST 6 MIN: the alert service's immutable
records (~/dual-ai-alerts/sent.sqlite3) with official outcomes (outcomes7) or the six-minute reference move.
Reports rolling 25/50/100/300-call accuracy, daily, trading-session and volatility-regime splits, UP/DOWN bias,
Brier, log loss, Wilson intervals and a first-half vs second-half two-proportion test.

  python3 scripts/stability_report.py [--journal J] [--alerts A]
"""
import argparse
import json
import math
import sqlite3
from collections import defaultdict

J = "/home/opc/kalshi-preview-upgrade/logs/upgraded-live-8768.db"
A = "/home/opc/dual-ai-alerts/sent.sqlite3"


def wilson(k, n, z=1.96):
    if not n:
        return None
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


def two_prop_p(k1, n1, k2, n2):
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    z = (k1 / n1 - k2 / n2) / se
    return round(math.erfc(abs(z) / math.sqrt(2)), 4)                    # two-sided


def session(hour):
    return "Asia 00-08 UTC" if hour < 8 else "Europe 08-13 UTC" if hour < 13 else "US 13-21 UTC" if hour < 21 else "Late 21-24 UTC"


def summarize(rows):
    """rows: dicts with opens (iso), dir (UP/DOWN/WAIT/None), p (P(UP) or None), y (1/0)."""
    calls = [r for r in rows if r["dir"] in ("UP", "DOWN")]
    k = sum((r["dir"] == "UP") == (r["y"] == 1) for r in calls)
    probs = [r for r in rows if isinstance(r.get("p"), (int, float))]
    out = {"settled": len(rows), "calls": len(calls), "right": k, "wrong": len(calls) - k,
           "wait_or_missing": len(rows) - len(calls),
           "accuracy": round(k / len(calls), 3) if calls else None, "accuracy_ci95": wilson(k, len(calls)),
           "coverage": round(len(calls) / len(rows), 3) if rows else None,
           "up_call_share": round(sum(r["dir"] == "UP" for r in calls) / len(calls), 3) if calls else None,
           "up_outcome_rate": round(sum(r["y"] for r in rows) / len(rows), 3) if rows else None}
    if probs:
        out["brier"] = round(sum((r["p"] - r["y"]) ** 2 for r in probs) / len(probs), 4)
        out["log_loss"] = round(-sum(math.log(max(1e-6, r["p"] if r["y"] else 1 - r["p"])) for r in probs) / len(probs), 4)
    rolling = {}
    for w in (25, 50, 100, 300):
        if len(calls) >= w:
            last = calls[-w:]
            kk = sum((r["dir"] == "UP") == (r["y"] == 1) for r in last)
            rolling[f"last_{w}_calls"] = {"accuracy": round(kk / w, 3), "ci95": wilson(kk, w)}
    out["rolling"] = rolling
    groups = {"by_day": lambda r: r["opens"][:10], "by_session": lambda r: session(int(r["opens"][11:13]))}
    for name, key in groups.items():
        g = defaultdict(list)
        for r in rows:
            g[key(r)].append(r)
        out[name] = {}
        for gk, gr in sorted(g.items()):
            gc = [r for r in gr if r["dir"] in ("UP", "DOWN")]
            gk_ = sum((r["dir"] == "UP") == (r["y"] == 1) for r in gc)
            out[name][gk] = {"settled": len(gr), "calls": len(gc), "accuracy": round(gk_ / len(gc), 3) if gc else None,
                             "ci95": wilson(gk_, len(gc))}
    if len(calls) >= 20:
        h = len(calls) // 2
        a, b = calls[:h], calls[h:]
        ka = sum((r["dir"] == "UP") == (r["y"] == 1) for r in a)
        kb = sum((r["dir"] == "UP") == (r["y"] == 1) for r in b)
        out["first_vs_second_half"] = {"first": [ka, len(a)], "second": [kb, len(b)], "p_value": two_prop_p(ka, len(a), kb, len(b))}
    return out


def ai1_rows(journal):
    c = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    c.execute("PRAGMA query_only=ON")
    out = {r[0]: r[3] for r in c.execute("SELECT * FROM forecast_outcomes WHERE origin='official'")}
    rows, seen = [], set()
    for t, up, payload in c.execute("SELECT ticker, up, payload FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA' ORDER BY rowid"):
        if t in seen or up is None or out.get(t) not in (0, 1):
            continue
        p = json.loads(payload)
        if not str(p.get("model", "")).startswith("opening-candidate"):
            continue
        seen.add(t)
        d = "UP" if up >= 0.55 else "DOWN" if up <= 0.45 else "WAIT"
        rows.append({"opens": p.get("opens_at"), "dir": d, "p": float(up), "y": int(out[t])})
    return sorted(rows, key=lambda r: r["opens"])


def service_rows(alerts):
    c = sqlite3.connect(f"file:{alerts}?mode=ro", uri=True)
    out = {t: (1 if o == "UP" else 0) for t, o in c.execute("SELECT ticker, outcome FROM outcomes7")}
    res = {"COMBINED CALL": [], "AI #2": [], "MINUTE-6 FINAL": [], "FIRST 6 MIN": []}
    for t, opens, ch, chp, comb, combp in c.execute(
            "SELECT ticker, opens_at, challenger, challenger_up, combined, combined_up FROM openings WHERE kind='READY'"):
        if t in out:
            res["AI #2"].append({"opens": opens, "dir": ch, "p": chp, "y": out[t]})
            res["COMBINED CALL"].append({"opens": opens, "dir": comb, "p": combp, "y": out[t]})
    for t, opens, d, payload in c.execute("SELECT ticker, opens_at, direction, payload FROM minute6"):
        if t in out:
            ok = (json.loads(payload or "{}").get("eligible") or 0) >= 2
            res["MINUTE-6 FINAL"].append({"opens": opens, "dir": d if ok else None, "p": None, "y": out[t]})
    try:
        for t, opens, d, p, o in c.execute("SELECT f.ticker, f.opens_at, f.direction, f.p_up, o.outcome FROM first6 f "
                                           "JOIN first6_outcomes o ON o.ticker=f.ticker WHERE o.outcome IN ('UP','DOWN')"):
            res["FIRST 6 MIN"].append({"opens": opens, "dir": d, "p": p, "y": 1 if o == "UP" else 0})
    except sqlite3.OperationalError:
        pass
    return {k: sorted(v, key=lambda r: r["opens"]) for k, v in res.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=J)
    ap.add_argument("--alerts", default=A)
    a = ap.parse_args(argv)
    report = {"AI #1 (all locked live calls)": summarize(ai1_rows(a.journal))}
    for k, v in service_rows(a.alerts).items():
        report[f"{k} (prospective, alert-service records)"] = summarize(v)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
