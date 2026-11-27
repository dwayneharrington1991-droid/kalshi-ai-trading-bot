"""Order-flow-enhanced scoring gate — DISABLED. Prepared for a future, explicitly approved promotion only.

Nothing in the live path imports this module today. When (and only when) the user approves a validated OF1
candidate, the opening / minute-6 code would call `enhanced(baseline_p, flow_snapshot)` and use its result.

Safety contract (all must hold, otherwise the unchanged production probability is returned with a reason):
  1. ENABLED flag: the file ~/dual-ai-alerts/ORDERFLOW_MODEL_APPROVED exists and names the exact model sha256.
  2. The frozen model JSON exists and its sha256 equals that approved hash (no silent weight changes).
  3. The order-flow snapshot is fresh (<= MAX_AGE_S before the decision, never after it) and every feature the
     model needs is present and finite.
Any exception falls back to the baseline. The result always says which path produced it, for provenance.
"""
import hashlib
import json
import math
from pathlib import Path

HOME = Path.home() / "dual-ai-alerts"
FLAG = HOME / "ORDERFLOW_MODEL_APPROVED"
MAX_AGE_S = 2.0


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def enhanced(baseline_p, features, snapshot_at, decision_at, model_path=None, flag=FLAG):
    """Returns {"p_up", "source", "reason", "model_sha"}; source is "production" unless every gate passes."""
    out = {"p_up": baseline_p, "source": "production", "reason": None, "model_sha": None}
    try:
        if not Path(flag).exists():
            out["reason"] = "disabled (no approval flag)"
            return out
        approved = Path(flag).read_text().split()[0].strip()
        model_path = Path(model_path or HOME / "orderflow_model.json")
        payload = model_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != approved:
            out["reason"] = "model hash does not match the approved hash"
            return out
        if not _finite(snapshot_at) or not _finite(decision_at) or not 0 <= decision_at - snapshot_at <= MAX_AGE_S:
            out["reason"] = "order-flow snapshot stale or after the decision"
            return out
        model = json.loads(payload)
        names, weights, means, scales = (model[key] for key in ("features", "weights", "mean", "scale"))
        intercept, baseline_weight = model["intercept"], model.get("baseline_weight", 0.0)
        if (not all(isinstance(items, list) for items in (names, weights, means, scales))
                or not names or not len(names) == len(weights) == len(means) == len(scales)
                or not all(isinstance(name, str) and name for name in names)
                or len(set(names)) != len(names)
                or not all(_finite(v) for v in (intercept, baseline_weight, *weights, *means, *scales))
                or not all(scale > 0 for scale in scales)
                or not _finite(baseline_p) or not 0 <= baseline_p <= 1):
            out["reason"] = "invalid model schema or baseline probability"
            return out
        z = intercept + baseline_weight * _logit(baseline_p)
        for name, w, mu, sd in zip(names, weights, means, scales):
            v = features.get(name)
            if not _finite(v):
                out["reason"] = f"missing or invalid feature {name}"
                return out
            z += w * (v - mu) / sd
        if not math.isfinite(z):
            out["reason"] = "non-finite model score"
            return out
        out.update(p_up=1 / (1 + math.exp(-max(-30.0, min(30.0, z)))), source="orderflow", model_sha=approved)
        return out
    except Exception as exc:                                    # never break the live call
        out["reason"] = f"fallback: {type(exc).__name__}"
        return out


def _logit(p):
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))
