"""L13: opening records say how their stream inputs were computed, and training can keep one computation."""
from datetime import timedelta
from pathlib import Path

from src.signals_display import opening_models as om

ROOT = Path(__file__).resolve().parents[1]


def test_new_records_are_stamped_with_the_current_computation():
    src = (ROOT / "src/signals_display/prediction_engine.py").read_text(encoding="utf-8")
    assert f'"stream_computation": "{om.STREAM_COMPUTATION}"' in src


def test_unstamped_records_are_classified_by_the_stream_fix_time():
    before = om.STREAM_FIX_LIVE_AT - timedelta(minutes=6)
    after = om.STREAM_FIX_LIVE_AT + timedelta(minutes=9)
    assert om.stream_computation({}, before) == om.LEGACY_STREAM_COMPUTATION
    assert om.stream_computation({}, after) == om.STREAM_COMPUTATION
    assert om.stream_computation({"stream_computation": "full-window-v2"}, before) == "full-window-v2"


def test_dataset_filter_is_optional_and_exact(monkeypatch):
    rows = {"a": {"stream_computation": om.LEGACY_STREAM_COMPUTATION}, "b": {"stream_computation": om.STREAM_COMPUTATION}}
    monkeypatch.setattr(om, "_opening_rows", lambda db, asof: [("a",), ("b",)])
    monkeypatch.setattr(om, "_assess_opening", lambda raw, asof: (dict(rows[raw[0]], ticker=raw[0]), "ELIGIBLE"))
    assert [r["ticker"] for r in om.dataset(None)] == ["a", "b"]                       # unchanged default
    assert [r["ticker"] for r in om.dataset(None, stream_computation=om.STREAM_COMPUTATION)] == ["b"]
    assert [r["ticker"] for r in om.dataset(None, stream_computation=om.LEGACY_STREAM_COMPUTATION)] == ["a"]
