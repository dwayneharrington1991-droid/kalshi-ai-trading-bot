"""Offline synthetic fixtures only: no bot, clients, environment, or APIs."""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.signals_display.model import evaluate

NOW = datetime(2026, 10, 26, 12, 5, tzinfo=timezone.utc)


def snapshot():
    market = dict(
        series="KXBTC15M",
        status="open",
        ticker="KXBTC15M-MOCK",
        opens_at=(NOW - timedelta(minutes=5)).isoformat(),
        closes_at=(NOW + timedelta(minutes=10)).isoformat(),
        observed_at=NOW.isoformat(),
        strike=100000,
        settlement_reference="mock official index",
        up_definition="reference_at_settlement > strike",
        fee_type="quadratic",
        fee_multiplier=1,
        quotes={s: dict(bid=0.48, ask=0.52, bid_size=10, ask_size=10) for s in ("up", "down")},
    )
    paths = [
        dict(
            settlement_up=i < 18,
            quotes=[
                dict(seconds=0, up_bid=0.48, down_bid=0.48),
                dict(
                    seconds=30, up_bid=0.70 if i < 12 else 0.25, down_bid=0.70 if i >= 12 else 0.25
                ),
                dict(seconds=60, up_bid=0.70, down_bid=0.25),
            ],
        )
        for i in range(30)
    ]
    return dict(
        markets=[market],
        evidence=dict(
            ticker=market["ticker"],
            snapshot_at=market["observed_at"],
            strike=market["strike"],
            settlement_reference=market["settlement_reference"],
            method="synthetic conditional paths for unit tests, not live evidence",
            paths=paths,
        ),
    )


def test_settlement_and_separate_scalp_first_passage():
    result = evaluate(snapshot(), now=NOW, stop=0.15)
    assert result.status == "ESTIMATE"
    assert result.settlement_up == 0.6
    assert result.settlement_down == 0.4
    assert result.scalp_up == 0.4
    assert result.scalp_down == 0.6
    assert "UNVALIDATED" in result.label


def test_spread_and_two_leg_fees_can_stop_both_sides_immediately():
    result = evaluate(snapshot(), now=NOW)  # Initial net liquidation is -.08.
    assert result.scalp_up == result.scalp_down == 0


def test_target_before_stop_and_timeouts_are_not_wins():
    data = snapshot()
    for path in data["evidence"]["paths"]:
        path["quotes"][1].update(up_bid=0.7, down_bid=0.2)
        path["quotes"][2].update(up_bid=0.1, down_bid=0.7)
    result = evaluate(data, now=NOW, stop=0.15)
    assert result.scalp_up == 1
    assert result.scalp_down == 0
    assert evaluate(data, now=NOW, target=0.5, stop=0.15).scalp_up == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["markets"][0].update(observed_at=(NOW - timedelta(seconds=6)).isoformat()),
        lambda d: d["markets"][0].update(observed_at=(NOW + timedelta(seconds=1)).isoformat()),
        lambda d: d["markets"].append(deepcopy(d["markets"][0])),
        lambda d: d["markets"][0].update(status="closed"),
        lambda d: d["markets"][0].update(up_definition="unknown"),
        lambda d: d["evidence"].update(ticker="wrong"),
        lambda d: d["evidence"].update(paths=d["evidence"]["paths"][:29]),
        lambda d: d["markets"][0]["quotes"]["up"].update(ask=float("nan")),
        lambda d: d["markets"][0]["quotes"]["up"].update(ask_size=0),
        lambda d: d["evidence"]["paths"][0].update(settlement_up=None),
    ],
)
def test_insufficient_or_stale_evidence_fails_closed(change):
    data = snapshot()
    change(data)
    result = evaluate(data, now=NOW)
    assert result.status == "WAIT"
    assert result.settlement_up is result.scalp_up is result.scalp_down is None


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["markets"][0].update(fee_type="unknown"),
        lambda d: d["markets"][0].update(fee_multiplier=float("inf")),
        lambda d: d["evidence"]["paths"][0].update(quotes=[]),
        lambda d: d["evidence"]["paths"][0]["quotes"][0].update(up_bid=0.1),
        lambda d: d["evidence"]["paths"][0]["quotes"][1].update(seconds=0),
        lambda d: d["evidence"]["paths"][0]["quotes"][-1].update(seconds=61),
    ],
)
def test_scalp_missing_evidence_preserves_only_settlement(change):
    data = snapshot()
    change(data)
    result = evaluate(data, now=NOW)
    assert result.status == "WAIT"
    assert result.settlement_up == 0.6
    assert result.scalp_up is result.scalp_down is None


def test_time_limit_cannot_extend_after_settlement():
    result = evaluate(snapshot(), now=NOW, horizon=601)
    assert result.status == "WAIT" and result.scalp_up is None


def test_empty_display_shows_both_questions_and_unavailable():
    app = AppTest.from_file(Path("scripts/signals_display.py").resolve()).run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["unavailable"] * 4
    assert any(item.value == "WAIT" for item in app.subheader)


def test_display_dependency_boundary():
    for file in ("src/signals_display/model.py", "scripts/signals_display.py"):
        tree = ast.parse(Path(file).read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert all(
            not module or module.split(".")[0] in {"dataclasses", "datetime", "typing", "src"}
            for module in imports
        )
        assert not any(
            module and ("clients" in module or "jobs" in module or "strategies" in module)
            for module in imports
        )


def test_scalp_probabilities_are_not_complements():
    data = snapshot()
    for path in data["evidence"]["paths"]:
        path["quotes"][1].update(up_bid=0.7, down_bid=0.25)
        path["quotes"].insert(2, dict(seconds=45, up_bid=0.25, down_bid=0.7))
    result = evaluate(data, now=NOW, stop=0.4)
    assert result.scalp_up == result.scalp_down == 1


def test_target_only_at_deadline_is_not_before_time_limit():
    data = snapshot()
    for path in data["evidence"]["paths"]:
        path["quotes"][1].update(up_bid=0.48, down_bid=0.48)
    assert evaluate(data, now=NOW, stop=0.4).scalp_up == 0


def test_fee_rounding_changes_success_even_with_same_spread():
    data = snapshot()
    for path in data["evidence"]["paths"]:
        path["quotes"][1].update(up_bid=0.59, down_bid=0.40)
    assert evaluate(data, now=NOW, stop=0.4).scalp_up == 0
    data["markets"][0]["fee_multiplier"] = 0
    assert evaluate(data, now=NOW, stop=0.4).scalp_up == 1


def test_display_with_mock_upload_renders_both_estimates_and_controls():
    script = f"""
from datetime import datetime, timezone
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch
import scripts.signals_display as page
payload = {snapshot()!r}
with patch.object(page.st, "file_uploader", return_value=StringIO(json.dumps(payload))), patch.object(page, "datetime", SimpleNamespace(now=lambda tz: datetime(2026,10,26,12,5,tzinfo=timezone.utc))):
    page.main()
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["60.0%", "40.0%", "0.0%", "0.0%"]
    assert "UNVALIDATED" in app.warning[0].value
    assert [widget.value for widget in app.number_input] == [0.05, 0.03, 60]
