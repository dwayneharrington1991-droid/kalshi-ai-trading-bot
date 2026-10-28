"""Integration checks for streaming, incremental history and read-only comparisons."""
from copy import deepcopy
from datetime import timedelta
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.signals_display.account_readonly import ReadOnlyKalshi, AccountReadError
from src.signals_display.alerts import MonitoringAlerts, risk_alerts
from src.signals_display.comparison import model_market
from src.signals_display.monitor import PublicFeed, analyze
from src.signals_display.streaming import StreamHub
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def test_candle_preload_then_only_missing_minutes_are_requested(monkeypatch):
    from src.signals_display import monitor as module

    clock = [NOW]
    calls = []

    def get(url, **_kwargs):
        query = parse_qs(urlsplit(url).query)
        left, right = int(query["start"][0]), int(query["end"][0])
        calls.append((left, right))
        bars = [
            dict(
                start=str(t), open="100000", high="100002", low="99999", close="100001", volume="1"
            )
            for t in range(left, right, 60)
        ]
        return httpx.Response(200, json={"candles": bars}, request=httpx.Request("GET", url))

    monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    feed = PublicFeed(get)
    first = feed.candles(clock[0])
    assert len(calls) == 5 and len(first["candles"]) == 1440
    old = deepcopy(first)
    clock[0] += timedelta(seconds=60)
    second = feed.candles(clock[0])
    assert len(calls) == 6 and calls[-1][1] - calls[-1][0] <= 180
    assert len(second["candles"]) == 1440 and second["refresh"]["requests"] == 1
    assert first == old  # No mutation of an earlier frozen history object.
    assert second["candles"][-1]["start"] != first["candles"][-1]["start"]


