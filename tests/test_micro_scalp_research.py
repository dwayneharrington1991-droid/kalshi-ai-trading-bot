"""Research harness: split hygiene, holdout ledger, candidate engine causality."""
import json
import math

import pytest

from src.micro_scalp.engine import Engine
from src.micro_scalp.research.candidates import BTC_DELAY, Candidate, ResearchEngine, btc_events, merge
from src.micro_scalp.research.protocol import HoldoutLedger, credible, splits

from tests.test_micro_scalp_engine import T0, book, market, predictable_markets


def _write_contracts(tmp_path, n):
    for k in range(n):
        opens = T0 + 900 * k
        m = {"ticker": f"KXBTC15M-R{k:03d}", "opens_at": opens, "closes_at": opens + 900, "result": "yes",
             "candles": [], "trades_compact": []}
        (tmp_path / f"KXBTC15M-R{k:03d}.json").write_text(json.dumps(m))


def test_splits_are_chronological_disjoint_and_complete(tmp_path):
    _write_contracts(tmp_path, 20)
    sp = splits(tmp_path)
    assert [len(sp[k]) for k in ("train", "validation", "test")] == [10, 5, 5]
    allt = sp["train"] + sp["validation"] + sp["test"]
    assert allt == sorted(allt) and len(set(allt)) == 20
    assert max(sp["train"]) < min(sp["validation"]) and max(sp["validation"]) < min(sp["test"])


def test_ledger_allows_one_test_per_family(tmp_path):
    led = HoldoutLedger(tmp_path / "ledger.jsonl")
    a, b = Candidate("flow", k=0.05), Candidate("flow", k=0.10)
    assert led.authorize("flow", a) is None
    led.record("flow", a, {"net_pnl": -1})
    assert led.authorize("flow", a)["result"] == {"net_pnl": -1}  # identical config: cached, not re-run
    with pytest.raises(PermissionError):
        led.authorize("flow", b)  # tweaked config on the same holdout: refused
    assert led.authorize("momentum_k", b) is None  # other families unaffected


def test_investigation_refuses_test_split():
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "src.micro_scalp.research.investigate", "--historical", "x", "--btc", "y",
                        "--out", "z", "--splits", "train,test"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "reserved" in r.stderr


def test_credibility_criteria():
    good = {"completed_trades": 150, "net_pnl": 40.0, "profit_factor": 1.5, "vs_no_trade": {"net_pnl_95ci": [5.0, 80.0]}}
    assert credible(good)[0]
    for bad in ({**good, "completed_trades": 60}, {**good, "net_pnl": -1}, {**good, "profit_factor": 1.1},
                {**good, "vs_no_trade": {"net_pnl_95ci": [-3.0, 80.0]}}):
        assert not credible(bad)[0]


def _btc_bars(start, n, drift=0.0, price=60000.0):
    out = []
    for i in range(n):
        price *= math.exp(drift + (0.0002 if i % 2 else -0.0002))
        out.append([int(start + i), price, 0.1, 0.05, 3])
    return out


def test_btc_bar_usable_only_after_second_closes():
    ev = list(btc_events([[int(T0), 60000.0, 1.0, 0.5, 4]]))
    assert ev[0]["at"] == T0 + 1 + BTC_DELAY


def test_baseline_engine_ignores_btc_events():
    events = predictable_markets(2)
    plain = Engine().run(events)
    mixed = Engine().run(merge(iter(events), btc_events(_btc_bars(T0 - 1200, 3000))))
    assert [t["net_pnl"] for t in plain.trades] == [t["net_pnl"] for t in mixed.trades]
    assert plain.decision_counts == mixed.decision_counts


def test_fair_value_tracks_btc_distance_from_opening_anchor():
    e = ResearchEngine(Candidate("btc_fair_gap", k=0.5))
    e.apply(market("KXBTC15M-F", T0))
    for ev in btc_events(_btc_bars(T0 - 1200, 1500, drift=0.0)):
        e.apply(ev)
    m = e.markets["KXBTC15M-F"]
    p_flat = e.p_fair(m, T0 + 300)
    assert p_flat is not None and 0.0 < p_flat < 1.0
    e2 = ResearchEngine(Candidate("btc_fair_gap", k=0.5))
    e2.apply(market("KXBTC15M-F", T0))
    for ev in btc_events(_btc_bars(T0 - 1200, 1200) + _btc_bars(T0, 300, drift=0.00005, price=60000.0)):
        e2.apply(ev)
    assert e2.p_fair(e2.markets["KXBTC15M-F"], T0 + 300) > 0.9  # BTC well above the opening anchor


def test_fair_value_never_uses_future_btc():
    e = ResearchEngine(Candidate("btc_fair_gap", k=0.5))
    e.apply(market("KXBTC15M-F", T0))
    for ev in btc_events(_btc_bars(T0 - 1200, 1500)):
        if ev["at"] <= T0 + 200:
            e.apply(ev)
    assert e.btc_price(T0 + 200) is not None
    assert e.btc_price(T0 + 210) is None  # last usable bar is >5 s old: no stale price used


def test_fair_value_hold_holds_to_settlement_and_pays_one_fee():
    events = []
    tk = "KXBTC15M-H"
    events.append(market(tk, T0))
    for s in range(900):
        events.append(book(T0 + s + 0.5, tk, 0.40, 0.41))  # market says 41c
    events.append({"kind": "settlement", "at": T0 + 1000, "ticker": tk, "outcome": "up"})
    bars = _btc_bars(T0 - 1200, 1200) + _btc_bars(T0, 900, drift=0.00004, price=60000.0)  # BTC rallies -> fair ~1
    e = ResearchEngine(Candidate("fair_value_hold", margin=0.05)).run(merge(iter(events), btc_events(bars)))
    assert len(e.trades) == 1
    t = e.trades[0]
    assert t["settled"] and t["side"] == "up" and t["exit_price"] == 1.0
    assert t["fees"] == pytest.approx(taker_fee_one(t), abs=0.011)


def taker_fee_one(trade):
    from src.micro_scalp.risk import taker_fee

    return taker_fee(trade["entry_price"], trade["contracts"])


def test_rule_families_pay_identical_costs_to_baseline():
    e = ResearchEngine(Candidate("mean_reversion", k=1.0)).run(predictable_markets(3))
    for t in e.trades:
        assert t["fees"] > 0
    assert e.ec == Engine().ec and e.rc == Engine().rc
