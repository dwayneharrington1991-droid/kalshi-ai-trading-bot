"""Past-only synthetic price/candle fixtures; no live access or order calls."""
from copy import deepcopy
from datetime import timedelta
import math
import sqlite3

import pytest

from src.signals_display.candles import candle_context
from src.signals_display.edges import assess_edges
from src.signals_display.evaluate_replay import walk_forward
from src.signals_display.live_model import LiveEstimator
from src.signals_display.model import timestamp, fee
from src.signals_display.monitor import Journal, active_market, analyze
from tests.test_signals_display import NOW
from tests.test_signals_live_model import live_frame, trained, estimate


def candles(now=NOW):
    minute = int(now.timestamp()) // 60 * 60
    bars = []
    for i in range(61):
        start = minute - (61 - i) * 60
        close = 100000 + 10 * math.sin(i * 0.4)
        bars.append(
            dict(
                start=str(start),
                open="100000",
                high="100020",
                low="99980",
                close=str(close),
                volume="2",
            )
        )
    return dict(
        source="Coinbase synthetic proxy candles", received_at=now.isoformat(), candles=bars
    )


def test_closed_candle_statistics_bootstrap_settlement_without_tick_warmup():
    payload = live_frame(0)
    payload["btc_candles"] = candles()
    model = LiveEstimator()
    result, info = estimate(model, payload)
    assert result.settlement_up is not None and result.scalp_up is None
    assert info["btc_context"]["closed_bars"] == 61
    assert info["btc_context"]["volume_5m_btc"] == 10
    assert info["volatility_method"].startswith("closed one-minute")
    assert "INSUFFICIENT_HISTORY" in info["scalp_reason"]


def test_future_and_in_progress_candle_prices_cannot_leak_into_predictions():
    payload = live_frame(0)
    payload["btc_candles"] = candles()
    first, info = estimate(LiveEstimator(), payload)
    future = deepcopy(payload)
    for seconds in (0, 60, 120):
        future["btc_candles"]["candles"].append(
            dict(
                start=str(int(NOW.timestamp()) + seconds),
                open="1",
                high="999999999",
                low=".1",
                close="99999999",
                volume="999999",
            )
        )
    second, more = estimate(LiveEstimator(), future)
    assert first.settlement_up == second.settlement_up
    assert info["btc_context"] == more["btc_context"]


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            lambda d: d.update(received_at=(NOW - timedelta(seconds=151)).isoformat()),
            "STALE_CANDLES",
        ),
        (lambda d: d["candles"].pop(20), "INCOMPLETE_CANDLES"),
        (lambda d: d["candles"][0].update(low="200000"), "INVALID_CANDLES"),
        (
            lambda d: d["candles"][0].update(start=str(int(d["candles"][0]["start"]) + 1)),
            "INCOMPATIBLE_CANDLES",
        ),
    ],
)
def test_bad_candle_inputs_fail_closed(change, reason):
    data = candles()
    change(data)
    stats, error = candle_context(data, NOW)
    assert not stats and reason in error


def test_high_probability_is_not_a_positive_edge_after_costs():
    model, payload = trained()
    result, info = estimate(model, payload, stop=0.01)
    market = payload["markets"][0]
    edges = assess_edges(market, result, info, timestamp(payload["now"]))
    assert edges["scalp_call"] == "WAIT"
    for side in ("up", "down"):
        edge = edges["scalp"][side]
        assert edge["net_expected_profit_per_opportunity"] <= 0
        assert (
            sum(edge[k] for k in ("target_count", "stop_count", "timeout_count", "no_entry_count"))
            == 2000
        )
        assert edge["edge_status"] == "edge unverified"
    assert edges["profitable_edge_demonstrated"] is False


def test_settlement_net_edge_uses_independent_probability_and_executable_ask():
    model, payload = trained()
    result, info = estimate(model, payload)
    market = payload["markets"][0]
    edges = assess_edges(market, result, info, timestamp(payload["now"]))
    up = edges["settlement"]["up"]
    cap = market["quotes"]["up"]["ask"] + 0.01
    assert up["net_edge_dollars_per_contract"] == pytest.approx(
        result.settlement_up - cap - fee(cap, 1)
    )
    assert up["qualification"] == "WAIT"  # Official index basis remains unmeasured.


