"""Micro-scalp paper engine: sizing, execution realism, look-ahead, accounting."""
import math
import random

import pytest

from src.micro_scalp.engine import Engine, StrategyConfig
from src.micro_scalp.events import ai_event, historical_market_events, record_to_event
from src.micro_scalp.execution import ExecutionConfig, PaperExchange
from src.micro_scalp.features import MarketState, features
from src.micro_scalp.metrics import cluster_bootstrap, summarize, trade_stats
from src.micro_scalp.recorder import extract_ai, parse_book
from src.micro_scalp.risk import RiskConfig, RiskManager, taker_fee

T0 = 1_800_000_000.0  # aligned to a 15-minute boundary


# --------------------------------------------------------------- helpers
def book(t, ticker, up_bid, up_ask, size=500.0):
    yes = [(round(up_bid - 0.01 * i, 2), size) for i in range(5)]
    no = [(round(1 - up_ask - 0.01 * i, 2), size) for i in range(5)]
    return record_to_event(
        {"type": "book", "received_at": t, "ticker": ticker, "yes_bids": yes, "no_bids": no}
    )


def market(ticker, opens):
    return {"kind": "market", "at": opens - 1, "ticker": ticker, "opens": opens, "closes": opens + 900, "strike": 1}


def predictable_markets(n=8, seed=1):
    """Synthetic contracts where taker flow predicts the next 30s move.

    Every 60s: 10s of one-sided taker flow, then the mid drifts 15c that way.
    """
    rng = random.Random(seed)
    events = []
    for k in range(n):
        tk, opens = f"KXBTC15M-SYN{k}", T0 + 900 * k
        events.append(market(tk, opens))
        mid = 0.5
        for s in range(0, 900):
            t = opens + s
            phase, pos = divmod(s, 60)
            direction = 1 if (phase + k) % 2 == 0 else -1
            if pos < 10:
                events.append({"kind": "trade", "at": t + 0.2, "exchange_time": t, "ticker": tk, "yes_price": round(mid, 2),
                               "count": 200.0, "taker_side": "yes" if direction > 0 else "no"})
            elif pos < 40:
                mid += direction * 0.15 / 30
            mid = min(0.85, max(0.15, mid + rng.uniform(-0.002, 0.002)))
            bid = round(mid - 0.005, 2)
            events.append(book(t + 0.5, tk, bid, round(bid + 0.01, 2)))
        events.append({"kind": "settlement", "at": opens + 1000, "ticker": tk, "outcome": "up" if mid > 0.5 else "down"})
    events.sort(key=lambda e: (e["at"], {"market": 0, "quote": 1, "trade": 2, "ai": 3, "settlement": 4}[e["kind"]]))
    return events


# ------------------------------------------------------------------ risk
def test_kalshi_taker_fee_formula():
    assert taker_fee(0.50, 100) == 1.75
    assert taker_fee(0.50, 1) == 0.02  # rounds up to the cent
    assert taker_fee(0.10, 10) == 0.07
    assert taker_fee(0.5, 0) == 0.0


def test_one_percent_worst_case_sizing():
    r = RiskManager(RiskConfig(max_total_exposure=0.5))
    qty, _ = r.size(1000, 0.50, None, 0.0)
    assert qty == math.floor(10 / (0.50 + 0.02)) == 19
    assert qty * (0.50 + taker_fee(0.5, 1)) <= 0.01 * 1000 + 1e-9


def test_size_respects_exposure_and_liquidity():
    r = RiskManager(RiskConfig(max_total_exposure=0.05, liquidity_fraction=0.25))
    assert r.size(1000, 0.5, None, exposure=49.0)[0] == 1  # $1 of room left
    assert r.size(1000, 0.5, None, exposure=50.0)[0] == 0
    assert r.size(1000, 0.5, displayed_qty=20, exposure=0)[0] == 5


def test_no_martingale_size_only_shrinks_after_losses():
    r = RiskManager(RiskConfig())
    before, _ = r.size(1000, 0.4, None, 0)
    for i in range(3):
        r.on_trade_closed(i, -5.0)
    same_equity, _ = r.size(1000, 0.4, None, 0)
    after_loss, _ = r.size(985, 0.4, None, 0)
    assert same_equity == before  # loss streak never increases size
    assert after_loss <= before


