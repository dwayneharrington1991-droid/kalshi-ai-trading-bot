"""All feeds mocked or replayed; never public APIs, credentials, or trading."""
from copy import deepcopy
from datetime import timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import httpx
import pytest

from src.signals_display import monitor as module
from src.signals_display.model import fee
from src.signals_display.monitor import PublicFeed, ReplayFeed, Journal, analyze, levels
from src.signals_display.server import Monitor, make_handler
from tests.test_signals_display import NOW, snapshot


def frame(seconds=0):
    data = snapshot()
    time = NOW + timedelta(seconds=seconds)
    data["now"] = time.isoformat()
    data["clock"] = data["now"]
    data["mode"] = "REPLAY / SIMULATED"
    data["markets"][0]["observed_at"] = data["now"]
    data["evidence"]["snapshot_at"] = data["now"]
    data["reference"] = dict(
        price=100020,
        source="Synthetic official test reference",
        observed_at=data["now"],
        official=True,
    )
    return data


def test_panels_have_distinct_decisions_and_complete_plan():
    data = frame()
    result = analyze(data, NOW, stop=0.15)
    assert result["direction"] == "UP"
    assert result["scalp"] == "DOWN SCALP"
    assert result["plans"]["down"]["entry"] == 0.52
    assert result["plans"]["down"]["holding_seconds"] == 60
    assert result["plans"]["down"]["target_bid"] == 0.61
    assert result["reference"]["price"] == 100020


def test_no_conditional_model_means_no_invented_probabilities():
    data = frame()
    data["evidence"] = {}
    state = analyze(data, NOW)
    assert state["direction"] == "UNCLEAR" and state["scalp"] == "WAIT"
    assert state["estimate"]["settlement_up"] is None
    assert state["estimate"]["scalp_up"] is None
    assert state["plans"]["up"]["entry"] == 0.52  # hypothetical, not an opportunity


def test_unknown_fees_or_mapping_prohibit_plans():
    data = frame()
    data["markets"][0]["fee_type"] = "unknown"
    assert analyze(data, NOW)["plans"] == {}
    data = frame()
    data["markets"][0]["mapping_verified"] = False
    assert analyze(data, NOW)["plans"] == {}


def test_stale_data_clears_all_decisions():
    state = analyze(frame(), NOW + timedelta(seconds=6))
    assert state["stale"] and state["direction"] == "UNCLEAR" and state["scalp"] == "WAIT"
    assert state["plans"] == {}


def test_next_contract_rollover():
    data = frame()
    next_market = deepcopy(data["markets"][0])
    next_market.update(
        ticker="KXBTC15M-NEXT",
        opens_at=data["markets"][0]["closes_at"],
        closes_at=(NOW + timedelta(minutes=25)).isoformat(),
    )
    data["markets"].append(next_market)
    assert analyze(data, NOW)["market"]["ticker"] == "KXBTC15M-MOCK"
    assert analyze(data, NOW + timedelta(minutes=10))["market"]["ticker"] == "KXBTC15M-NEXT"


@pytest.mark.parametrize("legacy", [False, True])
def test_public_adapter_only_gets_allowlisted_public_data(monkeypatch, legacy):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert "auth" not in kwargs
        assert kwargs["headers"] == {"Accept": "application/json"}
        assert not kwargs["follow_redirects"]
        if "/markets?" in url:
            payload = {
                "markets": [
                    dict(
                        ticker="KXBTC15M-MOCK",
                        strike_type="greater",
                        floor_strike=100000,
                        open_time=(NOW - timedelta(minutes=5)).isoformat(),
                        close_time=(NOW + timedelta(minutes=10)).isoformat(),
                        yes_bid_dollars="0.48",
                        yes_ask_dollars="0.52",
                        no_bid_dollars="0.48",
                        no_ask_dollars="0.52",
                    )
                ],
                "cursor": "",
            }
        elif url.endswith("/series/KXBTC15M"):
            payload = {
                "series": dict(
                    settlement_sources=[{"name": "Official test index"}],
                    fee_type="quadratic",
                    fee_multiplier=1,
                )
            }
        elif url.endswith("/orderbook"):
            payload = {
                "orderbook_fp": {
                    "yes_dollars": [["0.48", "12.00"]],
                    "no_dollars": [["0.48", "14.00"]],
                }
            }
            if legacy:
                payload = {"orderbook": {"yes": [[48, 12]], "no": [[48, 14]]}}
        elif url == module.COINBASE:
            payload = {"data": {"amount": "100020", "base": "BTC", "currency": "USD"}}
        else:
            raise AssertionError(url)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(module, "utcnow", lambda: NOW)
    data = PublicFeed(get).poll()
    assert len(calls) == 4
    assert data["markets"][0]["quotes"]["up"]["ask_size"] == 14
    assert not data["reference"]["official"]
    assert analyze(data, NOW)["scalp"] == "WAIT"


