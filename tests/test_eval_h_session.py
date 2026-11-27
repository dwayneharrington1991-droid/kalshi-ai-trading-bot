"""H-session scorer: only post-start calls, WAITs ignored, needs enough calls and both tests."""
import importlib.util
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hs", ROOT / "scripts/eval_h_session.py")
hs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hs)


def test_before_start_and_waits_are_ignored():
    early = hs.START - timedelta(hours=1)
    rows = [(early.replace(hour=4), 0.7, 1), (hs.START.replace(hour=5), 0.5, 1)]
    r = hs.score(rows)
    assert r["window_calls"] == 0 and r["other_calls"] == 0 and r["decision"] == "NOT ENOUGH CALLS"


def test_supported_requires_enough_calls_and_a_clear_difference():
    base = hs.START.replace(hour=0) + timedelta(days=1)
    rows = []
    for i in range(150):
        rows.append((base.replace(hour=5) + timedelta(days=i), 0.6, 1 if i % 10 < 8 else 0))   # 80% in window
        rows.append((base.replace(hour=15) + timedelta(days=i), 0.6, 1 if i % 2 else 0))       # 50% elsewhere
    assert hs.score(rows)["decision"] == "SUPPORTED"
    assert hs.score(rows[:100])["decision"] == "NOT ENOUGH CALLS"