def test_daily_loss_halt_resets_next_day_and_kill_switch_is_permanent():
    r = RiskManager(RiskConfig(daily_loss_limit=0.03, max_drawdown_kill=0.10))
    r.update(T0, 1000)
    r.update(T0 + 60, 965)
    assert r.day_halted and not r.can_enter(T0 + 61)[0]
    r.update(T0 + 86400, 965)
    assert not r.day_halted and r.can_enter(T0 + 86401)[0]
    r.update(T0 + 86500, 890)
    assert r.killed
    r.update(T0 + 3 * 86400, 2000)
    assert r.killed and not r.can_enter(T0 + 3 * 86400)[0] and r.must_flatten()


def test_consecutive_loss_pause():
    r = RiskManager(RiskConfig(max_consecutive_losses=3, consecutive_loss_pause_seconds=600))
    for i in range(3):
        r.on_trade_closed(T0 + i, -1.0)
    assert not r.can_enter(T0 + 10)[0]
    assert r.can_enter(T0 + 700)[0]


def test_risk_config_refuses_excessive_risk():
    with pytest.raises(AssertionError):
        RiskConfig(risk_per_trade=0.2).validate()


# ------------------------------------------------------------- execution
def _order(ex, action="buy", side="up", qty=100, quote=0.50, t=T0):
    o = ex.make_order("KXBTC15M-A", side, action, qty, quote, t, "test")
    ex.submit(o)
    return o


def test_book_before_arrival_never_fills_and_fill_walks_depth():
    ex = PaperExchange(ExecutionConfig(latency_seconds=1.0, liquidity_fraction=0.5))
    o = _order(ex, qty=100, quote=0.50)  # limit 0.51
    early = book(T0 + 0.5, "KXBTC15M-A", 0.49, 0.50, size=100)
    assert ex.on_quote(early, True) == [] and o.filled == 0
    late = book(T0 + 1.2, "KXBTC15M-A", 0.49, 0.50, size=100)  # asks 0.50, 0.51, ...
    done = ex.on_quote(late, True)
    assert done == [o] and o.status == "FILLED"
    assert [f["price"] for f in o.fills] == [0.50, 0.51]  # 50 at each level (50% of 100)
    assert o.fees() == taker_fee(0.50, 50) + taker_fee(0.51, 50)


def test_partial_and_unfilled_orders():
    ex = PaperExchange(ExecutionConfig(latency_seconds=1.0, liquidity_fraction=0.25))
    o = _order(ex, qty=100, quote=0.50)
    ex.on_quote(book(T0 + 1.5, "KXBTC15M-A", 0.49, 0.50, size=40), True)
    assert o.status == "PARTIAL" and o.filled == 20  # 10 at 0.50 + 10 at 0.51
    o2 = _order(ex, qty=10, quote=0.50, t=T0 + 10)
    ex.on_quote(book(T0 + 11.5, "KXBTC15M-A", 0.55, 0.56), True)  # ran away above limit
    assert o2.status == "UNFILLED" and o2.filled == 0 and o2.fills == []


def test_order_expires_unfilled_without_evidence():
    ex = PaperExchange(ExecutionConfig(latency_seconds=1.0, fill_window_seconds=2.0))
    o = _order(ex)
    assert ex.expire(T0 + 2.9) == []
    assert ex.expire(T0 + 3.1) == [o] and o.status == "UNFILLED"


def test_print_mode_needs_matching_taker_side_after_arrival():
    ex = PaperExchange(ExecutionConfig(latency_seconds=1.0, liquidity_fraction=0.5, print_mode_extra_slippage=0.01))
    o = _order(ex, qty=30, quote=0.50)
    trade = lambda t, side, p, c=20.0: {"kind": "trade", "at": t + 0.2, "exchange_time": t, "ticker": "KXBTC15M-A",
                                        "yes_price": p, "count": c, "taker_side": side}
    ex.on_trade(trade(T0 + 0.5, "yes", 0.50), False)  # before arrival
    ex.on_trade(trade(T0 + 1.2, "no", 0.49), False)  # wrong side: hit the bid
    ex.on_trade(trade(T0 + 1.3, "yes", 0.53), False)  # above limit 0.51
    assert o.filled == 0
    p = trade(T0 + 1.4, "yes", 0.50)
    ex.on_trade(p, False)
    assert o.filled == 10 and o.fills[0]["price"] == 0.51  # evidence + 1c slippage
    o2 = _order(ex, qty=30, quote=0.50, t=T0 + 0.5)
    ex.on_trade(p, False)  # the same print cannot be used twice
    assert o2.filled == 0


