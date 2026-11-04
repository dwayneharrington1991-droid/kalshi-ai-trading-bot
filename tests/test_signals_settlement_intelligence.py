from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

from src.signals_display.contract_rules import snapshot
from src.signals_display.fair_value import compare, fee
from src.signals_display.realtime_workers import LatestValueWorker
from src.signals_display.settlement_probability import estimate
from src.signals_display.settlement_reference import SettlementReference


NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def metadata(**updates):
    value = {
        "ticker": "KXBTC15M-TEST", "series_ticker": "KXBTC15M",
        "floor_strike": "100000.25", "cap_strike": None,
        "strike_type": "greater_or_equal", "open_time": NOW.isoformat(),
        "close_time": (NOW + timedelta(minutes=15)).isoformat(),
        "rules_primary": "Value is at least the threshold using the published average.",
        "rules_secondary": "Official calculation controls.",
        "tick_size_dollars": "0.01",
        "settlement_spec": {"method": "arithmetic_mean", "window_seconds": 60,
                            "observation_interval_seconds": 1},
    }
    value.update(updates)
    return value


SERIES = {"settlement_sources": [{"name": "CF Benchmarks BRTI"}]}


def test_contract_snapshot_is_decimal_safe_fingerprinted_and_immutable():
    result = snapshot(metadata(), SERIES, received_at=NOW.isoformat())
    assert result.strike == "100000.25"
    assert result.comparison_operator == ">=" and result.equality_wins_yes
    assert result.price_increment == "0.01"
    assert result.rule_status == "STRUCTURED_SETTLEMENT_RULE_VERIFIED"
    assert len(result.rule_fingerprint) == 64
    try:
        result.strike = "1"
        assert False, "frozen snapshot accepted mutation"
    except Exception:
        pass


def test_rules_fail_closed_for_range_unknown_equality_and_unstructured_average():
    assert snapshot(metadata(cap_strike="100001"), SERIES, received_at=NOW.isoformat())["rule_status"] == "INVALID_OR_INCOMPLETE_METADATA"
    assert snapshot(metadata(strike_type="mystery"), SERIES, received_at=NOW.isoformat())["rule_status"] == "INVALID_OR_INCOMPLETE_METADATA"
    result = snapshot(metadata(settlement_spec=None), SERIES, received_at=NOW.isoformat())
    assert result.rule_status == "SETTLEMENT_CALCULATION_UNVERIFIED"
    assert result.averaging_window_seconds is None


def test_exact_authoritative_kalshi_rule_text_can_supply_the_average_specification():
    text = (
        "The value uses the BRTI simple average of the sixty seconds before close; "
        "the comparison uses the BRTI simple average of the sixty seconds at close."
    )
    result = snapshot(metadata(settlement_spec=None, rules_primary=text), SERIES,
                      received_at=NOW.isoformat())
    assert result.rule_status == "STRUCTURED_SETTLEMENT_RULE_VERIFIED"
    assert result.averaging_window_seconds == 60
    assert result.observation_interval_seconds == 1
    assert result.calculation_evidence == "EXACT_KALSHI_RULE_TEXT_MATCH"


def test_price_increment_requires_explicit_numeric_metadata():
    result = snapshot(metadata(tick_size_dollars=None, price_level_structure="linear_cent"), SERIES,
                      received_at=NOW.isoformat())
    assert result.price_increment is None


def test_reference_average_rejects_missing_duplicate_late_and_clock_skew_ticks():
    accumulator = SettlementReference(
        NOW, NOW + timedelta(seconds=3), 1, source="external proxy",
        authorized_official_source=False,
    )
    assert accumulator.add(NOW, NOW + timedelta(milliseconds=20), "100")
    assert not accumulator.add(NOW, NOW + timedelta(milliseconds=30), "101")
    assert accumulator.add(NOW + timedelta(seconds=1), NOW + timedelta(seconds=1.1), "101")
    partial = accumulator.result()
    assert partial["status"] == "INCOMPLETE" and partial["missing_ticks"] == 1
    assert partial["mode"] == "ESTIMATED_REFERENCE" and not partial["official"]
    assert not accumulator.add(NOW + timedelta(seconds=2), NOW + timedelta(seconds=40), "102")
    assert accumulator.add(NOW + timedelta(seconds=2), NOW + timedelta(seconds=2.2), "102")
    complete = accumulator.result()
    assert complete["status"] == "COMPLETE" and complete["value"] == "101"


