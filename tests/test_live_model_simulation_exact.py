"""The optimised scalp simulation must reproduce the original loop EXACTLY."""
import math
import random
import time

import pytest

from src.signals_display import live_model
from src.signals_display.model import fee


def reference(rng, paths, initial, sigma, manual_delay, horizon, qs, spreads, caps, slippage, multiplier,
              target, stop):
    """The original per-second loop from live_model._scalp, verbatim."""
    totals = {s: {"target": 0, "stop": 0, "timeout": 0, "no_entry": 0, "pnl": [],
                  "outcome_pnl": {"target": [], "stop": [], "timeout": []}} for s in caps}
    for _ in range(paths):
        entries = {s: None for s in caps}
        resolved = {s: False for s in caps}
        value = initial
        for second in range(manual_delay + horizon + 1):
            if second:
                value += rng.gauss(0, sigma)
            p = 1 / (1 + math.exp(-max(-30, min(30, value))))
            for side in caps:
                if resolved[side]:
                    continue
                fair = p if side == "up" else 1 - p
                if second < manual_delay:
                    continue
                if second == manual_delay:
                    ask = qs[side]["ask"] if second == 0 else min(1, fair + spreads[side])
                    buy = ask + slippage
                    if buy > caps[side] or buy >= 1:
                        totals[side]["no_entry"] += 1
                        totals[side]["pnl"].append(0.0)
                        resolved[side] = True
                        continue
                    entries[side] = buy
                raw_bid = qs[side]["bid"] if second == 0 else max(0, fair - spreads[side])
                bid = max(0, raw_bid - slippage)
                entry = entries[side]
                net = bid - entry - fee(entry, multiplier) - fee(bid, multiplier)
                elapsed = second - manual_delay
                outcome = None
                if elapsed >= horizon:
                    outcome = "timeout"
                elif net <= -stop:
                    outcome = "stop"
                elif net >= target:
                    outcome = "target"
                if outcome:
                    totals[side][outcome] += 1
                    totals[side]["pnl"].append(net)
                    totals[side]["outcome_pnl"][outcome].append(net)
                    resolved[side] = True
            if all(resolved.values()):
                break
    return totals


def cases():
    rng = random.Random(2026)
    for i in range(60):
        up_bid = round(rng.uniform(0.02, 0.95), 2)
        up_ask = min(0.99, round(up_bid + rng.choice([0.01, 0.02, 0.05]), 2))
        down_bid = round(max(0.0, 1 - up_ask - rng.choice([0, 0.01])), 2)
        down_ask = min(0.99, round(1 - up_bid + rng.choice([0, 0.01]), 2))
        qs = {"up": {"bid": up_bid, "ask": up_ask}, "down": {"bid": down_bid, "ask": down_ask}}
        slippage = rng.choice([0.0, 0.01, 0.02])
        caps = {s: qs[s]["ask"] + slippage for s in ("up", "down")}
        spreads = {s: (qs[s]["ask"] - qs[s]["bid"]) / 2 for s in ("up", "down")}
        mid = (up_bid + up_ask) / 2
        yield dict(paths=rng.choice([50, 200]), initial=math.log(mid / (1 - mid)),
                   sigma=rng.choice([0.001, 0.02, 0.08, 0.3]), delay=rng.choice([0, 1, 3, 10]),
                   horizon=rng.choice([5, 30, 60]), qs=qs, spreads=spreads, caps=caps, slippage=slippage,
                   multiplier=rng.choice([1.0, 0.5]), target=rng.choice([0.02, 0.05]),
                   stop=rng.choice([0.01, 0.03]), seed=i)
    for i in range(60):  # long-lived paths: tight quotes, low fees, wide exits
        mid = round(rng.uniform(0.2, 0.8), 3)
        qs = {"up": {"bid": mid - 0.002, "ask": mid + 0.002}, "down": {"bid": 1 - mid - 0.002, "ask": 1 - mid + 0.002}}
        slippage = rng.choice([0.0, 0.001])
        yield dict(paths=200, initial=math.log(mid / (1 - mid)), sigma=rng.choice([0.01, 0.05, 0.2]),
                   delay=rng.choice([0, 2, 5]), horizon=rng.choice([10, 60]), qs=qs,
                   spreads={s: 0.002 for s in ("up", "down")}, caps={s: qs[s]["ask"] + slippage for s in qs},
                   slippage=slippage, multiplier=rng.choice([0.0, 0.05]), target=rng.choice([0.02, 0.06]),
                   stop=rng.choice([0.05, 0.3]), seed=1000 + i)


@pytest.mark.parametrize("case", list(cases()))
def test_optimised_simulation_is_bit_identical(case):
    args = (case["paths"], case["initial"], case["sigma"], case["delay"], case["horizon"], case["qs"],
            case["spreads"], case["caps"], case["slippage"], case["multiplier"], case["target"], case["stop"])
    a, b = random.Random(case["seed"]), random.Random(case["seed"])
    assert live_model.simulate_paths(a, *args) == reference(b, *args)
    assert a.random() == b.random()  # identical number of draws consumed


def test_cases_cover_every_outcome():
    seen = {"target": 0, "stop": 0, "timeout": 0, "no_entry": 0}
    for c in cases():
        args = (c["paths"], c["initial"], c["sigma"], c["delay"], c["horizon"], c["qs"], c["spreads"],
                c["caps"], c["slippage"], c["multiplier"], c["target"], c["stop"])
        for side in live_model.simulate_paths(random.Random(c["seed"]), *args).values():
            for k in seen:
                seen[k] += side[k]
    assert all(v > 100 for v in seen.values()), seen


def test_optimised_simulation_is_faster():
    case = next(c for c in cases() if c["delay"] >= 1)
    args = (2000, case["initial"], 0.02, 3, 60, case["qs"], case["spreads"], case["caps"], 0.01, 1.0, 0.05, 0.03)
    t = time.perf_counter(); reference(random.Random(1), *args); old = time.perf_counter() - t
    t = time.perf_counter(); live_model.simulate_paths(random.Random(1), *args); new = time.perf_counter() - t
    assert new < old