def test_print_mode_down_side_mapping():
    ex = PaperExchange(ExecutionConfig(latency_seconds=0.0, liquidity_fraction=1.0, print_mode_extra_slippage=0.0))
    o = _order(ex, side="down", qty=5, quote=0.40)
    ex.on_trade({"kind": "trade", "at": T0 + 1, "exchange_time": T0 + 0.5, "ticker": "KXBTC15M-A",
                 "yes_price": 0.60, "count": 5.0, "taker_side": "no"}, False)
    assert o.status == "FILLED" and o.fills[0]["price"] == 0.40


# -------------------------------------------------------------- features
def test_stale_quotes_are_never_tradeable():
    m = MarketState("KXBTC15M-A", T0, T0 + 900)
    m.on_quote(book(T0 + 100, "KXBTC15M-A", 0.49, 0.50))
    assert features(m, T0 + 104, 5, 5, False)[0] is not None
    x, why = features(m, T0 + 106, 5, 5, False)
    assert x is None and "stale" in why


def test_ai_features_kept_separate_and_expire():
    m = MarketState("KXBTC15M-A", T0, T0 + 900)
    m.on_quote(book(T0 + 100, "KXBTC15M-A", 0.49, 0.51))
    m.on_ai({"live_up": 0.70, "opening_up": 0.40, "opening_ticker": "KXBTC15M-A", "at": T0 + 100})
    x, _ = features(m, T0 + 101, 5, 5, True)
    assert x[-4:] == [pytest.approx(0.20), 1.0, pytest.approx(-0.10), 1.0]
    x, _ = features(m, T0 + 104.9, 5, 3, True)
    assert x[-4:-2] == [0.0, 0.0] and x[-1] == 1.0  # live expired, opening persists


def test_ai_event_drops_future_or_stale_live_probability():
    good = {"ticker": "T", "stale": False, "live": {"up": 0.6, "updated_at": T0, "data_cutoff": T0}, "opening": {"up": 0.45, "generated_at": T0 - 600, "ticker": "T"}}
    e = ai_event(good, T0 + 0.1)
    assert e["live_up"] == 0.6 and e["opening_up"] == 0.45
    future = {**good, "live": {"up": 0.6, "updated_at": T0 + 5, "data_cutoff": T0 + 5}}
    assert ai_event(future, T0)["live_up"] is None
    assert ai_event({**good, "stale": True}, T0 + 1)["live_up"] is None


def test_recorder_keeps_opening_and_live_separate():
    state = {"generated_at": "x", "stale": False, "market": {"ticker": "T"},
             "prediction_engine": {"ticker": "T", "up": 0.7, "updated_at": "u", "data_cutoff": "c", "forecast_type": "LIVE_UPDATE",
                                   "initial": {"ticker": "T", "generated_at": "g", "data_cutoff": "o", "up": 0.48, "direction": "DOWN"}}}
    ai = extract_ai(state)
    assert ai["live"]["up"] == 0.7 and ai["opening"]["up"] == 0.48
    assert extract_ai({"prediction_engine": {}})["opening"] is None


def test_book_parsing_and_binary_mapping():
    parsed = parse_book({"orderbook_fp": {"yes_dollars": [["0.40", "10"], ["0.45", "5"]], "no_dollars": [["0.50", "7"]]}})
    assert parsed["yes_bids"][0] == (0.45, 5.0)
    e = record_to_event({"type": "book", "received_at": T0, "ticker": "T", **parsed})
    assert e["up_bid"] == 0.45 and e["up_ask"] == 0.50 and e["down_bid"] == 0.50 and e["down_ask"] == 0.55
    assert e["depth"]["up_asks"][0] == (0.50, 7.0)


def test_historical_conversion_skips_empty_book_and_delays_prints():
    m = {"ticker": "KXBTC15M-H", "opens_at": T0, "closes_at": T0 + 900, "result": "yes", "settlement_ts": None,
         "candles": [{"end_period_ts": T0 + 60, "yes_bid": {"close_dollars": "0.0010"}, "yes_ask": {"close_dollars": "1.0000"}},
                     {"end_period_ts": T0 + 120, "yes_bid": {"close_dollars": "0.55"}, "yes_ask": {"close_dollars": "0.56"}}],
         "trades_compact": [["2027-01-15T08:00:30+00:00", "0.55", "3", "yes"]]}
    m["trades_compact"][0][0] = T0 + 30
    ev = historical_market_events(m)
    quotes = [e for e in ev if e["kind"] == "quote"]
    assert len(quotes) == 1 and quotes[0]["at"] == T0 + 120 and quotes[0]["depth"] is None
    tr = [e for e in ev if e["kind"] == "trade"][0]
    assert tr["at"] > tr["exchange_time"] == T0 + 30
    st = [e for e in ev if e["kind"] == "settlement"][0]
    assert st["outcome"] == "up" and st["at"] >= T0 + 900


