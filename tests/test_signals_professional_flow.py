from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.signals_display.adaptive_weights import AdaptiveWeights
from src.signals_display.cross_exchange_flow import NormalizedFlow, SequenceGap, divergence
from src.signals_display.kalshi_walls import WallTracker


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def test_normalized_order_book_sequence_flow_and_vpin():
    flow = NormalizedFlow("test", "BTC-USD", maximum_levels=20, maximum_trades=100)
    flow.snapshot(bids=[("100", "5"), ("99", "3")], asks=[("101", "4"), ("102", "2")],
                  sequence=10, event_at=NOW.isoformat(), observed_at=NOW.isoformat())
    flow.delta(side="bid", price="100", size="7", sequence=11,
               event_at=NOW.isoformat(), observed_at=NOW.isoformat())
    flow.trade(price=101, size=8, aggressor="buy", event_at=NOW.isoformat(), observed_at=NOW.isoformat())
    flow.trade(price=100, size=2, aggressor="sell", event_at=NOW.isoformat(), observed_at=NOW.isoformat())
    report = flow.metrics(bucket_volume=Decimal("10"))
    assert report["status"] == "LIVE" and report["sequence"] == 11
    assert report["depth_imbalance"] == "0.25"
    assert report["cvd"] == "6"
    assert report["vpin_style_toxicity"] == "0.6"


def test_sequence_gap_invalidates_book_until_snapshot():
    flow = NormalizedFlow("test", "BTC-USD")
    flow.snapshot(bids=[(100, 1)], asks=[(101, 1)], sequence=1,
                  event_at=NOW.isoformat(), observed_at=NOW.isoformat())
    with pytest.raises(SequenceGap):
        flow.delta(side="bid", price=100, size=2, sequence=3,
                   event_at=NOW.isoformat(), observed_at=NOW.isoformat())
    assert flow.metrics()["status"] == "STALE_OR_DISCONNECTED"
    assert flow.diagnostics["sequence_gaps"] == 1


def test_cross_exchange_divergence_requires_two_fresh_venues():
    one = {"status": "LIVE", "venue": "a", "best_bid": "100", "best_ask": "101"}
    two = {"status": "LIVE", "venue": "b", "best_bid": "102", "best_ask": "103"}
    assert divergence([one])["status"] == "UNAVAILABLE"
    result = divergence([one, two])
    assert Decimal(result["maximum_midpoint_divergence"]) == Decimal("2")
    assert result["predictive_effect"] == "UNVALIDATED"


def test_wall_tracker_measures_persistence_without_institutional_claim():
    tracker = WallTracker(concentration_multiple=2)
    first = tracker.update([(.45, 2), (.44, 2), (.43, 10)], [(.55, 2), (.56, 2)])
    second = tracker.update([(.45, 2), (.44, 2), (.43, 12)], [(.55, 2), (.56, 2)])
    wall = second["walls"]["bid"][0]
    assert wall["persistence_updates"] == 2
    assert Decimal(second["replenished_quantity"]) == 2
    assert first["institutional_claim"] is False and second["actionable"] is False


def test_adaptive_weights_use_verified_past_outcomes_and_separate_kinds():
    weights = AdaptiveWeights(["baseline", "candidate"], prior_strength=20, minimum_verified=2)
    opening = weights.regime(forecast_kind="opening", volatility="high", trend="trend",
                             time_band="open", distance_band="near", liquidity="good",
                             spread="tight", flow="buy")
    settlement = weights.regime(forecast_kind="settlement", volatility="high", trend="trend",
                                time_band="late", distance_band="near", liquidity="good",
                                spread="tight", flow="buy")
    assert not weights.update(opening, {"baseline": .4, "candidate": .7}, 1, verified=False,
                              predicted_at=NOW, outcome_available_at=NOW + timedelta(minutes=15))
    assert weights.weights(opening)["status"] == "CONSERVATIVE_FALLBACK"
    for index in range(2):
        assert weights.update(opening, {"baseline": .4, "candidate": .7}, 1, verified=True,
                              predicted_at=NOW + timedelta(minutes=index * 15),
                              outcome_available_at=NOW + timedelta(minutes=index * 15 + 15))
    report = weights.combine(opening, {"baseline": .4, "candidate": .7})
    assert report["status"] == "ADAPTIVE_SHADOW" and not report["actionable"]
    assert report["weights"]["candidate"] > report["weights"]["baseline"]
    assert weights.weights(settlement)["status"] == "CONSERVATIVE_FALLBACK"


def test_adaptive_weighting_rejects_outcome_available_before_prediction():
    weights = AdaptiveWeights(["a", "b"])
    regime = weights.regime(forecast_kind="opening", volatility="low", trend="flat",
                            time_band="open", distance_band="far", liquidity="low",
                            spread="wide", flow="neutral")
    assert not weights.update(regime, {"a": .6, "b": .4}, 1, verified=True,
                              predicted_at=NOW, outcome_available_at=NOW)
