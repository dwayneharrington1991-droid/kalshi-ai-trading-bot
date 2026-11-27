"""Research only: is AI #1 overconfident, and would shrinking its P(UP) toward the past base rate make calls
more reliable? Non-reserved data only (opens before the v3 freeze, 2026-11-22 01:00 UTC).
Selection on the first 60% (chronological), check once on the remaining 40%.
"""
import json, math, sys
from datetime import datetime, timezone
import numpy as np
sys.path.insert(0, "scripts"); sys.path.insert(0, ".")
import forward_eval_v3 as v3
D = sys.argv[1] if len(sys.argv) > 1 else "../forward-v2/20261123T020628Z/"
rows = [r for r in v3.load(D + "journal_dump.json", freeze=datetime(2026, 11, 16, tzinfo=timezone.utc)) if r["opens"] < v3.FREEZE_V3]
y = np.array([r["y"] for r in rows], float); p = np.array([r["live"] for r in rows])
cut = int(len(rows) * 0.6)
def slope(pp, yy):
    z = np.log(np.clip(pp, 1e-6, 1 - 1e-6) / np.clip(1 - pp, 1e-6, 1)); w = np.zeros(2)
    for _ in range(3000):
        q = 1 / (1 + np.exp(-(w[0] + w[1] * z))); w -= 0.5 * np.array([np.mean(q - yy), np.mean((q - yy) * z)])
    return w
def wil(k, n):
    if not n: return None
    zz = 1.96; ph = k / n; d = 1 + zz * zz / n
    return round((ph + zz * zz / (2 * n) - zz * math.sqrt(ph * (1 - ph) / n + zz * zz / (4 * n * n))) / d, 3)
def calls(pp, yy, t=0.55):
    m = (pp >= t) | (pp <= 1 - t); k = int(((pp[m] >= .5) == (yy[m] == 1)).sum()); n = int(m.sum())
    return n, k, (k / n if n else None), wil(k, n), float(np.mean((pp - yy) ** 2)), float((pp[m] >= t).mean()) if n else None
w = slope(p[:cut], y[:cut])
print(f"markets {len(rows)} (fit {cut}, check {len(rows)-cut}); calibration on fit: intercept {w[0]:+.3f} slope {w[1]:.3f} (slope<1 = overconfident)")
print("check-window calibration slope:", round(slope(p[cut:], y[cut:])[1], 3))
best = None
for t in (0.55, 0.57, 0.60, 0.62, 0.65):
    n, k, acc, lo, br, up = calls(p[:cut], y[:cut], t)
    print(f"  FIT t={t}: calls {n} right {k} acc {acc and round(acc,3)} lo {lo} UPshare {up and round(up,2)}")
    if n >= 30 and lo is not None and (best is None or lo > best[1]): best = (t, lo)
t = best[0]
for name, sl in (("live t=0.55", slice(cut, None)),):
    pass
n0, k0, a0, l0, b0, u0 = calls(p[cut:], y[cut:], 0.55)
n1, k1, a1, l1, b1, u1 = calls(p[cut:], y[cut:], t)
print(f"SELECTED on fit: threshold {t}")
print(f"CHECK live 0.55: calls {n0} right {k0} acc {a0 and round(a0,3)} lo {l0} coverage {n0/(len(rows)-cut):.2f} UPshare {u0 and round(u0,2)} Brier {b0:.4f}")
print(f"CHECK sel  {t}: calls {n1} right {k1} acc {a1 and round(a1,3)} lo {l1} coverage {n1/(len(rows)-cut):.2f} UPshare {u1 and round(u1,2)}")
