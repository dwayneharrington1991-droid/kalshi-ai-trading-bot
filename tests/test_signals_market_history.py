from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from src.signals_display.monitor import Journal
from src.signals_display.server import Monitor, make_handler


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _insert_market(db, ticker, closes_at, *, probability=None, outcome=None):
    db.execute(
        "INSERT INTO btc_engine_contracts(mode,ticker,closes_at,revisions,latest_at) "
        "VALUES ('PUBLIC MARKET DATA',?,?,0,NULL)",
        (ticker, closes_at.isoformat() if closes_at else None),
    )
    if probability is not None:
        opened_at = closes_at - timedelta(minutes=15)
        generated_at = opened_at
        payload = {
            "ticker": ticker,
            "opens_at": opened_at.isoformat(),
            "generated_at": generated_at.isoformat(),
            "closes_at": closes_at.isoformat(),
            "up": probability,
            "model": "opening-baseline-v1",
        }
        db.execute(
            "INSERT INTO btc_engine_opening "
            "(mode,ticker,generated_at,cutoff,closes_at,up,status,payload) "
            "VALUES ('PUBLIC MARKET DATA',?,?,?,?,?,'RECORDED',?)",
            (
                ticker,
                generated_at.isoformat(),
                opened_at.isoformat(),
                closes_at.isoformat(),
                probability,
                json.dumps(payload),
            ),
        )
    if outcome is not None:
        observed_at = (closes_at or NOW - timedelta(minutes=1)) + timedelta(seconds=1)
        db.execute(
            "INSERT INTO forecast_outcomes(ticker,origin,observed_at,outcome) "
            "VALUES (?,'official',?,?)",
            (ticker, observed_at.isoformat(), outcome),
        )


def test_market_history_pages_preserve_openings_and_score_only_verified_pairs(tmp_path):
    journal = Journal(tmp_path / "market-history.db")
    with journal.connect() as db:
        _insert_market(
            db, "KXBTC15M-OLDER-UP", NOW - timedelta(hours=4), probability=0.8, outcome=1
        )
        _insert_market(
            db, "KXBTC15M-OLDER-DOWN", NOW - timedelta(hours=3), probability=0.2, outcome=0
        )
        _insert_market(
            db, "KXBTC15M-NEUTRAL", NOW - timedelta(hours=2), probability=0.5, outcome=0
        )
        _insert_market(
            db, "KXBTC15M-ACTIVE", NOW + timedelta(hours=1), probability=0.7
        )
        for suffix, result in (("UNRECONCILED-A", 1), ("UNRECONCILED-B", 0), ("UNRECONCILED-C", 1)):
            _insert_market(db, "KXBTC15M-" + suffix, None, outcome=result)

    first = journal.market_history_page(limit=2, now=NOW)
    second = journal.market_history_page(
        before_close=first["pagination"]["next_before_close"],
        before_ticker=first["pagination"]["next_before_ticker"],
        limit=2,
        now=NOW,
    )
    third = journal.market_history_page(
        before_close=second["pagination"]["next_before_close"],
        before_ticker=second["pagination"]["next_before_ticker"],
        limit=2,
        now=NOW,
    )
    fourth = journal.market_history_page(
        before_close="",
        before_ticker=third["pagination"]["next_before_ticker"],
        limit=2,
        now=NOW,
    )

    all_markets = first["markets"] + second["markets"] + third["markets"] + fourth["markets"]
    assert len(all_markets) == 7
    assert len({market["ticker"] for market in all_markets}) == 7
    assert first["markets"][0]["ticker"] == "KXBTC15M-ACTIVE"
    assert first["markets"][0]["status"] == "OPEN"
    assert first["markets"][1]["ticker"] == "KXBTC15M-NEUTRAL"
    assert first["markets"][1]["correct"] is None
    assert second["markets"][1]["opening_direction"] == "UP"
    assert second["markets"][1]["model_version"] == "opening-baseline-v1"
    assert second["markets"][1]["final_outcome"] == "UP"
    assert second["markets"][1]["correct"] is True
    assert third["pagination"]["has_more"] is True
    assert all(market["closing_at"] is None for market in third["markets"])
    assert len(fourth["markets"]) == 1

    summary = first["summary"]
    assert summary["observed_markets"] == 7
    assert summary["completed_markets"] == 6
    assert summary["opening_forecasts"] == 4
    assert summary["verified_outcomes"] == 6
    assert summary["verified_predictions"] == 3
    assert summary["correct_predictions"] == 2
    assert summary["incorrect_predictions"] == 0
    assert summary["neutral_predictions"] == 1
    assert summary["accuracy"] == 1


def test_market_history_http_route_is_bounded_and_validates_cursor(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "market-history-http.db"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/market-history?limit=1", timeout=2) as response:
            page = json.load(response)
        assert page["pagination"]["limit"] == 1
        for query in (
            "?limit=101",
            "?limit=1&before_ticker=KXBTC15M-CURSOR",
            "?limit=1&before_close=not-a-time&before_ticker=KXBTC15M-CURSOR",
        ):
            try:
                urlopen(base + "/api/market-history" + query, timeout=2)
            except HTTPError as error:
                assert error.code == 400
            else:
                raise AssertionError(f"invalid market history query accepted: {query}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
