"""Past-only regularized Bayesian model weights, separate by forecast kind."""
from __future__ import annotations

from collections import defaultdict
from math import log


class AdaptiveWeights:
    def __init__(self, model_names, *, prior_strength=20, minimum_verified=30):
        if not model_names or prior_strength < 2 or minimum_verified < 1:
            raise ValueError("invalid adaptive weighting configuration")
        self.models = tuple(model_names)
        self.prior_strength = float(prior_strength)
        self.minimum_verified = minimum_verified
        self.stats = defaultdict(lambda: {name: [self.prior_strength / 2, self.prior_strength / 2, 0]
                                            for name in self.models})

    @staticmethod
    def regime(*, forecast_kind, volatility, trend, time_band, distance_band, liquidity, spread, flow):
        if forecast_kind not in {"opening", "settlement"}:
            raise ValueError("opening and settlement models cannot share a regime")
        return (forecast_kind, volatility, trend, time_band, distance_band, liquidity, spread, flow)

    def update(self, regime, predictions, outcome, *, verified, predicted_at, outcome_available_at):
        if not verified or outcome not in {0, 1} or outcome_available_at <= predicted_at:
            return False
        bucket = self.stats[regime]
        for name in self.models:
            p = predictions.get(name)
            if not isinstance(p, (int, float)) or not 0 <= p <= 1:
                continue
            correct = (p >= .5) == bool(outcome)
            bucket[name][0 if correct else 1] += 1
            bucket[name][2] += 1
        return True

    def weights(self, regime):
        values = self.stats[regime]
        if sum(v[2] for v in values.values()) < self.minimum_verified * len(self.models):
            equal = 1 / len(self.models)
            return {"status": "CONSERVATIVE_FALLBACK", "weights": {name: equal for name in self.models},
                    "verified_samples": {name: values[name][2] for name in self.models}}
        scores = {name: max(1e-9, a / (a + b)) for name, (a, b, _) in values.items()}
        total = sum(scores.values())
        return {"status": "ADAPTIVE_SHADOW", "weights": {name: value / total for name, value in scores.items()},
                "verified_samples": {name: values[name][2] for name in self.models}}

    def combine(self, regime, predictions):
        report = self.weights(regime)
        available = {name: p for name, p in predictions.items()
                     if name in report["weights"] and isinstance(p, (int, float)) and 0 < p < 1}
        if not available:
            return {**report, "probability": None, "actionable": False}
        weights = {name: report["weights"][name] for name in available}
        scale = sum(weights.values())
        # Weighted log odds avoids treating a 0.9 model as an additive score.
        logit = sum(weights[name] / scale * log(p / (1 - p)) for name, p in available.items())
        probability = 1 / (1 + pow(2.718281828459045, -logit))
        return {**report, "probability": probability, "actionable": False,
                "validation": "Untouched forward comparison required before promotion"}
