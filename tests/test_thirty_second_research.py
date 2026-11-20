import random

from src.signals_display import thirty_second_research as tsr


def _walk(n, seed, drift=0.0):
    rng = random.Random(seed)
    price, bars = 100000.0, []
    for i in range(n):
        price *= 1 + drift * (1 if i % 2 == 0 else 1) + rng.gauss(0, 0.001)
        bars.append({"ts": i * 30, "close": price, "volume": 1.0 + rng.random()})
    return bars


def test_features_are_causal_future_bars_do_not_change_past_rows():
    bars = _walk(300, seed=1)
    base = tsr.features(bars)
    changed = [dict(b) for b in bars]
    for b in changed[200:]:
        b["close"] *= 1.5
        b["volume"] *= 3
    after = tsr.features(changed)
    assert base[:200] == after[:200]


def test_labels_use_the_next_bar_and_are_none_on_the_last():
    bars = [{"ts": 0, "close": 1.0, "volume": 1}, {"ts": 30, "close": 2.0, "volume": 1},
            {"ts": 60, "close": 1.5, "volume": 1}]
    assert tsr.labels(bars) == [1, 0, None]


def test_binomial_p_matches_known_values():
    assert abs(tsr.binomial_p_greater(5, 10) - 0.623046875) < 1e-9
    assert tsr.binomial_p_greater(10, 10) < 0.001


def test_ece_is_zero_for_perfectly_calibrated_bins():
    p = [0.0] * 50 + [1.0] * 50
    y = [0] * 50 + [1] * 50
    assert tsr.ece(p, y) == 0.0


def test_random_walk_is_not_reported_as_a_validated_edge():
    result = tsr.run(_walk(4000, seed=7))
    assert result["verdict"] == "NO_VALIDATED_EDGE"
    assert result["test_n"] >= tsr.MIN_TEST
    assert result["compute_seconds"] >= 0


def test_planted_momentum_is_found_but_not_credited_as_new_edge():
    # The planted effect (next return ~ 0.6 x last return) is exactly what the persistence baseline
    # already captures, so the harness must NOT validate it as an edge over that baseline.
    rng = random.Random(11)
    bars, price, prev = [], 100000.0, 0.0
    for i in range(6000):
        shock = rng.gauss(0, 0.001)
        ret = 0.6 * prev + shock
        price *= 1 + ret
        prev = ret
        bars.append({"ts": i * 30, "close": price, "volume": 1.0 + rng.random()})
    result = tsr.run(bars)
    assert result["test_accuracy"] > 0.6                 # the momentum is detected out of sample
    assert result["edge_over_baseline"] < tsr.MIN_EDGE   # but it is no better than persistence
    assert result["verdict"] == "NO_VALIDATED_EDGE"


def test_too_little_data_is_refused_not_guessed():
    result = tsr.run(_walk(200, seed=3))
    assert result["verdict"] == "NO_VALIDATED_EDGE"
    assert "insufficient" in result["reason"]