def test_time_probability_respects_time_distance_volatility_and_equality():
    near = estimate(reference="100001", strike="100000", seconds_remaining=1,
                    realized_volatility_per_sqrt_second="1", comparison_operator=">=")
    far = estimate(reference="100001", strike="100000", seconds_remaining=900,
                   realized_volatility_per_sqrt_second="1", comparison_operator=">=")
    assert Decimal(near["probability_yes"]) > Decimal(far["probability_yes"]) > Decimal("0.5")
    equal_ge = estimate(reference="100000", strike="100000", seconds_remaining=0,
                        realized_volatility_per_sqrt_second=0, comparison_operator=">=")
    equal_gt = estimate(reference="100000", strike="100000", seconds_remaining=0,
                        realized_volatility_per_sqrt_second=0, comparison_operator=">")
    equal_le = estimate(reference="100000", strike="100000", seconds_remaining=0,
                        realized_volatility_per_sqrt_second=0, comparison_operator="<=")
    equal_lt = estimate(reference="100000", strike="100000", seconds_remaining=0,
                        realized_volatility_per_sqrt_second=0, comparison_operator="<")
    assert equal_ge["probability_yes"] == "1" and equal_gt["probability_yes"] == "0"
    assert equal_le["probability_yes"] == "1" and equal_lt["probability_yes"] == "0"
    assert near["status"] == "SHADOW_UNCALIBRATED" and not near["actionable"]


def test_probability_includes_jump_and_data_uncertainty():
    base = estimate(reference=101, strike=100, seconds_remaining=10,
                    realized_volatility_per_sqrt_second="0.1")
    uncertain = estimate(reference=101, strike=100, seconds_remaining=10,
                         realized_volatility_per_sqrt_second="0.1",
                         jump_volatility_per_sqrt_second="0.5", data_uncertainty="1")
    assert Decimal(uncertain["effective_sigma"]) > Decimal(base["effective_sigma"])
    assert Decimal(uncertain["probability_yes"]) < Decimal(base["probability_yes"])


def test_decimal_fair_value_fee_depth_and_tick_precision():
    assert fee("0.50", 1, 1) == Decimal("0.02")
    result = compare(probability="0.60", bid="0.48", ask="0.50", ask_depth="3",
                     quantity="2", fee_multiplier="1", price_increment="0.01",
                     slippage_ticks="1")
    assert result["slippage_adjusted_entry"] == "0.51"
    assert result["estimated_fee"] == "0.04"
    assert result["net_expected_value"] == "0.14"
    assert not result["actionable"] and result["edge_status"] == "UNVERIFIED"
    blocked = compare(probability=.6, bid=.48, ask=.5, ask_depth=1, quantity=2,
                      fee_multiplier=1, price_increment=.01)
    assert blocked["status"] == "UNAVAILABLE"


def test_latest_value_worker_is_bounded_and_never_applies_older_result():
    def calculation(value):
        if value == "slow":
            time.sleep(.03)
        return value
    worker = LatestValueWorker(calculation, max_queue=1)
    worker.start()
    worker.submit(1, "slow")
    time.sleep(.005)
    worker.submit(2, "old")
    worker.submit(3, "new")
    deadline = time.monotonic() + 1
    while worker.snapshot()["metrics"]["completed"] < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    state = worker.snapshot()
    worker.close()
    assert state["latest_sequence"] == 3 and state["latest"] == "new"
    assert state["queue_depth"] == 0
    assert state["metrics"]["dropped_superseded"] >= 1