def test_provider_retry_after_prevents_extra_requests(monkeypatch):
    from src.signals_display import monitor as module

    clock, calls = [10.0], []

    def get(url, **_kwargs):
        calls.append(url)
        return httpx.Response(
            429, headers={"Retry-After": "10"}, json={}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    feed = PublicFeed(get)
    url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    with pytest.raises(httpx.HTTPStatusError):
        feed._read(url)
    with pytest.raises(ValueError, match="BACKOFF"):
        feed._read(url)
    assert len(calls) == 1
    clock[0] = 20.1
    with pytest.raises(httpx.HTTPStatusError):
        feed._read(url)
    assert len(calls) == 2


def test_stream_merge_preserves_receipts_and_never_refreshes_old_book():
    feed = PublicFeed(lambda *_args, **_kwargs: None)
    stream = StreamHub(autostart=False)
    feed.streams = stream
    message = {
        "channel": "ticker",
        "sequence_num": 0,
        "timestamp": NOW.isoformat(),
        "events": [
            {
                "type": "update",
                "tickers": [
                    {
                        "product_id": "BTC-USD",
                        "price": "100025",
                        "best_bid": "100024",
                        "best_ask": "100026",
                    }
                ],
            }
        ],
    }
    stream.process_coinbase(message, received_at=NOW, received_monotonic=10)
    original = frame()
    merged = feed.enriched(original, NOW + timedelta(milliseconds=500))
    assert merged["reference"]["price"] == 100025
    assert merged["reference"]["observed_at"] == NOW.isoformat()
    assert merged["markets"][0]["observed_at"] == original["markets"][0]["observed_at"]
    assert merged["stream_observations"] and stream.drain_observations() == []
    late = feed.enriched(original, NOW + timedelta(seconds=6))
    assert late["reference"] == original["reference"]
    assert analyze(late, NOW + timedelta(seconds=6))["stale"]
    assert original["reference"]["price"] == 100020
    stream.close()


def test_market_stream_handshake_is_fixed_get_and_permission_gated():
    signed = []
    client = ReadOnlyKalshi("MOCK_ONLY", lambda text: signed.append(text) or "MOCK_SIGNATURE")
    client.permissions = lambda: {"read_only_verified": False}
    with pytest.raises(AccountReadError, match="permissions"):
        client.websocket_headers()
    assert not signed
    client.permissions = lambda: {"read_only_verified": True}
    headers = client.websocket_headers()
    assert set(headers) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert signed == [headers["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2"]
    with pytest.raises(TypeError):
        client.websocket_headers("/trade-api/v2/portfolio/orders")
    for method in ("POST", "DELETE", "PATCH", "PUT"):
        with pytest.raises(AccountReadError):
            client.request(method, "/trade-api/v2/portfolio/orders")
    assert len(signed) == 1


def test_fresh_book_cannot_keep_stale_btc_forecast_live(tmp_path):
    from src.signals_display.monitor import Journal
    from src.signals_display.server import Monitor

    monitor = Monitor(None, Journal(tmp_path / "j.db"))
    state = analyze(frame(), NOW)
    state["reference"]["observed_at"] = (NOW - timedelta(seconds=6)).isoformat()
    state["prediction_engine"] = {
        "status": "EXPERIMENTAL",
        "up": 0.8,
        "down": 0.2,
        "initial": {"up": 0.6, "generated_at": NOW.isoformat()},
    }
    monitor.state = state
    result = monitor.snapshot()
    assert result["market_health"]["contract_quotes_fresh"]
    assert not result["market_health"]["reference_fresh"]
    assert result["stale"] and result["prediction_engine"]["up"] == 0.8
    assert result["prediction_engine"]["status"] == "STALE_LAST_KNOWN"
    assert result["trade_decision"]["action"] == "WAIT"
    assert result["prediction_engine"]["initial"]["up"] == 0.6


def test_positive_theoretical_value_never_becomes_validated_trade_edge():
    state = analyze(frame(), NOW)
    state["prediction_engine"] = {"status": "EXPERIMENTAL", "up": 0.8, "down": 0.2}
    result = model_market(state, slippage=0.01)
    assert result["status"] == "EDGE UNVERIFIED" and not result["actionable"]
    up = result["sides"]["up"]
    assert up["net_ev"] == pytest.approx(0.8 - 0.53 - up["entry_fee"])
    state["market"]["quotes"]["up"]["ask_size"] = 0
    assert model_market(state)["sides"]["up"]["net_ev"] is None
    state["stale"] = True
    assert model_market(state)["sides"]["down"]["model_probability"] is None


def test_advisory_alerts_deduplicate_and_risk_requires_explicit_limits():
    alerts = MonitoringAlerts()
    state = analyze(frame(), NOW)
    state["remaining_seconds"] = 59
    assert len(alerts.update(state)) == len(alerts.update(state)) == 1
    events = alerts.update(
        {**state, "stale": True, "now": (NOW + timedelta(seconds=1)).isoformat()}
    )
    assert len(events) == 2 and events[-1]["type"] == "DATA_OUTAGE"
    account = {
        "status": "READ_ONLY_CONNECTED",
        "holdings": [{"status": "OPEN", "ticker": "KXBTC15M-MOCK", "quantity": 10}],
    }
    assert risk_alerts(account, NOW) == []
    account["limits"] = {
        "max_account_quantity": 5,
        "max_per_contract_quantity": 3,
        "minimum_cash_reserve": 1,
    }
    events = risk_alerts(account, NOW)
    assert len(events) == 2 and len({e["id"] for e in events}) == 2
    assert account["holdings"][0]["quantity"] == 10
    account["status"] = "ACCOUNT_STALE"
    assert risk_alerts(account, NOW) == []


def test_scheduled_model_work_is_archive_only_and_not_repeated(tmp_path):
    from src.signals_display.monitor import Journal
    from src.signals_display.model_registry import scheduled_evaluation, ModelRegistry

    journal = Journal(tmp_path / "public.db")
    with journal.connect() as db:
        db.executemany(
            "INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
            [(f"KXBTC15M-{i}", "official", NOW.isoformat(), i % 2) for i in range(80)],
        )
    directory = tmp_path / "registry"
    report = scheduled_evaluation(journal.path, directory, NOW)
    assert report["status"] == "INSUFFICIENT_OFFICIAL_LABELS" and report["official_contracts"] == 80
    assert not report["active_model_changed"] and report["archived_version"] is None
    assert ModelRegistry(directory).state()["active_version"] is None
    assert (
        scheduled_evaluation(journal.path, directory, NOW + timedelta(hours=1))["status"]
        == "MODEL_UPDATE_NOT_DUE"
    )
    with journal.read() as db:
        assert db.execute("SELECT COUNT(*) FROM forecast_outcomes").fetchone()[0] == 80


def test_running_pipeline_processes_streams_between_rest_polls_and_rolls_over(
    tmp_path, monkeypatch
):
    """Real-time local fixture: 100ms inputs, 500ms calculations, 1s REST."""
    from datetime import datetime, timezone
    from http.server import ThreadingHTTPServer
    from statistics import median
    import signal
    import sys
    import threading
    from time import monotonic
    from urllib.request import ProxyHandler, build_opener
    from src.signals_display import server as module
    from tests.test_signals_prediction_engine import history

    started = datetime.now(timezone.utc)
    stop = threading.Event()
    opened = threading.Event()
    holder = []
    polls = []

    class FakeFeed(PublicFeed):
        def __init__(self):
            super().__init__(
                get=lambda *_args, **_kwargs: pytest.fail("External request"), multi_venue=False
            )
            self.streams = StreamHub(autostart=False)
            self.candle_cache = history(started)

            def pump():
                index = 0
                while not stop.is_set():
                    now = datetime.now(timezone.utc)
                    self.streams.process_coinbase(
                        {
                            "channel": "ticker",
                            "sequence_num": index,
                            "timestamp": now.isoformat(),
                            "events": [
                                {
                                    "tickers": [
                                        {
                                            "product_id": "BTC-USD",
                                            "price": str(100000 + index % 5),
                                            "best_bid": "99999",
                                            "best_ask": "100005",
                                        }
                                    ]
                                }
                            ],
                        },
                        received_at=now,
                    )
                    index += 1
                    stop.wait(0.1)

            threading.Thread(target=pump, daemon=True).start()

        def poll(self):
            now = datetime.now(timezone.utc)
            polls.append(now)
            rolled = (now - started).total_seconds() >= 2
            data = frame()
            data.update(
                clock=now.isoformat(),
                now=now.isoformat(),
                mode="PUBLIC MARKET DATA",
                evidence={},
                btc_candles=self.candle_cache,
            )
            market = data["markets"][0]
            opening = started + timedelta(seconds=2 if rolled else -898)
            market.update(
                ticker="KXBTC15M-NEW" if rolled else "KXBTC15M-OLD",
                opens_at=opening.isoformat(),
                closes_at=(opening + timedelta(minutes=15)).isoformat(),
                observed_at=now.isoformat(),
                mapping_verified=True,
                averaging_rule_verified=True,
                comparison_operator=">=",
            )
            return data

    def server_factory(*args, **kwargs):
        server = ThreadingHTTPServer(*args, **kwargs)
        holder.append(server)
        opened.set()
        return server

    monkeypatch.setattr(module, "PublicFeed", FakeFeed)
    monkeypatch.setattr(module, "ThreadingHTTPServer", server_factory)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dashboard",
            "--mode",
            "public",
            "--port",
            "0",
            "--journal",
            str(tmp_path / "j.db"),
            "--account-store",
            str(tmp_path / "a.db"),
            "--disable-scheduled-learning",
        ],
    )
    thread = threading.Thread(target=module.main, daemon=True)
    thread.start()
    assert opened.wait(3)
    server = holder[0]
    opener = build_opener(ProxyHandler({}))  # Only the ephemeral local fixture.
    observations, errors = [], []
    try:
        deadline = monotonic() + 4
        while monotonic() < deadline:
            try:
                with opener.open(
                    f"http://127.0.0.1:{server.server_port}/api/state?view=main", timeout=2
                ) as response:
                    state = json.load(response)
                version = state.get("runtime", {}).get("state_version")
                if version and (not observations or observations[-1][0] != version):
                    observations.append(
                        (
                            version,
                            state["runtime"]["published_at"],
                            (state.get("market") or {}).get("ticker"),
                        )
                    )
            except Exception as exc:
                errors.append(type(exc).__name__)
            stop.wait(0.05)
        intervals = [
            (datetime.fromisoformat(b[1]) - datetime.fromisoformat(a[1])).total_seconds()
            for a, b in zip(observations, observations[1:])
        ]
        assert not errors and len(observations) >= 5
        assert median(intervals) < 0.9  # Stream frames are processed between one-second REST calls.
        assert len(observations) > len(polls)
        assert {o[2] for o in observations} >= {"KXBTC15M-OLD", "KXBTC15M-NEW"}
    finally:
        stop.set()
        server.shutdown()
        thread.join(timeout=10)
    assert not thread.is_alive()
