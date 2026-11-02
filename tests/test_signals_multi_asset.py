from datetime import timedelta

from src.signals_display.multi_asset import MarketStrip, asset_of, opening_forecast
from tests.test_signals_display import NOW


def market(asset="XRP", ticker="KXXRP15M-TEST"):
    return {
        "asset": asset, "ticker": ticker, "series": f"KX{asset}15M",
        "opens_at": NOW.isoformat(), "closes_at": (NOW + timedelta(minutes=15)).isoformat(),
        "strike": 2.5, "mapping_verified": True, "settlement_rules_verified": True,
    }


def history():
    cutoff = int(NOW.timestamp()) // 60 * 60
    return {
        "received_at": (NOW - timedelta(seconds=1)).isoformat(),
        "candles": [
            {"start": t, "close": 2 + (index % 17) * 0.001}
            for index, t in enumerate(range(cutoff - 86400, cutoff, 60))
        ],
    }


def test_asset_identity_is_explicit_or_unambiguous():
    assert asset_of(market()) == "XRP"
    assert asset_of({"ticker": "KXSILVER15M-TEST"}) == "SILVER"
    assert asset_of({"ticker": "KXBTCETH15M-AMBIGUOUS"}) is None


def test_independent_opening_forecast_requires_preopening_history_and_reference():
    result = opening_forecast(
        market(),
        {"price": 2.55, "observed_at": (NOW - timedelta(seconds=1)).isoformat(),
         "source": "independent XRP/USD proxy"},
        history(), NOW,
    )
    assert result["status"] == "EXPERIMENTAL"
    assert result["direction"] in {"UP", "DOWN", "UNCLEAR"}
    assert result["up"] + result["down"] == 1
    assert result["immutable"] is True
    late = {**history(), "received_at": (NOW + timedelta(seconds=1)).isoformat()}
    unavailable = opening_forecast(market(), {}, late, NOW + timedelta(seconds=1))
    assert unavailable["status"] == "UNAVAILABLE"
    assert unavailable["up"] is None
    future = opening_forecast(
        market(),
        {"price": 2.55, "observed_at": (NOW + timedelta(milliseconds=1)).isoformat()},
        history(), NOW,
    )
    assert future["status"] == "UNAVAILABLE"
    assert "fresh independent opening reference" in future["reason"]


def test_each_market_keeps_immutable_opening_during_live_updates():
    strip = MarketStrip()
    references = {"XRP": {"price": 2.55, "observed_at": (NOW-timedelta(seconds=1)).isoformat()}}
    first = strip.update([market()], references, {"XRP": history()}, NOW)[0]
    references["XRP"]["price"] = 1.0
    later = strip.update([market()], references, {"XRP": history()}, NOW+timedelta(minutes=5))[0]
    assert later["up"] == first["up"]
    assert later["generated_at"] == first["generated_at"]
    assert later["remaining_seconds"] == 600


def test_dashboard_has_compact_market_strip_without_order_controls():
    html = open("src/signals_display/dashboard.html").read()
    assert 'id="market-strip"' in html
    assert "function marketStripView" in html
    assert "BUY" not in html.split('id="market-strip"', 1)[1].split("</section>", 1)[0]


def test_public_discovery_uses_verified_series_and_asset_specific_proxy(monkeypatch):
    from src.signals_display.monitor import PublicFeed

    feed = PublicFeed(lambda *_args, **_kwargs: None, multi_venue=False)
    cutoff = int(NOW.timestamp()) // 60 * 60

    def read(url):
        if "/markets?" in url:
            return {"markets": [{
                "ticker": "KXXRP15M-TEST", "series_ticker": "KXXRP15M",
                "title": "XRP price", "open_time": NOW.isoformat(),
                "close_time": (NOW+timedelta(minutes=15)).isoformat(),
                "floor_strike": 2.5, "strike_type": "greater_or_equal",
                "rules_primary": "XRP is at least the threshold using the published average.",
            }], "cursor": None}
        if "/series/KXXRP15M" in url:
            return {"series": {"settlement_sources": [{"name": "Verified XRP reference"}]}}
        if "/prices/XRP-USD/spot" in url:
            return {"data": {"amount": "2.55"}}
        if "/products/XRP-USD/candles" in url:
            return {"candles": [
                {"start": str(t), "close": str(2+(i%17)*.001)}
                for i, t in enumerate(range(cutoff-86400, cutoff, 60))
            ]}
        raise AssertionError(url)

    monkeypatch.setattr(feed, "_read", read)
    result = feed._load_multi_assets(NOW)
    assert result["markets"][0]["asset"] == "XRP"
    assert result["markets"][0]["settlement_rules_verified"] is True
    assert result["references"]["XRP"]["price"] == 2.55
    assert len(result["histories"]["XRP"]["candles"]) == 1440


def test_metals_stay_unavailable_without_an_independent_reference():
    silver = market("SILVER", "KXSILVER15M-TEST")
    result = opening_forecast(silver, {}, {}, NOW)
    assert result["status"] == "UNAVAILABLE"
    assert result["up"] is None
    assert "unavailable" in result["reason"]


def test_multi_asset_worker_reports_failure_without_blocking_cached_state():
    from concurrent.futures import Future
    from src.signals_display.monitor import PublicFeed

    feed = PublicFeed(lambda *_args, **_kwargs: None, multi_venue=False)
    failed = Future()
    failed.set_exception(ValueError("provider response rejected"))
    feed.multi_asset_pool = object()
    feed.multi_asset_job = failed
    feed.multi_asset_started = NOW
    cached = feed.sampled_multi_assets(NOW)
    assert cached["markets"] == []
    assert cached["diagnostics"]["status"] == "ERROR"
    assert cached["diagnostics"]["error_type"] == "ValueError"
