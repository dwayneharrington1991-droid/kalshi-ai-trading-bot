"""Score pre-registered hypothesis H-session (docs/research-20261122-path-to-80.md). Research only.

Live immutable opening calls (P >= 0.55 UP, <= 0.45 DOWN) with opens_at >= START; window = opens at
03:00-12:59 UTC. Supported only if >= MIN_CALLS window calls, Wilson 95% lower bound > 0.60 and a
two-proportion z-test against other hours gives p < 0.05.

  python scripts/eval_h_session.py --journal journal_dump.json
"""
import argparse
import json
import math
from datetime import datetime, timezone

START = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
MIN_CALLS = 120
WINDOW = range(3, 13)


def wilson(k, n):
    if not n:
        return [None, None]
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [c - h, c + h]


def two_proportion_p(k1, n1, k2, n2):
    if not n1 or not n2:
        return None
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (k1 / n1 - k2 / n2) / se
    return 0.5 * math.erfc(z / math.sqrt(2))          # one-sided: window better than others


def score(rows):
    """rows: (opens_at datetime, p_up, outcome 0/1)."""
    cells = {"window": [0, 0], "other": [0, 0]}
    for opens, p, y in rows:
        if opens < START or not (p >= 0.55 or p <= 0.45):
            continue
        cell = cells["window" if opens.hour in WINDOW else "other"]
        cell[1] += 1
        cell[0] += int((p >= 0.55) == (y == 1))
    (kw, nw), (ko, no) = cells["window"], cells["other"]
    lo = wilson(kw, nw)[0]
    pval = two_proportion_p(kw, nw, ko, no)
    decided = nw >= MIN_CALLS
    supported = decided and lo is not None and lo > 0.60 and pval is not None and pval < 0.05
    return {"start": START.isoformat(), "window_calls": nw, "window_correct": kw, "window_ci95": wilson(kw, nw),
            "other_calls": no, "other_correct": ko, "other_ci95": wilson(ko, no), "p_value_one_sided": pval,
            "minimum_window_calls": MIN_CALLS,
            "decision": "NOT ENOUGH CALLS" if not decided else "SUPPORTED" if supported else "NOT SUPPORTED"}


def load(journal):
    d = json.load(open(journal))
    out = {r[0]: r[3] for r in d["forecast_outcomes"]["rows"]}
    rows, seen = [], set()
    for _m, ticker, _g, _c, _cl, up, _s, payload in d["btc_engine_opening"]["rows"]:
        if ticker in seen or up is None or out.get(ticker) not in (0, 1):
            continue
        seen.add(ticker)
        rows.append((datetime.fromisoformat(json.loads(payload)["opens_at"].replace("Z", "+00:00")), float(up), int(out[ticker])))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", required=True)
    print(json.dumps(score(load(ap.parse_args(argv).journal)), indent=1))


if __name__ == "__main__":
    main()