# ---------------------------------------------------------------- engine
def _engine(**kw):
    s = StrategyConfig(**{"min_train_samples": 300, **kw})
    return Engine(s, RiskConfig(starting_balance=1000))


def test_decision_never_sees_events_at_or_after_its_time():
    e = _engine()
    seen = []
    original = e._decide

    def spy(t, m, *a):
        if m is not None:
            seen.append((t, max(m.bid_at, m.ask_at)))
        return original(t, m, *a)

    e._decide = spy
    e.run(predictable_markets(1))
    assert seen and all(quote_time < t for t, quote_time in seen)


def test_labels_are_only_learned_after_the_horizon_passes():
    e = _engine(horizon_seconds=30)
    learned = []
    original_resolve = e._resolve_labels

    def spy(t):
        before = len(e.samples)
        first = [s[0] for s in list(e.samples)[: max(0, before)]]
        original_resolve(t)
        consumed = first[: before - len(e.samples)]
        learned.extend((t0, t) for t0 in consumed)

    e._resolve_labels = spy
    e.run(predictable_markets(2))
    assert learned and all(t >= t0 + 30 for t0, t in learned)


def test_engine_end_to_end_accounting_and_unfilled_orders():
    e = _engine(require_oos_skill=False)
    e.run(predictable_markets(8))
    s = summarize(e)
    assert s["paper_trading_only"] is True
    assert e.trades, "synthetic predictable data should produce paper trades"
    assert all(t["fees"] > 0 for t in e.trades)
    assert not e.positions
    assert e.cash == pytest.approx(1000 + sum(t["net_pnl"] for t in e.trades), abs=0.02)
    orders = e.exchange.done
    filled_buys = [o for o in orders if o.action == "buy" and o.filled > 0]
    assert len(filled_buys) == len(e.trades)  # unfilled orders never became trades
    assert all(o.fills[0]["t"] >= o.arrival for o in orders if o.fills)
    assert s["total_fees"] == pytest.approx(sum(t["fees"] for t in e.trades), abs=0.01)
    assert all(qty["contracts"] >= 1 for qty in e.trades)


def test_random_walk_with_skill_gate_does_not_force_trades():
    rng = random.Random(3)
    events = []
    for k in range(4):
        tk, opens = f"KXBTC15M-RW{k}", T0 + 900 * k
        events.append(market(tk, opens))
        mid = 0.5
        for s in range(900):
            mid = min(0.8, max(0.2, mid + rng.choice((-0.01, 0, 0.01))))
            bid = round(mid - 0.005, 2)
            events.append(book(opens + s + 0.5, tk, bid, round(bid + 0.01, 2)))
        events.append({"kind": "settlement", "at": opens + 1000, "ticker": tk, "outcome": "up"})
    e = _engine().run(events)
    # Pure noise: the out-of-sample gate should keep (almost) everything at WAIT.
    assert e.decision_counts["WAIT"] > 0.98 * sum(e.decision_counts.values())


def test_settlement_pays_one_or_zero_without_exit_fee():
    e = _engine()
    e.apply(market("KXBTC15M-S", T0))
    from src.micro_scalp.engine import Position

    e.positions["KXBTC15M-S"] = Position("KXBTC15M-S", "up", qty=10, cost=5.2, entry_fees=0.2, entry_notional=5.0, opened_at=T0)
    e.cash -= 5.2
    e.apply({"kind": "settlement", "at": T0 + 1000, "ticker": "KXBTC15M-S", "outcome": "up"})
    t = e.trades[-1]
    assert t["settled"] and t["exit_price"] == 1.0 and t["net_pnl"] == pytest.approx(4.8) and t["fees"] == pytest.approx(0.2)


def test_kill_switch_blocks_new_entries():
    e = _engine(require_oos_skill=False)
    e.risk.killed, e.risk.kill_reason = True, "test"
    e.run(predictable_markets(3))
    assert not any(a["action"].startswith("BUY") for a in e.actions)


# --------------------------------------------------------------- metrics
def test_metrics_profit_factor_and_bootstrap():
    trades = [{"ticker": f"T{i % 5}", "net_pnl": p, "gross_pnl": p + 0.1, "fees": 0.1, "exit_time": i}
              for i, p in enumerate([2, -1, 3, -1, -2, 4, 1, -1, 2, -1])]
    s = trade_stats(trades)
    assert s["profit_factor"] == pytest.approx(12 / 6)
    assert s["win_rate"] == 0.5
    b = cluster_bootstrap(trades)
    assert b["contracts"] == 5 and b["net_pnl_95ci"][0] <= 6 <= b["net_pnl_95ci"][1]


