import ast
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.signals_display import opening_shadow as sh
from src.signals_display import opening_shadow_view as view

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _ledger(path, rows, outcomes=()):
    sh.write_ledger(path, rows)
    with sqlite3.connect(path) as led:
        for ticker, observed_at, outcome in outcomes:
            led.execute("INSERT INTO shadow_outcomes VALUES (?,?,?,?)", (ticker, observed_at, outcome, T0.isoformat()))


def _row(ticker, decision, generated=T0, scored=None, seconds=5.0, complete=1, probability=0.6, sha="v1"):
    scored = scored or (generated + timedelta(seconds=seconds))
    return {"ticker": ticker, "opens_at": generated.isoformat(), "generated_at": generated.isoformat(),
            "scored_at": scored.isoformat(), "seconds_after_open": seconds, "inputs_complete": complete,
            "probability": probability if complete else None, "decision": decision, "model_sha256": sha}


def test_no_ledger_reports_not_installed_and_claims_nothing(tmp_path):
    result = view.ledger_view(tmp_path / "missing.sqlite3", T0)
    assert result["status"] == "NOT INSTALLED" and result["forward"] is None and result["recent"] == []
    assert result["live_influence"] == "NONE"


def test_pending_settled_late_and_not_prospective_rows_are_never_mixed(tmp_path):
    rows = [
        _row("PENDING-1", "UP", generated=T0),
        _row("SETTLED-1", "UP", generated=T0 - timedelta(minutes=15)),
        _row("LATE-1", "DOWN", generated=T0 - timedelta(minutes=30), seconds=1200),
        _row("REPLAY-1", "UP", generated=T0 - timedelta(minutes=45), scored=T0 - timedelta(minutes=10)),
        _row("NOPRED-1", "NOT_SCORED", generated=T0 - timedelta(minutes=60), complete=0),
    ]
    path = tmp_path / "led.sqlite3"
    _ledger(path, rows, outcomes=[("SETTLED-1", (T0 - timedelta(minutes=1)).isoformat(), 1),
                                  ("REPLAY-1", (T0 - timedelta(minutes=20)).isoformat(), 1)])
    result = view.ledger_view(path, T0 + timedelta(minutes=2))
    by = {r["market"]: r for r in result["recent"]}
    assert by["PENDING-1"]["status"] == "PENDING" and by["PENDING-1"]["decision"] == "UP"
    assert by["SETTLED-1"]["status"] == "SETTLED" and by["SETTLED-1"]["captured_before_outcome"] is True
    assert by["LATE-1"]["status"] == "NOT COUNTED — late" and by["LATE-1"]["decision"] == "NO PREDICTION"
    assert by["REPLAY-1"]["status"] == "NOT COUNTED — not captured before outcome"
    assert by["REPLAY-1"]["captured_before_outcome"] is False and by["REPLAY-1"]["decision"] == "NO PREDICTION"
    assert by["NOPRED-1"]["status"] == "NO PREDICTION" and by["NOPRED-1"]["probability"] is None
    assert result["counts"] == {"settled": 1, "pending": 1, "no_prediction": 1, "not_counted": 2}


def test_no_prediction_is_never_shown_as_a_direction_or_probability(tmp_path):
    path = tmp_path / "led.sqlite3"
    _ledger(path, [_row("X", "NOT_SCORED", complete=0)])
    row = view.ledger_view(path, T0 + timedelta(minutes=1))["recent"][0]
    assert row["decision"] == "NO PREDICTION" and row["probability"] is None and row["status"] == "NO PREDICTION"


def test_forward_accuracy_requires_thirty_settled_markets_before_any_claim(tmp_path):
    path = tmp_path / "led.sqlite3"
    rows, outs = [], []
    for i in range(10):
        g = T0 - timedelta(hours=10) + timedelta(minutes=15 * i)
        rows.append(_row(f"M{i}", "UP", generated=g))
        outs.append((f"M{i}", (g + timedelta(minutes=16)).isoformat(), 1))
    _ledger(path, rows, outcomes=outs)
    result = view.ledger_view(path, T0)
    assert result["forward"]["status"] == "INSUFFICIENT SAMPLE" and result["forward"]["needed"] == 30
    assert result["forward"].get("accuracy") is None


def test_forward_metrics_are_computed_over_settled_prospective_markets(tmp_path):
    path = tmp_path / "led.sqlite3"
    rows, outs = [], []
    for i in range(40):
        g = T0 - timedelta(days=5) + timedelta(minutes=15 * i)
        decision = "UP" if i % 4 else "WAIT"           # 30 UP calls, 10 WAIT
        rows.append(_row(f"S{i}", decision, generated=g, probability=0.7 if decision == "UP" else 0.5))
        outs.append((f"S{i}", (g + timedelta(minutes=16)).isoformat(), 1 if i % 5 else 0))
    _ledger(path, rows, outcomes=outs)
    forward = view.ledger_view(path, T0)["forward"]
    assert forward["status"] == "OK" and forward["settled_markets"] == 40
    assert forward["directional_calls"] == 30
    assert forward["coverage"] == 0.75                       # 30 of 40 settled openings were directional calls
    expected_correct = sum(1 for i in range(40) if i % 4 and i % 5)
    assert forward["correct"] == expected_correct
    assert forward["accuracy"] == round(expected_correct / 30, 4)


def test_the_view_never_modifies_the_ledger(tmp_path):
    path = tmp_path / "led.sqlite3"
    _ledger(path, [_row("A", "UP")], outcomes=[("A", (T0 + timedelta(minutes=16)).isoformat(), 1)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    view.ledger_view(path, T0 + timedelta(minutes=20))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_the_view_module_imports_nothing_that_could_touch_live_behaviour():
    tree = ast.parse((ROOT / "src/signals_display/opening_shadow_view.py").read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        if isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    forbidden = ("learning", "lifecycle", "promotion", "guarded", "phone_alerts", "telegram", "prediction_engine",
                 "monitor", "server", "production", "settings", "clients", "strategies", "jobs")
    assert not any(any(word in name for word in forbidden) for name in names), names


def test_the_server_route_only_calls_the_read_only_view():
    source = (ROOT / "src/signals_display/server.py").read_text()
    start = source.index('elif route == "/api/shadow-opening":')
    block = source[start:source.index('elif route == "/api/learning-summary":', start)]
    assert "ledger_view(shadow_ledger_path(monitor.journal.path))" in block
    for forbidden in ("write", "execute", "insert", "place_order", "shadow_openings", "learning", "telegram"):
        assert forbidden not in block.lower(), forbidden


def test_the_ledger_path_is_derived_from_the_journal_and_needs_no_launch_change(tmp_path):
    journal = tmp_path / "logs" / "upgraded-live-8768.db"
    assert view.shadow_ledger_path(journal) == tmp_path / "logs" / "shadow" / "shadow_ledger.sqlite3"