def model_state(model, payload):
    now = timestamp(payload["now"])
    result, info = model.estimate(payload, active_market(payload, now), now, 0.05, 0.10, 10)
    return analyze(payload, now, 0.05, 0.10, 10, result, info)


def advanced(payload, seconds):
    data = deepcopy(payload)
    now = timestamp(payload["now"]) + timedelta(seconds=seconds)
    data["now"] = data["clock"] = now.isoformat()
    data["markets"][0]["observed_at"] = data["reference"]["observed_at"] = data["now"]
    return data


def test_delayed_counterfactual_entry_and_slipped_exit_are_not_real_fills(tmp_path):
    model, payload = trained()
    journal = Journal(tmp_path / "journal.db")
    journal.step(model_state(model, payload))
    first = journal.history()["simulations"]
    assert len(first) == 2 and all(
        r["status"] == "AWAITING_ENTRY" and r["entry"] is None for r in first
    )
    journal.step(model_state(model, advanced(payload, 2)))
    assert all(r["entry"] is None for r in journal.history()["simulations"])
    journal.step(model_state(model, advanced(payload, 4)))
    rows = {r["side"]: r for r in journal.history()["simulations"]}
    cap = payload["markets"][0]["quotes"]["up"]["ask"] + 0.01
    assert rows["up"]["entry"] == pytest.approx(cap)
    assert rows["up"]["execution_at"] == advanced(payload, 4)["now"]
    next_frame = advanced(payload, 6)
    next_frame["markets"][0]["quotes"]["up"].update(bid=0.90, ask=0.92)
    next_frame["markets"][0]["quotes"]["down"].update(bid=0.08, ask=0.10)
    journal.step(model_state(model, next_frame))
    rows = {r["side"]: r for r in journal.history()["simulations"]}
    assert rows["up"]["status"] == "SIMULATED_TARGET"
    assert rows["up"]["exit_bid"] == pytest.approx(0.89)
    assert rows["up"]["net_pnl"] == pytest.approx(0.89 - cap - fee(cap, 1) - fee(0.89, 1))
    assert all(r["real_fill"] == 0 for r in rows.values())
    stats = journal.performance()["research"]
    group = next(iter(stats["scalp_by_model_and_mode"].values()))
    assert group["observed_outcomes"] == 2 and group["target_hits"] == 1


def test_manual_delay_price_move_records_no_entry_not_fake_loss_or_win(tmp_path):
    model, payload = trained()
    journal = Journal(tmp_path / "journal.db")
    journal.step(model_state(model, payload))
    after = advanced(payload, 4)
    after["markets"][0]["quotes"]["up"].update(bid=0.88, ask=0.90)
    after["markets"][0]["quotes"]["down"].update(bid=0.10, ask=0.12)
    journal.step(model_state(model, after))
    row = next(r for r in journal.history()["simulations"] if r["side"] == "up")
    assert row["status"] == "SIMULATED_NO_ENTRY_PRICE_MOVED"
    assert row["entry"] is row["net_pnl"] is None


def test_walk_forward_predictions_do_not_change_when_future_prices_change(tmp_path):
    frames = [live_frame(i) for i in range(64)]
    first = walk_forward(frames, Journal(tmp_path / "first.db"), training_frames=61, horizon=5)
    changed = deepcopy(frames)
    changed[-1]["reference"]["price"] = 200000
    second = walk_forward(changed, Journal(tmp_path / "second.db"), training_frames=61, horizon=5)
    assert first["predictions"][:-1] == second["predictions"][:-1]
    assert len(first["predictions"]) == 3 and first["profitable_edge_demonstrated"] is False
    reversed_frames = [frames[1], frames[0]]
    with pytest.raises(ValueError, match="strictly increasing"):
        walk_forward(reversed_frames, Journal(tmp_path / "bad.db"), training_frames=1)


