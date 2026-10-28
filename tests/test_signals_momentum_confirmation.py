from src.signals_display.momentum_confirmation import describe


def test_adaptive_momentum_is_volatility_normalized_shadow_data():
    report = describe(
        {
            "return_1m": 0.001,
            "return_5m": 0.002,
            "return_15m": 0.003,
            "volatility_24h": 0.0001,
            "distance_scaled": 0.002,
            "remaining_fraction": 0.5,
            "acceleration": -0.0002,
        },
        {"quotes": {"up": {"bid": 0.6, "ask": 0.62}}},
    )
    assert report["status"] == "EXPERIMENTAL_SHADOW"
    assert report["features"]["adaptive_momentum_consistency"] == 1
    assert report["features"]["adaptive_threshold_distance"] > 0
    assert report["features"]["adaptive_reversal"] == 0
    assert report["market_benchmark"]["up_executable_ask"] == 0.62
    assert report["actionable"] is False


def test_missing_or_zero_volatility_never_invents_normalized_features():
    report = describe({"return_1m": 0.1, "volatility_24h": 0})
    assert report["features"]["adaptive_momentum_strength"] is None
    assert report["features"]["adaptive_threshold_distance"] is None
    assert report["market_benchmark"] is None
