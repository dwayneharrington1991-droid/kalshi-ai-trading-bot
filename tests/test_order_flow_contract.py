from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display.order_flow_contract import dashboard_payload, venue_payload
from src.signals_display.order_flow_verification import Trade


NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)


def frame():
    return {
        "source": "Coinbase", "symbol": "BTC-USD", "connected": True, "synced": True,
        "event_at": NOW - timedelta(milliseconds=50), "received_at": NOW,
        "last_change_at": NOW,
        "bids": [(99 - i, 10 if i == 0 else 1) for i in range(20)],
        "asks": [(101 + i, 1) for i in range(20)],
    }


def test_quiet_book_and_missing_trade_coverage_are_distinct():
    data = frame()
    assert venue_payload(data, NOW)["update_state"] == "UPDATED"
    quiet = venue_payload(data, NOW + timedelta(seconds=1))
    assert quiet["update_state"] == "UNCHANGED"
    assert quiet["depth"]["20"]["bid_btc"] == "29"
    assert quiet["executed_flow"]["status"] == "UNAVAILABLE"
    assert venue_payload(data, NOW + timedelta(seconds=3))["levels"]["bids"] == []
    assert venue_payload({**data, "bids": []}, NOW)["status"] == "EMPTY"
    assert venue_payload({**data, "synced": False, "event_at": None}, NOW)["status"] == "UNAVAILABLE"
    assert venue_payload({**data, "checksum_verified": False}, NOW)["status"] == "UNAVAILABLE"


def test_trade_windows_require_coverage_and_ignore_late_receipts():
    epoch = NOW.timestamp()
    data = {**frame(), "trade_coverage_start": NOW - timedelta(seconds=5), "trade_coverage_end": NOW,
            "trades": [Trade("x", epoch - .2, epoch, "buy", "0.5"),
                       Trade("late", epoch - .1, epoch + .1, "sell", "1")]}
    result = venue_payload(data, NOW)["executed_flow"]
    assert set(result["windows"]) == {"1s", "5s"}
    assert result["windows"]["1s"]["cvd_change_btc"] == "0.5"
    assert result["windows"]["1s"]["volume_imbalance"] == "1"


def test_cross_venue_disagreement_and_duplicate_source():
    one = frame()
    two = {**one, "source": "Kraken", "bids": [(99 - i, 1) for i in range(20)],
           "asks": [(101 + i, 10 if i == 0 else 1) for i in range(20)]}
    result = dashboard_payload([one, two], NOW, kalshi_contract={"unit": "contracts"})
    assert result["cross_exchange"]["depth_pressure"] == "DISAGREE"
    assert result["kalshi_contract"] == {"unit": "contracts"}
    with pytest.raises(ValueError, match="duplicate"):
        dashboard_payload([one, one], NOW)