def test_skill_gate_needs_enough_scored_forecasts():
    e = _engine(min_oos_samples=1000)
    e.model.beta = [0.0] * e.model.k  # pretend fitted
    for _ in range(300):
        e.model.score(0.01, 0.01)  # perfect but small sample
    m = MarketState("KXBTC15M-G", T0, T0 + 900)
    m.on_quote(book(T0 + 100, "KXBTC15M-G", 0.49, 0.50))
    x, info = features(m, T0 + 101, 5, 5, False)
    d = e._decide(T0 + 101, m, x, info, 0.2, 1000.0, None)
    assert d["action"] == "WAIT" and "need ≥1000" in d["why"]


def test_duplicate_settlement_records_are_ignored(tmp_path):
    import json as _json

    from src.micro_scalp.events import recorded_events

    p = tmp_path / "r.jsonl"
    rows = [{"type": "settlement", "received_at": T0 + i, "ticker": "T", "result": "yes", "outcome": "up"} for i in range(3)]
    p.write_text("\n".join(_json.dumps(r) for r in rows))
    ev, info = recorded_events(p)
    assert len(ev) == 1 and info["record_counts"]["duplicate_settlement"] == 2


def test_recorder_writes_each_settlement_once(tmp_path):
    import json as _json

    from src.micro_scalp.recorder import Recorder

    class FakeClient:
        calls = 0

        def kalshi(self, path, params=None):
            FakeClient.calls += 1
            return {"market": {"result": "yes"}}

    out = tmp_path / "rec.jsonl"
    r = Recorder(out, None, client=FakeClient())
    r.markets["KXBTC15M-OLD"] = {"opens_at": "2020-01-01T00:00:00Z", "closes_at": "2020-01-01T00:15:00Z"}
    for _ in range(5):
        r.active()  # closed market -> queued for settlement only if not yet settled
        for k in r.pending_settlement:
            r.pending_settlement[k] = 0.0  # skip the 20s poll throttle
        r.poll_settlements()
    r.fh.close()
    rows = [_json.loads(line) for line in out.read_text().splitlines()]
    assert [x["type"] for x in rows] == ["settlement"]
    assert FakeClient.calls == 1 and "KXBTC15M-OLD" in r.settled


def test_gate_audit_counts_blocked_candidates_and_never_violates():
    e = _engine(min_oos_samples=10**9)  # gate can never open
    e.run(predictable_markets(4))
    assert e.gate_violations == 0
    assert not any(a["action"].startswith("BUY") for a in e.actions)
    assert e.gate_blocked_candidates > 0  # it really blocked would-be trades
    act = summarize(e)["activity"]
    assert act["active_market_hours"] == pytest.approx(1.0, abs=0.01)
    assert act["buy_signals_per_hour"] == 0


def test_momentum_baseline_trades_without_model_or_gate():
    e = _engine(policy="momentum", min_train_samples=10**9)
    e.run(predictable_markets(4))
    assert not e.model.ready()
    assert any(a["action"].startswith("BUY") for a in e.actions)
    assert all("Momentum baseline" in a["why"] for a in e.actions if a["action"].startswith("BUY"))


def test_ai_direct_policy_is_silent_without_ai_and_follows_ai_when_present():
    events = predictable_markets(2)
    silent = _engine(policy="ai_direct", min_expected_net_edge=-1.0, min_abs_predicted_move=0.01).run(events)
    assert not any(a["action"].startswith("BUY") for a in silent.actions)
    with_ai = list(events)
    for k in range(2):
        opens = T0 + 900 * k
        with_ai += [{"kind": "ai", "at": opens + s + 0.7, "ticker": f"KXBTC15M-SYN{k}", "live_up": 0.85,
                     "opening_up": None, "opening_ticker": None} for s in range(0, 900, 2)]
    with_ai.sort(key=lambda e: (e["at"], {"market": 0, "quote": 1, "trade": 2, "ai": 3, "settlement": 4}[e["kind"]]))
    follow = _engine(policy="ai_direct", min_expected_net_edge=-1.0, min_abs_predicted_move=0.01).run(with_ai)
    buys = [a for a in follow.actions if a["action"].startswith("BUY")]
    assert buys and all(a["action"] == "BUY UP" for a in buys)
