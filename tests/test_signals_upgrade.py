"""Offline safety, persistence, basis and past-only forecasting checks."""
from copy import deepcopy
from datetime import timedelta
import pytest
from src.signals_display.basis import estimate_basis
from src.signals_display.features import extract, fresh
from src.signals_display.learning import compare, preprocess
from src.signals_display.manual import ManualJournal
from src.signals_display.scanner import context, qualify
from src.signals_display.scores import score
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame

DEFAULTS = dict(target=0.05, stop=0.10, horizon=60, slippage=0.01)


def test_proxy_without_anchor_is_not_silently_official():
    p = frame()
    p["reference"].update(official=False, source="Proxy USD")
    basis, reason = estimate_basis(p, p["markets"][0], [], NOW)
    assert not basis and "MISSING_BASIS_ANCHOR" in reason


def test_only_matching_official_index_has_zero_basis():
    p = frame()
    m = p["markets"][0]
    p["reference"]["source"] = m["settlement_reference"]
    assert estimate_basis(p, m, [], NOW)[0]["point"] == 0
    p["reference"]["official"] = False
    assert estimate_basis(p, m, [], NOW)[1]


def test_future_candles_and_stale_receipts_do_not_enter_features():
    p = frame()
    p["btc_candles"] = {
        "received_at": NOW.isoformat(),
        "candles": [dict(start=int(NOW.timestamp()), open=1, high=2, low=1, close=2, volume=5)],
    }
    assert extract(p, p["markets"][0], NOW)["closed_candles"] == []
    p["btc_candles"]["received_at"] = (NOW - timedelta(seconds=151)).isoformat()
    assert "stale/future" in extract(p, p["markets"][0], NOW)["diagnostics"][0]


@pytest.mark.parametrize("age,expected", [(0, True), (15, True), (16, False), (-1, False)])
def test_context_timestamp_validation(age, expected):
    feed = {
        "status": "CONNECTED",
        "data": {"observed_at": (NOW - timedelta(seconds=age)).isoformat()},
    }
    assert fresh(feed, NOW, 15) is expected


def test_scanner_does_not_invent_twap_flow_or_liquidations_and_mutes_spread():
    p = frame()
    m = p["markets"][0]
    m["quotes"]["up"].update(bid=0.4, ask=0.44)
    scan = context(p, m, {}, NOW)
    assert all(
        scan[k] is None
        for k in (
            "official_60s_brti_twap",
            "liquidation_clusters",
            "aggressive_trade_flow_ratio",
            "confluence_probability",
        )
    )
    state = {"scalp": "SCALP UP", "market_comparison": {"model_minus_market": 0.15}}
    qualify(state, scan)
    assert state["scalp"] == "WAIT" and "3 cents" in state["scalp_explanation"]
    assert scan["experimental_probability_divergence"] == 0.15


def test_recent_public_release_mutes_but_missing_calendar_is_not_clean():
    p = frame()
    p["coverage"] = {
        "Federal Reserve public releases": {
            "status": "CONNECTED",
            "data": {"items": [{"published_at": NOW.isoformat()}]},
        }
    }
    scan = context(p, p["markets"][0], {}, NOW)
    assert scan["macro_blackout"] and "Incomplete" in scan["macro_calendar_status"]


def test_manual_restore_requires_fresh_quotes_and_never_auto_closes(tmp_path):
    path = tmp_path / "journal.db"
    j = ManualJournal(path)
    i = j.enter(
        dict(
            ticker="KXBTC15M-MOCK",
            side="up",
            price=0.4,
            quantity=2,
            fees=0.02,
            entered_at=NOW.isoformat(),
        ),
        NOW,
        DEFAULTS,
    )
    quote = frame()["markets"][0]
    quote["quotes"]["up"].update(bid=0.7, bid_size=5)
    g = j.guidance({"KXBTC15M-MOCK": quote}, NOW, {})[0]
    assert g["guidance"] == "EXIT — TARGET"
    repeated = j.guidance({"KXBTC15M-MOCK": quote}, NOW, {})[0]
    assert repeated["alert_id"] == g["alert_id"]
    restored = ManualJournal(path)
    assert restored.guidance({}, NOW, {})[0]["guidance"] == "DATA STALE — CHECK POSITION"
    assert restored.records()["entries"][0]["status"] == "OPEN"
    restored.exit(
        dict(entry_id=i, price=0.65, quantity=1, fees=0.01, exited_at=NOW.isoformat()), NOW
    )
    assert restored.records()["entries"][0]["remaining_quantity"] == 1
    restored.exit(
        dict(entry_id=i, price=0.65, quantity=1, fees=0.01, exited_at=NOW.isoformat()), NOW
    )
    assert restored.records()["entries"][0]["status"] == "CLOSED"


def test_insufficient_official_labels_never_produce_trained_model():
    artifact, report = compare([])
    assert artifact is None and not report["trained"] and report["edge_status"] == "edge unverified"


def test_training_preprocessing_excludes_odds_and_uses_training_only():
    rows = [{"features": {"return_1m": float(i), "kalshi_odds": 0.99}} for i in range(20)]
    prep = preprocess(rows)
    assert prep["names"] == ["return_1m"] and prep["means"] == [9.5]
    metrics = score([0.2, 0.8], [0, 1])
    assert metrics["brier"] == pytest.approx(0.04) and metrics["samples"] == 2


def test_chronological_training_never_fits_on_validation_or_test(monkeypatch):
    from src.signals_display import learning

    captured = []
    original = learning.fit

    def fit(rows, *args):
        captured.append({r["ticker"] for r in rows})
        return original(rows, *args)

    monkeypatch.setattr(learning, "fit", fit)
    rows = []
    for i in range(180):
        at = NOW + timedelta(minutes=15 * i)
        rows.append(
            dict(
                ticker=f"C{i:03}",
                timestamp=at.isoformat(),
                closes_at=(at + timedelta(minutes=10)).isoformat(),
                label_available_at=(at + timedelta(minutes=11)).isoformat(),
                features={"distance_scaled": (-1) ** i * 0.001, "return_1m": (-1) ** i * 0.0001},
                outcome=i % 2,
                baseline=0.5,
                market=0.5,
                remaining=600,
            )
        )
    artifact, report = learning.compare(rows)
    assert captured and all(s == {f"C{i:03}" for i in range(108)} for s in captured)
    assert report["split_contracts"] == [108, 36, 36]
    assert report["validation_end"] < report["test_first_prediction"]
    assert report["test"]["market_implied"]["samples"] == 36
    assert (
        report["validation_input_ablations"]["derivatives"]["status"] == "UNAVAILABLE_IN_TRAINING"
    )
    # Delayed labels at the boundary must be purged, not backdated into training.
    rows[107]["label_available_at"] = rows[150]["timestamp"]
    _, purged = learning.compare(rows)
    assert purged["split_contracts"][0] == 107
