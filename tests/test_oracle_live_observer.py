import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


PATH = Path(__file__).parents[1] / "deploy/oracle/observe_live_rollover.py"
spec = importlib.util.spec_from_file_location("observer", PATH)
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def sample(ticker, suffix, *, opening=False):
    return {
        "health": {
            "build_commit": "a" * 40,
            "signals_only": True,
            "workers": {"healthy": True},
        },
        "ready": {"status": "READY"},
        "state": {
            "market": {"ticker": ticker, "observed_at": "2026-11-05T00:00:" + suffix + "Z"},
            "reference": {"observed_at": "2026-11-05T00:00:" + suffix + "Z"},
            "prediction_engine": {
                "initial": {
                    "ticker": ticker,
                    "generated_at": "2026-11-05T00:00:" + suffix + "Z",
                } if opening else {},
            },
            "scalping_monitor": {"signal": "WAIT"},
            "decision_intelligence": {"execution_capability": False},
        },
        "chart": {"status": "READY", "latest_bar_start": int(suffix)},
    }


def test_observer_requires_real_rollover_and_new_immutable_opening():
    old = "KXBTC15M-OLD"
    new = "KXBTC15M-NEW"
    first_new = sample(new, "03", opening=True)
    second_new = sample(new, "04", opening=True)
    second_new["state"]["prediction_engine"]["initial"] = deepcopy(
        first_new["state"]["prediction_engine"]["initial"]
    )
    result = observer.summarize(
        [sample(old, "01"), sample(old, "02"), first_new, second_new],
        "a" * 40,
    )
    assert result["status"] == "PASS"
    assert result["contracts"] == [old, new]
    assert result["automated_trading"] == "DISABLED"
    assert result["opening_forecast_immutable"] is True


def test_observer_rejects_health_only_without_rollover():
    with pytest.raises(ValueError, match="no complete"):
        observer.summarize([sample("KXBTC15M-ONLY", "01")], "a" * 40)


def test_observer_rejects_any_execution_capability():
    rows = [
        sample("KXBTC15M-OLD", "01"),
        sample("KXBTC15M-NEW", "02", opening=True),
        sample("KXBTC15M-NEW", "03", opening=True),
    ]
    rows[-1]["state"]["prediction_engine"]["initial"] = deepcopy(
        rows[-2]["state"]["prediction_engine"]["initial"]
    )
    rows[-1]["state"]["decision_intelligence"]["execution_capability"] = True
    with pytest.raises(ValueError, match="execution boundary"):
        observer.summarize(rows, "a" * 40)


def test_observer_rejects_repainted_opening():
    rows = [
        sample("KXBTC15M-OLD", "01"),
        sample("KXBTC15M-NEW", "02", opening=True),
        sample("KXBTC15M-NEW", "03", opening=True),
    ]
    rows[-1]["state"]["prediction_engine"]["initial"]["generated_at"] = (
        rows[-2]["state"]["prediction_engine"]["initial"]["generated_at"]
    )
    rows[-1]["state"]["prediction_engine"]["initial"]["up"] = 0.99
    with pytest.raises(ValueError, match="changed"):
        observer.summarize(rows, "a" * 40)
