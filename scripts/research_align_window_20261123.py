import json, sys
import numpy as np
from datetime import datetime, timezone
sys.path.insert(0, "scripts"); sys.path.insert(0, ".")
import forward_eval_v4 as v4
D = "../forward-v2/20261123T020628Z/"
art = json.load(open(D + "live_artifact.json")); align = json.load(open(str(v4.ALIGNMENT)))
rows = v4.load(D + "journal_dump.json", freeze=datetime(2026, 11, 16, tzinfo=timezone.utc))
CHANGE = datetime(2026, 11, 20, 20, 21, tzinfo=timezone.utc)
W = [r for r in rows if CHANGE <= r["opens"] < v4.FREEZE_V4]           # skew window, not reserved for v4
pre = [r for r in rows if r["opens"] < CHANGE]
y = np.array([r["y"] for r in W]); live = np.array([r["live"] for r in W])
al = np.array([v4.aligned_probability(art, align, r["features"], r["live"]) for r in W])
def desc(p, y):
    c = v4._calls(p, y); b = float(np.mean((p - y) ** 2))
    return f"calls {c['calls']} acc {c['accuracy'] and round(c['accuracy'],3)} UP-share {c['up_calls']/max(c['calls'],1):.2f} (UP {c['up_correct']}/{c['up_calls']}, DOWN {c['down_correct']}/{c['down_calls']}) mean P {p.mean():.3f} Brier {b:.4f}"
print(f"skew window {W[0]['opens']:%m-%d %H:%M} .. {W[-1]['opens']:%m-%d %H:%M}: n {len(W)}, UP outcome rate {y.mean():.3f}, base Brier {y.mean()*(1-y.mean()):.4f}")
print("  AI #1 live   :", desc(live, y))
print("  AI #1 aligned:", desc(al, y))
rng = np.random.default_rng(7); d = (al - y) ** 2 - (live - y) ** 2
bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(5000)]
print(f"  Brier(aligned)-Brier(live) = {d.mean():+.4f}, 95% CI [{np.percentile(bs,2.5):+.4f}, {np.percentile(bs,97.5):+.4f}]")
yp = np.array([r["y"] for r in pre]); lp = np.array([r["live"] for r in pre])
print(f"pre-change n {len(pre)}: AI #1 live", desc(lp, yp))
