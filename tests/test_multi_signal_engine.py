from datetime import datetime, timedelta, timezone

import pytest

from src.signals.calibration import SignalCalibrationRepository
from src.signals.consistency import check_complements, check_exclusive_outcomes
from src.signals.engine import MultiSignalEngine
from src.signals.fees import calculate_taker_fee
from src.signals.mispricing import evaluate_mispricing
from src.signals.models import MarketSignalContext, SignalEstimate
from src.signals.orderbook import executable_quote


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def signal(name, probability, reliability=.8, age=0, group=None, sample=100):
    return SignalEstimate(name, probability, reliability,
                          NOW - timedelta(seconds=age), group or name, sample)


def context(signals, *, quantity=5):
    return MarketSignalContext(
        "MKT", "sports", "PRE_GAME",
        {"yes_bid_dollars": ".55", "yes_ask_dollars": ".56",
         "no_bid_dollars": ".43", "no_ask_dollars": ".44"},
        {"orderbook_fp": {"no_dollars": [[".44", "20"]],
                          "yes_dollars": [[".56", "20"]]}},
        quantity, tuple(signals), (), NOW,
    )


def test_reliability_freshness_and_redundancy_weight_consensus():
    signals = [signal("poll", .70, .9, group="polls"),
               signal("duplicate_poll", .71, .9, group="polls"),
               signal("sports_stats", .66, .8, group="stats"),
               signal("stale_news", .95, .9, age=5000, group="news")]
    result = MultiSignalEngine(providers=(), max_age_seconds=900).forecast(context(signals))
    assert .65 < result.probability_yes < .72
    assert "stale_news:stale" in result.rejected_signals
    assert result.disagreement < .1
    assert result.calibrated_confidence < .9


def test_external_public_signals_are_routed_by_category():
    weather = SignalEstimate("weather", .8, .9, NOW, "weather_api", 100,
                             {"categories": ["weather"]})
    sports = signal("sports_stats", .66, group="sports_stats")
    result = MultiSignalEngine(providers=()).forecast(context([weather, sports, signal("model", .67)]))
    assert "weather:category_not_applicable" in result.rejected_signals
    assert all(item.provider != "weather" for item in result.signals)


def test_insufficient_independent_evidence_fails_closed():
    with pytest.raises(ValueError, match="insufficient independent fresh signals"):
        MultiSignalEngine(providers=(), minimum_signals=2).forecast(context([signal("one", .7)]))
    with pytest.raises(ValueError, match="independent"):
        MultiSignalEngine(providers=()).forecast(context([
            signal("one", .7, group="same"), signal("copy", .71, group="same")
        ]))


def test_mispricing_uses_dollar_costs_and_executable_depth():
    forecast = MultiSignalEngine(providers=(), minimum_signals=2).forecast(
        context([signal("stats", .68), signal("poll", .70)])
    )
    decision = evaluate_mispricing(context(forecast.signals), forecast,
                                   fee_dollars=.03, slippage_dollars=.01)
    assert decision.side == "YES"
    assert decision.net_ev_dollars > 0
    assert decision.executable_liquidity == 20
    assert decision.accepted


def test_extreme_discrepancy_and_missing_depth_fail_closed():
    ctx = context([signal("a", .95), signal("b", .96)])
    forecast = MultiSignalEngine(providers=(), minimum_signals=2).forecast(ctx)
    assert not evaluate_mispricing(ctx, forecast, fee_dollars=0, slippage_dollars=0).accepted
    shallow = MarketSignalContext(ctx.market_id, ctx.category, ctx.phase, ctx.market,
                                  {"orderbook_fp": {"no_dollars": []}}, 5,
                                  ctx.external_signals, (), NOW)
    normal = MultiSignalEngine(providers=(), minimum_signals=2).forecast(
        context([signal("a", .66), signal("b", .67)])
    )
    assert "depth" in evaluate_mispricing(shallow, normal, fee_dollars=0,
                                           slippage_dollars=0).reason


def test_unverified_fee_fails_closed():
    ctx = context([signal("a", .68), signal("b", .69)])
    forecast = MultiSignalEngine(providers=(), minimum_signals=2).forecast(ctx)
    decision = evaluate_mispricing(ctx, forecast, fee_dollars=None, slippage_dollars=0)
    assert not decision.accepted
    assert "fee" in decision.reason


def test_actual_quadratic_fee_uses_series_multiplier_and_rounds_up():
    assert calculate_taker_fee(
        fee_type="quadratic", fee_multiplier=1.0, price=.50, quantity=1
    ) == .02
    assert calculate_taker_fee(
        fee_type="flat", fee_multiplier=1.0, price=.50, quantity=1
    ) is None


def test_orderbook_walk_handles_yes_no_inversion_and_slippage():
    book = {"orderbook_fp": {
        "no_dollars": [[".45", "2"], [".40", "4"]],
        "yes_dollars": [[".35", "3"], [".30", "5"]],
    }}
    yes = executable_quote(book, "YES", 5, maximum_price=.61)
    assert yes.best_price == pytest.approx(.55)
    assert yes.average_price == pytest.approx(.58)
    assert yes.slippage_dollars == pytest.approx(.15)
    no = executable_quote(book, "NO", 4, maximum_price=.71)
    assert no.best_price == pytest.approx(.65)
    assert no.average_price == pytest.approx(.6625)
    assert no.slippage_dollars == pytest.approx(.05)


def test_orderbook_walk_fails_closed_on_bad_or_insufficient_book():
    assert executable_quote({}, "YES", 2).fillable_quantity == 0
    assert executable_quote(
        {"orderbook_fp": {"no_dollars": [["bad", "2"]]}}, "YES", 2
    ).fillable_quantity == 0


def test_calibration_persistence_is_idempotent(tmp_path):
    repo = SignalCalibrationRepository(tmp_path / "signals.db")
    repo.migrate()
    kwargs = dict(market_id="MKT", category="sports", probability_yes=.7,
                  confidence=.6, expected_ev_dollars=.2,
                  provider_contributions={"stats": .8},
                  created_at="2026-01-01T00:00:00+00:00")
    assert repo.record(**kwargs) == repo.record(**kwargs)
    assert repo.settle("MKT", 1, .4, "2026-01-02T00:00:00+00:00") == 1
    bucket = next(item for item in repo.report() if item.label == "70-80%")
    assert bucket.predictions == 1
    assert bucket.win_rate == 1
    assert bucket.brier_score == pytest.approx(.09)
    provider = repo.provider_category_report()[0]
    assert provider == {
        "category": "sports", "provider": "stats", "samples": 1,
        "brier_score": pytest.approx(.09), "win_rate": 1.0,
    }


def test_cross_market_consistency_is_diagnostic_not_arbitrage_claim():
    finding = check_exclusive_outcomes((.40, .35, .35))
    assert finding.requires_review
    assert "relative-value" in finding.reason
    assert "arbitrage" not in finding.reason
    assert not check_complements(.52, .48).requires_review


def test_cross_market_consistency_rejects_incomplete_or_malformed_inputs():
    with pytest.raises(ValueError):
        check_exclusive_outcomes(())
    with pytest.raises(ValueError):
        check_exclusive_outcomes((.4, 1.2))
