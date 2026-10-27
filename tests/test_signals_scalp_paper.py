"""Synthetic fixtures verify a paper protocol, never strategy profitability."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from src.signals_display.scalp_paper import (
    create_schema,
    record,
    evaluate,
    evaluate_journal,
    load_journal,
    cost_fee,
)
from tests.test_signals_display import NOW


def row(
    seconds,
    *,
    bid=0.48,
    ask=0.50,
    ask_size=10,
    bid_size=10,
    observed=None,
    generated=None,
    candidate=True,
    ticker="KXBTC15M-PAPER",
):
    at = NOW + timedelta(seconds=seconds)
    observation = NOW + timedelta(seconds=seconds if observed is None else observed)
    generation = NOW + timedelta(seconds=seconds if generated is None else generated)
    return {
        "timestamp": observation.isoformat(),
        "generated_at": generation.isoformat(),
        "data_cutoff": observation.isoformat(),
        "recorded_at": at.isoformat(),
        "mode": "PUBLIC MARKET DATA",
        "regime": "synthetic sideways",
        "market": {
            "ticker": ticker,
            "observed_at": observation.isoformat(),
            "opens_at": (NOW - timedelta(minutes=1)).isoformat(),
            "closes_at": (NOW + timedelta(minutes=14)).isoformat(),
            "fee_type": "quadratic",
            "fee_multiplier": 1,
            "quotes": {
                "up": {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size},
                "down": {
                    "bid": round(1 - ask, 6),
                    "ask": round(1 - bid, 6),
                    "bid_size": ask_size,
                    "ask_size": bid_size,
                },
            },
        },
        "candidates": [
            {
                "side": "up",
                "target_probability_given_entry": 0.7,
                "net_expected_profit_per_opportunity": 0.02,
                "net_ev_sampling_interval": [0.01, 0.03],
                "entered_paths": 1000,
                "model_definition": {
                    "quantity": 1,
                    "target": 0.05,
                    "stop": 0.10,
                    "holding_seconds": 60,
                    "manual_delay_seconds": 3,
                    "slippage_per_leg": 0.01,
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                },
            }
        ]
        if candidate
        else [],
    }


def candidate(report):
    return report["strategies"]["candidate"]


def test_target_uses_actual_after_delay_quote_and_both_costs():
    result = evaluate([row(0), row(3), row(4, bid=0.62, ask=0.64)])
    stats = candidate(result)
    trade = stats["trades"][0]
    assert stats["simulated_trades"] == stats["observed_complete_trades"] == 1
    assert trade["entry_fills"][0]["observed_at"] == row(3)["market"]["observed_at"]
    assert trade["entry_cost"] == pytest.approx(0.51 + 0.02)
    assert trade["exit_fees"] == pytest.approx(0.02)
    assert trade["net_pnl"] == pytest.approx(0.62 - 0.01 - 0.51 - 0.02 - 0.02)
    assert stats["net_simulated_pnl"] == pytest.approx(0.06)
    assert result["strategies"]["no_trade"]["net_simulated_pnl"] == 0
    assert not result["profitable_edge_demonstrated"] and trade["real_fill"] is False


def test_old_receipt_cannot_fill_after_delay_and_price_cap_can_miss():
    result = evaluate([row(0), row(3.5, observed=0), row(6, bid=0.50, ask=0.52)])
    stats = candidate(result)
    assert stats["simulated_trades"] == 0 and stats["missed_fills"] == 1
    assert stats["net_simulated_pnl"] == 0
    moved = evaluate([row(0), row(3, bid=0.51, ask=0.53), row(6, bid=0.51, ask=0.53)])
    assert candidate(moved)["simulated_trades"] == 0


def test_partial_entry_and_exit_use_depth_once_and_round_each_fee():
    result = evaluate(
        [
            row(0, ask_size=0.5),
            row(3, ask_size=0.5),
            row(3.2, ask_size=0.5),
            row(3.4, ask_size=1),
            row(4, bid=0.40, ask=0.42, bid_size=0.5),
            row(4.2, bid=0.40, ask=0.42, bid_size=0.5),
            row(4.4, bid=0.40, ask=0.42, bid_size=1),
        ]
    )
    stats = candidate(result)
    trade = stats["trades"][0]
    assert len(trade["entry_fills"]) == len(trade["exit_fills"]) == 2
    assert trade["quantity"] == trade["exited_quantity"] == 1
    assert trade["entry_fees"] == trade["exit_fees"] == pytest.approx(0.02)
    assert trade["status"] == "SIMULATED_STOP"
    assert trade["net_pnl"] == pytest.approx(-0.16)
    assert stats["maximum_drawdown"] == pytest.approx(0.16)


def test_repeated_identical_new_receipts_do_not_replenish_liquidity():
    result = evaluate(
        [row(0, ask_size=0.5), row(3, ask_size=0.5), row(4, ask_size=0.5), row(5.1, ask_size=0.5)]
    )
    stats = candidate(result)
    trade = stats["trades"][0]
    assert trade["quantity"] == 0.5 and len(trade["entry_fills"]) == 1
    assert trade["entry_remaining"] == 0
    assert stats["remaining_open_cost_at_risk"] == pytest.approx(0.265)
    assert stats["observed_complete_trades"] == 0


def test_target_requires_enough_liquidity_for_supported_whole_position_exit():
    result = evaluate([row(0), row(3), row(4, bid=0.62, ask=0.64, bid_size=0.2)])
    stats = candidate(result)
    assert stats["open_paper_positions"] == 1 and stats["completed_trades"] == 0
    assert stats["trades"][0]["exit_fills"] == []


def test_gap_censors_later_profitable_quote_without_inventing_stop_price():
    result = evaluate([row(0), row(3), row(10, bid=0.62, ask=0.64)])
    stats = candidate(result)
    trade = stats["trades"][0]
    assert trade["status"] == "UNOBSERVED_PATH" and trade["net_pnl"] is None
    assert trade["observed_cashflow_net_pnl"] == pytest.approx(0.06)
    assert stats["unobserved_outcomes"] == 1 and stats["net_simulated_pnl"] == 0
    assert stats["win_rate_observed_only"] is None


def test_no_deadline_quote_keeps_position_open_until_new_supported_exit():
    rows = [row(0), row(3), row(4)]
    for item in rows:
        item["candidates"][0]["model_definition"]["holding_seconds"] = 5
    result = evaluate(rows, {"holding_seconds": 5})
    assert candidate(result)["open_paper_positions"] == 1
    assert candidate(result)["completed_trades"] == 0
    rows += [row(7), row(8, observed=7), row(8.1)]
    for item in rows:
        item["candidates"][0]["model_definition"]["holding_seconds"] = 5
    result = evaluate(rows, {"holding_seconds": 5})
    trade = candidate(result)["trades"][0]
    assert trade["exit_at"] == (NOW + timedelta(seconds=8.1)).isoformat()
    assert trade["status"] == "SIMULATED_TIME_LIMIT"


def test_actual_recording_delay_is_added_before_manual_execution_delay():
    slow = row(2, observed=0, generated=0)
    result = evaluate([slow, row(3), row(5), row(6, bid=0.62, ask=0.64)])
    trade = candidate(result)["trades"][0]
    assert trade["signal_at"] == NOW.isoformat()
    assert trade["available_at"] == (NOW + timedelta(seconds=2)).isoformat()
    assert trade["eligible_at"] == (NOW + timedelta(seconds=5)).isoformat()
    assert trade["entry_fills"][0]["at"] == trade["eligible_at"]


@pytest.mark.parametrize(
    "criteria",
    [
        {"holding_seconds": 30},
        {"quantity": 2},
        {"target": 0.06},
        {"stop": 0.12},
        {"manual_delay_seconds": 4},
        {"slippage_per_leg": 0.02},
    ],
)
def test_probabilities_for_different_execution_events_are_not_reused(criteria):
    rows = [row(0), row(3), row(4)]
    report = evaluate(rows, criteria)
    assert candidate(report)["opportunities"] == 0
    assert report["strategies"]["simple_always_up"]["simulated_trades"] == 1


def test_probability_with_missing_or_incompatible_fee_definition_is_withheld():
    for key, value in (("fee_type", "flat"), ("fee_multiplier", 2), ("target", None)):
        first = row(0)
        first["candidates"][0]["model_definition"][key] = value
        assert candidate(evaluate([first, row(3)]))["simulated_trades"] == 0


def test_regressing_book_receipt_cannot_execute_at_an_older_price():
    result = evaluate([row(0), row(3), row(4), row(5, observed=3, bid=0.62, ask=0.64)])
    assert result["coverage"]["excluded"]["OUT_OF_ORDER_BOOK_RECEIPT"] == 1
    assert candidate(result)["open_paper_positions"] == 1
    assert candidate(result)["completed_trades"] == 0


def test_live_and_synthetic_observations_cannot_be_pooled():
    data = row(3)
    data["mode"] = "REPLAY / SIMULATED"
    with pytest.raises(ValueError, match="separately"):
        evaluate([row(0), data])


def test_unknown_fee_or_contradictory_book_never_becomes_a_fill():
    bad = row(3)
    bad["market"]["fee_type"] = None
    result = evaluate([row(0), bad])
    assert result["coverage"]["excluded"]["UNKNOWN_FEES"] == 1
    assert candidate(result)["simulated_trades"] == 0
    conflict = row(4, bid=0.60, ask=0.62, observed=3)
    result = evaluate([row(0), row(3), conflict])
    assert result["coverage"]["accepted_book_records"] == 0
    assert result["coverage"]["excluded"]["CONTRADICTORY_BOOK_CONTRACT"] == 3


def test_unverified_or_future_cutoff_and_out_of_order_records_are_rejected():
    bad = row(0)
    bad["data_cutoff"] = (NOW + timedelta(seconds=1)).isoformat()
    result = evaluate([bad, row(3), row(2)])
    assert result["coverage"]["excluded"]["FUTURE_PREDICTION_DATA_CUTOFF"] == 1
    assert result["coverage"]["excluded"]["OUT_OF_ORDER_AVAILABILITY"] == 1
    assert candidate(result)["simulated_trades"] == 0
    missing = row(0)
    del missing["data_cutoff"]
    assert candidate(evaluate([missing, row(3)]))["simulated_trades"] == 0


def test_exposure_cost_limits_do_not_invest_unfilled_quantity():
    result = evaluate([row(0), row(3), row(4)], {"max_total_cost": 0.1})
    trade = candidate(result)["trades"][0]
    assert trade["quantity"] == pytest.approx(0.17)
    assert trade["entry_cost"] <= 0.1
    assert trade["entry_fees"] == cost_fee(0.51, 0.17, 1)
    result = evaluate([row(0), row(3), row(11), row(12)], {"opportunity_interval_seconds": 10})
    assert candidate(result)["simulated_trades"] == 1
    assert candidate(result)["blocked_opportunities"] == 1


def test_rollover_does_not_fabricate_close_of_old_paper_position():
    rows = [row(0), row(3), row(4, ticker="KXBTC15M-NEXT")]
    result = evaluate(rows)
    stats = candidate(result)
    assert stats["open_paper_positions"] == 1 and stats["completed_trades"] == 0
    assert stats["blocked_opportunities"] == 1


def test_recording_is_additive_timing_verified_and_modes_are_separate(tmp_path):
    path = tmp_path / "journal.db"
    state = {
        "mode": "PUBLIC MARKET DATA",
        "now": NOW.isoformat(),
        "generated_at": NOW.isoformat(),
        "market": row(0)["market"],
        "model": {"scalp_edges": {"up": row(0)["candidates"][0]}},
    }
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE original(value TEXT)")
        db.execute("INSERT INTO original VALUES ('keep')")
        create_schema(db)
        assert record(db, state, NOW)
        assert record(db, state, NOW)  # Idempotent unique timing key.
        state["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        assert not record(db, state, NOW)
        state.update(generated_at=NOW.isoformat(), mode="REPLAY / SIMULATED")
        assert record(db, state, NOW)
        assert db.execute("SELECT COUNT(*) FROM scalp_book_observations").fetchone()[0] == 2
    result = load_journal(path)
    assert len(result["records"]) == 1
    assert result["records"][0]["mode"] == "PUBLIC MARKET DATA"
    evaluate_journal(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM original").fetchone()[0] == "keep"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_legacy_journal_without_verified_record_times_is_reported_unavailable(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE predictions(payload TEXT)")
        db.execute("INSERT INTO predictions VALUES (?)", (json.dumps(row(0)),))
    result = evaluate_journal(path)
    assert result["status"] == "INSUFFICIENT_HISTORICAL_ORDER_BOOKS"
    assert result["journal_coverage"]["loaded_records"] == 0
    assert candidate(result)["simulated_trades"] == 0


def _legacy_schema(db):
    db.executescript(
        "CREATE TABLE predictions(id INTEGER PRIMARY KEY,mode TEXT,ticker TEXT,timestamp TEXT,payload TEXT);"
        "CREATE TABLE btc_engine_predictions(mode TEXT,ticker TEXT,timestamp TEXT,recorded_at TEXT);"
    )


def _legacy_record(db, item, *, engine_timestamp=None):
    definition = item["candidates"][0]["model_definition"]
    payload = {
        "market": item["market"],
        "prediction_engine": {"updated_at": item["generated_at"]},
        "model": {
            "scalp_edges": {"up": item["candidates"][0]},
            "execution_assumptions": {
                k: definition[k] for k in ("manual_delay_seconds", "slippage_per_leg")
            },
        },
        "plans": {
            "up": {
                "net_target": definition["target"],
                "net_stop": definition["stop"],
                "holding_seconds": definition["holding_seconds"],
            }
        },
        "stale": False,
    }
    db.execute(
        "INSERT INTO predictions(mode,ticker,timestamp,payload) VALUES (?,?,?,?)",
        (item["mode"], item["market"]["ticker"], item["timestamp"], json.dumps(payload)),
    )
    db.execute(
        "INSERT INTO btc_engine_predictions VALUES (?,?,?,?)",
        (
            item["mode"],
            item["market"]["ticker"],
            engine_timestamp or item["generated_at"],
            item["recorded_at"],
        ),
    )


def test_legacy_opt_in_requires_exact_independent_generation_and_recording(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        _legacy_schema(db)
        _legacy_record(db, row(0), engine_timestamp=row(1)["generated_at"])
    assert load_journal(path, include_verified_legacy=True)["records"] == []
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM btc_engine_predictions")
        db.execute(
            "INSERT INTO btc_engine_predictions VALUES (?,?,?,?)",
            ("PUBLIC MARKET DATA", "KXBTC15M-PAPER", row(0)["generated_at"], row(2)["recorded_at"]),
        )
    loaded = load_journal(path, include_verified_legacy=True)
    assert loaded["coverage"]["verified_legacy_joined_records"] == 1
    assert loaded["records"][0]["recorded_at"] == row(2)["recorded_at"]
    assert loaded["records"][0]["provenance"].startswith("Bounded legacy")
    # Legacy scanning remains explicitly opt-in even with a verified join.
    assert load_journal(path)["records"] == []


def test_combined_forward_and_legacy_records_keep_total_replay_limit(tmp_path):
    path = tmp_path / "combined.db"
    with sqlite3.connect(path) as db:
        _legacy_schema(db)
        create_schema(db)
        for i in range(3):
            _legacy_record(db, row(i))
            item = row(i + 3)
            db.execute(
                "INSERT INTO scalp_book_observations(mode,payload) VALUES (?,?)",
                ("PUBLIC MARKET DATA", json.dumps(item)),
            )
    loaded = load_journal(path, limit=3, include_verified_legacy=True)
    assert loaded["coverage"]["truncated"] is True
    assert len(loaded["records"]) == 3
    assert [v["recorded_at"] for v in loaded["records"]] == [
        row(i)["recorded_at"] for i in (3, 4, 5)
    ]


def test_official_payout_does_not_fabricate_a_target_hit_or_executable_exit():
    close = row(0)["market"]["closes_at"]
    official = {
        "type": "official_settlement",
        "ticker": "KXBTC15M-PAPER",
        "mode": "PUBLIC MARKET DATA",
        "origin": "official",
        "closes_at": close,
        "available_at": (NOW + timedelta(minutes=15)).isoformat(),
        "outcome": "up",
    }
    stats = candidate(evaluate([row(0), row(3), official]))
    trade = stats["trades"][0]
    assert stats["open_paper_positions"] == 0
    assert stats["unobserved_outcomes"] == 1
    assert stats["net_simulated_pnl"] == 0
    assert stats["target_before_stop_or_time_rate_observed_only"] is None
    assert trade["exit_fills"] == [] and trade["net_pnl"] is None
    assert trade["official_settlement"]["payout_per_contract"] == 1
    assert trade["observed_cashflow_net_pnl"] == pytest.approx(0.47)


def test_nonobject_json_is_reported_malformed_without_crashing_loader(tmp_path):
    path = tmp_path / "malformed.db"
    with sqlite3.connect(path) as db:
        create_schema(db)
        db.execute(
            "INSERT INTO scalp_book_observations(mode,payload) VALUES (?,?)",
            ("PUBLIC MARKET DATA", "null"),
        )
    loaded = load_journal(path)
    assert loaded["coverage"]["malformed_records"] == 1
    assert loaded["records"] == []
