import math
from collections import defaultdict

from .models import ConsensusForecast, MarketSignalContext, SignalEstimate
from .providers import SignalProvider, default_providers


class MultiSignalEngine:
    """Reliability-weighted shadow consensus with freshness/redundancy penalties."""

    def __init__(self, providers: tuple[SignalProvider, ...] | None = None,
                 *, max_age_seconds: float = 900, minimum_signals: int = 2,
                 minimum_independent_groups: int = 2):
        self.providers = default_providers() if providers is None else providers
        self.max_age_seconds = max_age_seconds
        self.minimum_signals = minimum_signals
        self.minimum_independent_groups = minimum_independent_groups

    def forecast(self, context: MarketSignalContext) -> ConsensusForecast:
        raw = list(context.external_signals)
        rejected = []
        for provider in self.providers:
            if not provider.supports(context):
                continue
            try:
                estimate = provider.estimate(context)
            except Exception:
                rejected.append(f"{provider.name}:provider_failure")
                continue
            if estimate is not None:
                raw.append(estimate)
        weighted: list[tuple[SignalEstimate, float]] = []
        groups = defaultdict(int)
        for signal in raw:
            categories = signal.metadata.get("categories")
            if categories is not None:
                if (not isinstance(categories, (list, tuple, set, frozenset))
                        or context.category.casefold() not in {
                            str(category).casefold() for category in categories
                        }):
                    rejected.append(f"{signal.provider}:category_not_applicable")
                    continue
            age = max(0.0, (context.now - signal.observed_at).total_seconds())
            if age > self.max_age_seconds:
                rejected.append(f"{signal.provider}:stale")
                continue
            freshness = math.exp(-age / max(1.0, self.max_age_seconds))
            sample = 1.0 if signal.sample_size is None else min(1.0, math.sqrt(max(1, signal.sample_size)) / 10)
            groups[signal.correlation_group] += 1
            redundancy = 1 / groups[signal.correlation_group]
            weighted.append((signal, signal.reliability * freshness * sample * redundancy))
        independent_groups = len({signal.correlation_group for signal, _ in weighted})
        if (len(weighted) < self.minimum_signals
                or independent_groups < self.minimum_independent_groups
                or sum(weight for _, weight in weighted) <= 0):
            raise ValueError("insufficient independent fresh signals")
        denominator = sum(weight for _, weight in weighted)
        probability = sum(signal.probability_yes * weight for signal, weight in weighted) / denominator
        variance = sum(weight * (signal.probability_yes - probability) ** 2 for signal, weight in weighted) / denominator
        disagreement = min(1.0, math.sqrt(variance) * 2)
        completeness = min(1.0, independent_groups / 4)
        mean_reliability = sum(signal.reliability for signal, _ in weighted) / len(weighted)
        confidence = max(0.0, min(1.0, mean_reliability * completeness * (1 - disagreement)))
        oldest = max((context.now - signal.observed_at).total_seconds() for signal, _ in weighted)
        return ConsensusForecast(
            context.market_id, probability, confidence, disagreement,
            1 - disagreement, tuple(signal for signal, _ in weighted),
            tuple(rejected), oldest,
        )
