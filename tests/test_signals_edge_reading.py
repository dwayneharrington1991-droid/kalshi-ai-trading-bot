"""Observable edge candidates, with no future labels, account access or probabilities."""
from datetime import timedelta
from copy import deepcopy
import pytest
from src.signals_display.edge_reading import assess, flow_metrics
from src.signals_display.coverage import coinbase_daily_levels
from src.signals_display.features import extract
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def candles():
    return [
        dict(
            start=int(NOW.timestamp()) - 60 * (21 - i),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=10,
        )
        for i in range(21)
    ]


def inputs():
    p = frame()
    p["reference"]["price"] = 100
    p["btc_candles"] = {"received_at": NOW.isoformat()}
    info = {"candidate_features": {"closed_candles": candles()}}
    return p, p["markets"][0], info


def trades(buy=True):
    return [
        (
            str(i),
            NOW - timedelta(seconds=180 - i),
            100 + i / 1000,
            1,
            "buy" if buy else "buy" if i % 2 else "sell",
            0,
        )
        for i in range(181)
    ]


def test_dem_is_signed_and_near_zero_delta_is_unavailable():
    out = flow_metrics(trades(), NOW - timedelta(seconds=180), NOW)
    assert out["status"] == "LIVE" and out["delta_efficiency"] == pytest.approx(0.18 / 181)
    tiny = flow_metrics(trades(False), NOW - timedelta(seconds=180), NOW)
    assert tiny["delta_efficiency"] is None and tiny["net_volume_3m"] == -1
    assert flow_metrics(trades()[:-6], NOW - timedelta(seconds=180), NOW)["status"] == "unavailable"


def test_dem_fragility_requires_price_impact_net_buy_fraction_and_volume_taper():
    rows = []
    for i in range(181):
        rows.append(
            (
                str(i),
                NOW - timedelta(seconds=180 - i),
                100 + (0.2 * i / 180),
                0.25 if i > 120 else 1,
                "buy" if i % 10 < 6 else "sell",
                0,
            )
        )
    # Enforce a measured net buy fraction in the declared 5-20% range.
    for i, row in enumerate(rows):
        rows[i] = (*row[:4], "buy" if i % 20 < 11 else "sell", 0)
    out = flow_metrics(rows, NOW - timedelta(seconds=180), NOW)
    assert out["fragile_upward_impact_candidate"] and out["volume_taper_ratio"] < 0.7


def test_absorption_is_candidate_with_no_institution_or_probability_attribution():
    p, m, info = inputs()
    p["coverage"] = {
        "Kraken public order flow": {
            "data": {
                "spot": dict(
                    status="LIVE",
                    observed_at=NOW.isoformat(),
                    passive_sell_absorption_candidate=True,
                )
            }
        }
    }
    core = assess(p, m, info, NOW)
    assert core["candidate_lean"] == "DOWN" and core["probability"] is None
    assert core["probability_adjustment"] == 0 and core["line"].startswith("Edge Reading Core:")
    assert "identity/icebergs" in core["signatures"][0]["reason"]
    p["coverage"]["Kraken public order flow"]["data"]["spot"]["observed_at"] = (
        NOW - timedelta(seconds=6)
    ).isoformat()
    assert not assess(p, m, info, NOW)["signatures"]


def test_sweep_uses_preexisting_level_closed_bar_and_declining_volume():
    p, m, info = inputs()
    bar = info["candidate_features"]["closed_candles"][-1]
    bar.update(high=103, volume=1)
    core = assess(p, m, info, NOW)
    assert core["candidate_lean"] == "DOWN"
    assert core["signatures"][0]["level"] == 101 and core["probability"] is None
    bar["volume"] = 20
    assert not assess(p, m, info, NOW)["signatures"]
    bar.update(volume=1, start=int(NOW.timestamp()))
    assert not assess(p, m, info, NOW)["signatures"]


def test_opposing_signatures_stay_unclear_and_stale_market_clears_signatures():
    p, m, info = inputs()
    info["candidate_features"]["closed_candles"][-1].update(low=97, volume=1)
    p["coverage"] = {
        "Kraken public order flow": {
            "data": {
                "spot": dict(
                    status="LIVE",
                    observed_at=NOW.isoformat(),
                    passive_sell_absorption_candidate=True,
                )
            }
        }
    }
    assert assess(p, m, info, NOW)["candidate_lean"] == "UNCLEAR"
    m["observed_at"] = (NOW - timedelta(seconds=6)).isoformat()
    core = assess(p, m, info, NOW)
    assert not core["signatures"] and "unavailable" in core["line"]


def test_pinning_is_unavailable_even_near_target_in_final_minutes():
    p, m, info = inputs()
    m.update(strike=100, closes_at=(NOW + timedelta(seconds=120)).isoformat())
    core = assess(p, m, info, NOW)
    assert core["pinning"]["final_three_minutes"] and core["pinning"]["distance_to_target_bps"] == 0
    assert core["pinning"]["status"] == "unavailable" and core["probability_adjustment"] == 0


def test_daily_levels_are_utc_and_forming_session_is_not_previous_day(monkeypatch):
    monkeypatch.setattr("src.signals_display.coverage.now_utc", lambda: NOW)
    day = int(NOW.timestamp()) // 86400 * 86400
    rows = [
        dict(start=day - 86400, open=100, high=110, low=90, close=105),
        dict(start=day, open=105, high=111, low=100, close=106),
    ]
    levels = coinbase_daily_levels(lambda url: {"candles": rows})
    assert levels["previous_day"]["closed"] and not levels["session"]["closed"]
    assert levels["previous_day"]["high"] == 110
    with pytest.raises(ValueError):
        coinbase_daily_levels(lambda url: {"candles": rows + rows[:1]})


def test_edge_features_are_fresh_candidates_without_probability_override():
    p, m, info = inputs()
    p["coverage"] = {
        "Kraken public order flow": {
            "data": {
                "spot": dict(
                    status="LIVE",
                    observed_at=NOW.isoformat(),
                    delta_fraction_60s=0.5,
                    passive_sell_absorption_candidate=True,
                    delta_efficiency_3m={"status": "LIVE", "delta_efficiency": 2.5},
                )
            }
        }
    }
    f = extract(p, m, NOW)["values"]
    assert f["spot_absorption_candidate"] == 1 and f["spot_delta_efficiency_3m"] == 2.5
    assert f["perpetual_absorption_candidate"] is None


def test_recommendation_stays_wait_despite_extreme_model_scores_and_unverified_flags():
    from src.signals_display.decision import assess as decide

    state = {
        "stale": False,
        "direction": "UP",
        "scalp": "SCALP UP",
        "estimate": {"settlement_up": 0.99, "scalp_up": 0.99},
        "model": {"validated": True},
        "profitable_edge_demonstrated": True,
    }
    result = decide(state)
    assert result["action"] == "WAIT" and result["scalp"] == "NO"
    assert (
        result["entry_price"] is result["maximum_risk_dollars"] is result["profit_target"] is None
    )
    assert "validated" in result["reason"]


def test_recommendation_stale_reason_and_account_exit_warning_are_separate():
    from src.signals_display.decision import assess as decide

    result = decide({"stale": True})
    assert "too old" in result["reason"] and result["action"] == "WAIT"
    account = {"status": "READ_ONLY_CONNECTED", "guidance": [{"guidance": "CONSIDER EXIT"}]}
    assert "exit warning is active" in decide({"stale": False, "account": account})["exit_warning"]
    account["status"] = "ACCOUNT_STALE"
    assert (
        "exit warning is active" not in decide({"stale": False, "account": account})["exit_warning"]
    )
