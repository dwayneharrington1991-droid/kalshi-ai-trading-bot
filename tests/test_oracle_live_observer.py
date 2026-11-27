import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


PATH = Path(__file__).parents[1] / "deploy/oracle/observe_live_rollover.py"
spec = importlib.util.spec_from_file_location("observer", PATH)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
TARGET = "a" * 40
CUTOFF = 1762300800


def integrity(*, missing=(), duplicate=False):
    starts = [stamp for stamp in range(CUTOFF - 86400, CUTOFF, 60) if stamp not in missing]
    if duplicate:
        starts.append(starts[-1])
    return {
        "status": "READY" if not missing and not duplicate else "INCOMPLETE",
        "cutoff": CUTOFF, "interval_seconds": 60, "starts": starts,
        "bars": len(starts), "unique_bars": len(set(starts)),
        "duplicate_bars": len(starts) - len(set(starts)), "invalid_bars": 0,
        "missing_intervals": list(missing), "unexpected_intervals": [],
    }


def sample(ticker, index, *, opening=None, new=False):
    now = f"2026-11-05T00:{15 if new else 14}:{index:02d}+00:00"
    market = {
        "ticker": ticker, "observed_at": now,
        "opens_at": "2026-11-05T00:15:00+00:00" if new else "2026-11-05T00:00:00+00:00",
        "closes_at": "2026-11-05T00:30:00+00:00" if new else "2026-11-05T00:15:00+00:00",
    }
    return {
        "at": now,
        "health": {"status": "HEALTHY", "build_commit": TARGET, "signals_only": True, "workers": {"healthy": True}},
        "ready": {"status": "READY", "signals_only": True, "contract": ticker, "contract_quotes_fresh": True, "reference_fresh": True},
        "state": {
            "now": now, "stale": False, "market": market,
            "reference": {"observed_at": now},
            "prediction_engine": {"updated_at": now, "direction": "UP", "initial": opening or {}},
            "scalping_monitor": {"timestamp": now}, "scalp": "WAIT",
            "decision_intelligence": {"execution_capability": False},
            "storage_health": {"status": "NORMAL", "filesystem_free_bytes": 20 * 1024**3},
        },
        "chart": {"status": "READY", "bars": [{"start": CUTOFF - 60}], "latest_bar_start": CUTOFF + index * 60},
        "integrity": integrity(), "request_latency_ms": [1.0, 2.0],
    }


def valid_rows():
    old = "KXBTC15M-OLD"
    new = "KXBTC15M-NEW"
    opening = {
        "ticker": new, "generated_at": "2026-11-05T00:15:00+00:00",
        "data_cutoff": "2026-11-05T00:15:00+00:00",
        "input_data_cutoff": "2026-11-05T00:14:59+00:00", "direction": "WAIT",
        "up": None, "down": None, "status": "INSUFFICIENT DATA",
    }
    return [
        sample(old, 1), sample(old, 2), sample(new, 3, opening=deepcopy(opening), new=True),
        sample(new, 4, opening=deepcopy(opening), new=True),
    ]


def test_observer_requires_real_rollover_complete_candles_and_immutable_opening():
    result = observer.summarize(valid_rows(), TARGET)
    assert result["status"] == "PASS"
    assert result["contracts"] == ["KXBTC15M-OLD", "KXBTC15M-NEW"]
    assert result["completed_candle_rollovers"] == 3
    assert result["candle_integrity"]["bars"] == 1440
    assert result["opening_forecast_immutable"] is True
    assert result["automated_trading"] == "DISABLED"


def test_observer_rejects_missing_interval_with_exact_utc_reason():
    rows = valid_rows()
    missing = CUTOFF - 300
    rows[-1]["integrity"] = integrity(missing=(missing,))
    with pytest.raises(ValueError, match="missing UTC intervals=2025-11-04T23:55:00"):
        observer.summarize(rows, TARGET)


def test_observer_rejects_duplicate_future_or_out_of_order_candles():
    with pytest.raises(ValueError, match="duplicates=1"):
        observer.validate_candle_integrity(integrity(duplicate=True))
    future = integrity()
    future["starts"][-1] = CUTOFF
    future["unexpected_intervals"] = [CUTOFF]
    future["missing_intervals"] = [CUTOFF - 60]
    with pytest.raises(ValueError, match="future=1"):
        observer.validate_candle_integrity(future)
    out_of_order = integrity()
    out_of_order["starts"][-2:] = reversed(out_of_order["starts"][-2:])
    with pytest.raises(ValueError, match="ordered"):
        observer.validate_candle_integrity(out_of_order)


def test_observer_rejects_health_only_without_contract_rollover():
    rows = valid_rows()[:2]
    with pytest.raises(ValueError, match="no complete"):
        observer.summarize(rows, TARGET)


def test_observer_rejects_execution_stale_storage_and_repaint():
    rows = valid_rows()
    rows[-1]["state"]["decision_intelligence"]["execution_capability"] = True
    with pytest.raises(ValueError, match="execution boundary"):
        observer.summarize(rows, TARGET)
    rows = valid_rows()
    rows[-1]["ready"]["reference_fresh"] = False
    with pytest.raises(ValueError, match="stale"):
        observer.summarize(rows, TARGET)
    rows = valid_rows()
    rows[-1]["state"]["storage_health"]["filesystem_free_bytes"] = 5 * 1024**3
    with pytest.raises(ValueError, match="10 GiB"):
        observer.summarize(rows, TARGET)
    rows = valid_rows()
    rows[-1]["state"]["prediction_engine"]["initial"]["up"] = 0.99
    with pytest.raises(ValueError, match="changed"):
        observer.summarize(rows, TARGET)


def test_observer_rejects_post_open_cutoff_and_nonadvancing_live_or_scalp():
    rows = valid_rows()
    for row in rows[2:]:
        row["state"]["prediction_engine"]["initial"]["input_data_cutoff"] = "2026-11-05T00:15:00+00:00"
    with pytest.raises(ValueError, match="strictly before"):
        observer.summarize(rows, TARGET)
    rows = valid_rows()
    for row in rows[2:]:
        row["state"]["prediction_engine"]["updated_at"] = "same"
        row["state"]["scalping_monitor"]["timestamp"] = "same"
    with pytest.raises(ValueError, match="LIVE"):
        observer.summarize(rows, TARGET)
