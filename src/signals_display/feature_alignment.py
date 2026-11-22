"""Quantile alignment of inputs whose live computation changed after a model was trained (candidate only).

Regression ledger L13: since deployment 9a9a6ea the micro-structure and trade-flow inputs are computed
over complete windows, so their live distribution differs from the one recorded in the live model's
training data. An alignment maps each affected input through its own post-change distribution onto the
training-era distribution: value -> empirical quantile among post-change inputs -> the same quantile of
the training-era present values. It is built from INPUT VALUES ONLY (never outcomes) and is frozen
before it is evaluated.

Nothing in production imports this module; using it changes predictions and needs explicit approval.
"""
import bisect
import math

FORMAT = "kxbtc-feature-alignment-v1"


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _quantiles(values, points):
    s = sorted(values)
    n = len(s)
    out = []
    for i in range(points):
        q = i / (points - 1) * (n - 1)
        lo = int(math.floor(q))
        hi = min(lo + 1, n - 1)
        out.append(s[lo] + (s[hi] - s[lo]) * (q - lo))
    return out


def build(reference_rows, recent_rows, names, *, points=101, minimum=30, metadata=None):
    """reference_rows / recent_rows: lists of feature dicts (training-era / post-change)."""
    maps = {}
    for name in names:
        ref = [r[name] for r in reference_rows if _finite(r.get(name))]
        new = [r[name] for r in recent_rows if _finite(r.get(name))]
        if len(ref) < minimum or len(new) < minimum:
            continue
        maps[name] = {"recent": _quantiles(new, points), "reference": _quantiles(ref, points),
                      "reference_count": len(ref), "recent_count": len(new)}
    return {"format": FORMAT, "points": points, "maps": maps, **(metadata or {})}


def _map(value, recent, reference):
    if value <= recent[0]:
        return reference[0]
    if value >= recent[-1]:
        return reference[-1]
    j = bisect.bisect_right(recent, value)
    lo, hi = recent[j - 1], recent[j]
    t = 0.5 if hi == lo else (value - lo) / (hi - lo)
    # quantile position between knots j-1 and j, then the reference value at that position
    return reference[j - 1] + (reference[j] - reference[j - 1]) * t


def apply(features, alignment):
    """Return a new feature dict with aligned values; absent or non-finite inputs stay as they are."""
    if alignment.get("format") != FORMAT:
        raise ValueError("Unknown feature alignment format")
    out = dict(features)
    for name, m in alignment["maps"].items():
        v = features.get(name)
        if _finite(v):
            out[name] = _map(float(v), m["recent"], m["reference"])
    return out