def test_evaluation_rejects_future_outcome_labels(tmp_path):
    frames = [live_frame(i) for i in range(63)]
    frames[-1]["settlements"] = {"KXBTC15M-MOCK": "yes"}
    with pytest.raises(ValueError, match="before contract close"):
        walk_forward(frames, Journal(tmp_path / "bad.db"), training_frames=61, horizon=5)


def test_prediction_is_recorded_before_outcome_update(tmp_path):
    frames = [live_frame(i) for i in range(61)]
    event = advanced(frames[-1], 480)
    event["settlements"] = {"KXBTC15M-MOCK": "yes"}
    frames.append(event)
    journal = Journal(tmp_path / "ordered.db")
    seen = []
    original = journal.settle

    def settle(ticker, outcome, mode):
        with journal.connect() as db:
            seen.append(db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0])
        original(ticker, outcome, mode)

    journal.settle = settle
    walk_forward(frames, journal, training_frames=59, horizon=5)
    assert seen and seen[0] > 0


def test_journal_migration_preserves_existing_rows_and_is_repeatable(tmp_path):
    path = tmp_path / "journal.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE simulations (id INTEGER PRIMARY KEY,prediction_id INTEGER,mode TEXT,ticker TEXT,
            side TEXT,started_at TEXT,last_at TEXT,deadline TEXT,entry REAL,entry_fee REAL,multiplier REAL,target REAL,stop REAL,
            last_bid REAL,status TEXT,exit_at TEXT,exit_bid REAL,exit_fee REAL,net_pnl REAL,
            real_fill INTEGER NOT NULL DEFAULT 0 CHECK(real_fill=0),probability REAL,UNIQUE(prediction_id,side))"""
        )
        db.execute(
            "INSERT INTO simulations (id,mode,ticker,status,real_fill) VALUES (1,'old','old','PENDING',0)"
        )
    for _ in range(2):
        Journal(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM simulations").fetchone()[0] == 1
        assert (
            db.execute("SELECT model_version FROM simulations").fetchone()[0]
            == "legacy-immediate-probe"
        )


def test_ninety_five_percent_target_score_cannot_override_negative_net_ev():
    model, payload = trained()
    result, info = estimate(model, payload)
    for side in ("up", "down"):
        info["scalp_edges"][side].update(
            target_probability_given_entry=0.95,
            net_expected_profit_per_opportunity=-0.01,
            net_ev_sampling_interval=[-0.012, -0.008],
            entered_paths=2000,
        )
    edges = assess_edges(payload["markets"][0], result, info, timestamp(payload["now"]))
    assert edges["scalp_call"] == "WAIT"
    assert all("non-positive" in d["reason"] for d in edges["scalp"].values())


def test_zero_manual_delay_still_applies_both_leg_slippage_and_model_version(tmp_path):
    _, payload = trained()
    model = LiveEstimator(manual_delay=0, slippage=0.01)
    # Tight stop makes the entry/exit cost charge observable immediately.
    model.restore(
        [
            {
                "mode": "PUBLIC MARKET DATA",
                "market": live_frame(i)["markets"][0],
                "reference": live_frame(i)["reference"],
                "now": live_frame(i)["now"],
            }
            for i in range(61)
        ]
    )
    now = timestamp(payload["now"])
    result, info = model.estimate(payload, active_market(payload, now), now, 0.05, 0.01, 10)
    state = analyze(payload, now, 0.05, 0.01, 10, result, info)
    journal = Journal(tmp_path / "zero.db")
    journal.step(state)
    up = next(r for r in journal.history()["simulations"] if r["side"] == "up")
    raw = payload["markets"][0]["quotes"]["up"]
    assert up["entry"] == pytest.approx(raw["ask"] + 0.01)
    assert up["exit_bid"] == pytest.approx(raw["bid"] - 0.01)
    assert up["net_pnl"] == pytest.approx(
        up["exit_bid"] - up["entry"] - up["entry_fee"] - up["exit_fee"]
    )
    assert up["model_version"] != "legacy-immediate-probe"
