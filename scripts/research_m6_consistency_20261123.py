import json, math, statistics as st, sys
from collections import defaultdict
sys.path.insert(0, "../repo")
from src.signals_display import minute6 as m6
rows = sorted(json.load(open("m6_dev.json")), key=lambda r: r["opens_at"])
def call(r):
    a = m6.component_a(r["closes"], r["threshold"]); b = m6.component_b(r["closes"])
    c = m6.component_c(r["engine"]["up"] if r["engine"] else None)
    return m6.consensus(a, b, c)["direction"], a["sigma_1m"]
by_day, by_vol = defaultdict(lambda: [0, 0, 0]), defaultdict(lambda: [0, 0, 0])
sig = []
for r in rows:
    d, s = call(r); sig.append(s); r["_d"], r["_s"] = d, s
q1, q2 = sorted(sig)[len(sig)//3], sorted(sig)[2*len(sig)//3]
for r in rows:
    for key, tab in ((r["opens_at"][:10], by_day), ("low vol" if r["_s"] < q1 else "mid vol" if r["_s"] < q2 else "high vol", by_vol)):
        t = tab[key]; t[2] += 1
        if r["_d"] in ("UP", "DOWN"):
            t[0] += 1; t[1] += (r["_d"] == "UP") == (r["outcome"] == 1)
print("MINUTE-6 FINAL frozen rule, by day (calls, right, accuracy, coverage):")
for k, (n, c, tot) in sorted(by_day.items()): print(f"  {k}: {n:3d} {c:3d} {c/n if n else 0:.3f} cov {n/tot:.2f}")
print("by volatility tercile:")
for k, (n, c, tot) in by_vol.items(): print(f"  {k}: {n:3d} {c:3d} {c/n if n else 0:.3f} cov {n/tot:.2f}")