def test_public_pagination_ambiguity_fails_closed():
    response = httpx.Response(
        200, json={"markets": [], "cursor": "repeated"}, request=httpx.Request("GET", module.KALSHI)
    )
    with pytest.raises(ValueError, match="Repeated"):
        PublicFeed(lambda *a, **k: response).poll()


def test_counterfactual_journal_deduplicates_and_includes_spread_and_fees(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    first = analyze(frame(), NOW, stop=0.15)
    journal.step(first)
    journal.step(first)
    assert len(journal.history()["simulations"]) == 2
    second = frame(2)
    second["markets"][0]["quotes"]["up"].update(bid=0.70, ask=0.72)
    second["markets"][0]["quotes"]["down"].update(bid=0.25, ask=0.30)
    journal.step(analyze(second, NOW + timedelta(seconds=2), stop=0.15))
    rows = {row["side"]: row for row in journal.history()["simulations"]}
    assert rows["up"]["status"] == "SIMULATED_TARGET"
    assert rows["down"]["status"] == "SIMULATED_STOP"
    assert rows["up"]["net_pnl"] == pytest.approx(0.70 - 0.52 - fee(0.52, 1) - fee(0.70, 1))
    assert all(row["real_fill"] == 0 for row in rows.values())
    journal.settle("KXBTC15M-MOCK", "yes", "REPLAY / SIMULATED")
    assert all(p["settlement_up"] == 1 for p in journal.history()["predictions"])


@pytest.mark.parametrize(
    "seconds,expected", [(6, "UNOBSERVED_DATA_GAP"), (61, "UNOBSERVED_TIME_LIMIT")]
)
def test_gaps_are_never_targets_or_pnl(tmp_path, seconds, expected):
    journal = Journal(tmp_path / "journal.db")
    journal.step(analyze(frame(), NOW, stop=0.15))
    later = frame(seconds)
    later["markets"][0]["quotes"]["up"].update(bid=0.9, ask=0.91)
    journal.step(analyze(later, NOW + timedelta(seconds=seconds), stop=0.15))
    old = [r for r in journal.history()["simulations"] if r["started_at"] == NOW.isoformat()]
    assert old[0]["status"] == expected
    assert all(r["net_pnl"] is None for r in old)


def test_timeout_at_deadline_is_not_target(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    journal.step(analyze(frame(), NOW, stop=0.4, horizon=4))
    for sec in (2, 4):
        data = frame(sec)
        journal.step(analyze(data, NOW + timedelta(seconds=sec), stop=0.4, horizon=4))
    assert all(row["status"] == "SIMULATED_TIMEOUT" for row in journal.history()["simulations"])


def test_replay_clock_and_rollover(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps([frame(), frame(2)]))
    feed = ReplayFeed(path)
    assert feed.poll()["clock"] == NOW.isoformat()
    assert feed.poll()["clock"] == (NOW + timedelta(seconds=2)).isoformat()
    assert feed.poll()["clock"] >= (NOW + timedelta(seconds=2)).isoformat()


def test_failed_poll_clears_prior_signal(tmp_path):
    class Feed:
        def poll(self):
            raise RuntimeError("mock unavailable")

    monitor = Monitor(Feed(), Journal(tmp_path / "journal.db"))
    monitor.tick()
    state = monitor.snapshot()
    assert state["stale"] and state["scalp"] == "WAIT"
    assert state["estimate"]["settlement_up"] is None


def test_slow_poll_cannot_keep_previous_signal_fresh(tmp_path, monkeypatch):
    class Feed:
        def poll(self):
            return frame()

    monitor = Monitor(Feed(), Journal(tmp_path / "journal.db"))
    monitor.tick()
    monkeypatch.setattr(
        "src.signals_display.server.utcnow", lambda: monitor.last_tick + timedelta(seconds=6)
    )
    assert monitor.snapshot()["stale"]
    assert monitor.snapshot()["estimate"]["settlement_up"] is None


def test_read_only_browser_routes(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert b"OVERALL DIRECTION" in urlopen(base).read()
        assert json.load(urlopen(base + "/api/state"))["scalp"] == "WAIT"
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + "/api/state", method=method))
            assert error.value.code == 405
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/orders")
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_changed_fee_schedule_does_not_produce_false_wins(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    journal.step(analyze(frame(), NOW, stop=0.15))
    data = frame(2)
    data["markets"][0]["fee_multiplier"] = 2
    journal.step(analyze(data, NOW + timedelta(seconds=2), stop=0.15))
    assert all(
        row["status"] == "UNOBSERVED_FEE_CHANGE" and row["net_pnl"] is None
        for row in journal.history()["simulations"]
    )


def test_persisted_pending_probe_resumes_on_restart(tmp_path):
    path = tmp_path / "journal.db"
    Journal(path).step(analyze(frame(), NOW, stop=0.15))
    restarted = Journal(path)
    data = frame(2)
    data["markets"][0]["quotes"]["up"].update(bid=0.7, ask=0.75)
    restarted.step(analyze(data, NOW + timedelta(seconds=2), stop=0.15))
    assert any(row["status"] == "SIMULATED_TARGET" for row in restarted.history()["simulations"])


def test_write_access_is_absent_from_public_adapter_and_server():
    import ast

    for path in ("src/signals_display/monitor.py", "src/signals_display/server.py"):
        tree = ast.parse(Path(path).read_text())
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
            if isinstance(node, ast.Import):
                modules.extend(item.name for item in node.names)
        assert not any(
            any(
                word in name
                for word in ("clients", "strategies", "jobs", "dotenv", "settings", "os")
            )
            for name in modules
        )
    assert not any(
        name in PublicFeed.__dict__
        for name in ("post", "put", "delete", "place_order", "cancel_order")
    )


def test_levels_really_match_fee_adjusted_thresholds():
    for entry in (0.21, 0.525, 0.70):
        target_bid, stop_bid = levels(entry, 1, 0.05, 0.10)
        assert target_bid - entry - fee(entry, 1) - fee(target_bid, 1) >= 0.05 - 1e-9
        assert stop_bid - entry - fee(entry, 1) - fee(stop_bid, 1) <= -0.1 + 1e-9


def test_accuracy_goal_is_not_a_claim_and_repeated_polls_do_not_inflate_samples(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    for seconds in (0, 2, 4):
        journal.step(analyze(frame(seconds), NOW + timedelta(seconds=seconds), stop=0.15))
    assert journal.performance()["by_mode"]["REPLAY / SIMULATED"]["accuracy"] is None
    journal.settle("KXBTC15M-MOCK", "yes", "REPLAY / SIMULATED")
    report = journal.performance()
    assert report["accuracy_goal"] == 0.9 and not report["validated"]
    assert report["by_mode"]["REPLAY / SIMULATED"]["contracts"] == 1
    assert report["by_mode"]["REPLAY / SIMULATED"]["accuracy"] == 1
    assert report["by_mode"]["PUBLIC MARKET DATA"]["accuracy"] is None


def test_proxy_denial_explains_access_without_requesting_credentials(tmp_path):
    class Feed:
        def poll(self):
            raise httpx.ProxyError("403 Forbidden")

    monitor = Monitor(Feed(), Journal(tmp_path / "journal.db"))
    monitor.tick()
    state = monitor.snapshot()
    assert any("403" in message for message in state["warnings"])
    assert state["scalp"] == "WAIT"
