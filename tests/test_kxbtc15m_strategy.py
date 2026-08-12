from datetime import datetime, timedelta, timezone
import time

import pytest

from src.strategies.kxbtc15m import (
    KXBTC15M_SERIES,
    KXBTC15MSettings,
    KXBTC15MSignalEngine,
    KXBTC15MExecutionAdapter,
    KXBTC15MDiscovery,
    ReconstructedOrderBook,
    ReferencePrice,
    contract_close_time,
    authoritative_target_price,
    authoritative_settlement_reference,
    parse_contract_metadata,
    discover_active_contract,
    position_action,
    seconds_to_expiration,
)


NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def market(ticker="KXBTC15M-TEST", *, minutes=15, status="open", series=KXBTC15M_SERIES):
    return {
        "ticker": ticker, "series_ticker": series, "status": status,
        "close_time": (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
    }


def fresh_book(sequence=10):
    book = ReconstructedOrderBook()
    assert book.apply_snapshot({"orderbook": {"yes": [[.40, 4]], "no": [[.30, 6]], "sequence": sequence}}, received_at=time.monotonic())
    return book


def test_only_open_kxbtc15m_contract_is_selected_and_rolls_over():
    selected = discover_active_contract([
        market("OTHER", minutes=1, series="KXOTHER"), market("CLOSED", minutes=1, status="closed"),
        market("LATER", minutes=30), market("NEXT", minutes=15),
    ], now=NOW)
    assert selected["ticker"] == "NEXT"
    rollover = discover_active_contract([market("NEXT", minutes=-1), market("AFTER", minutes=15)], now=NOW)
    assert rollover["ticker"] == "AFTER"


@pytest.mark.asyncio
async def test_paginated_discovery_is_series_only_and_rejects_repeated_cursor():
    class Client:
        async def get_markets(self, **kwargs):
            assert kwargs["series_ticker"] == KXBTC15M_SERIES
            return {"markets": [market("NEXT", minutes=15)], "cursor": None}
    assert (await KXBTC15MDiscovery(Client()).active_contract(now=NOW))["ticker"] == "NEXT"

    class RepeatingClient:
        async def get_markets(self, **kwargs):
            return {"markets": [market("NEXT", minutes=15)], "cursor": "again"}
    with pytest.raises(RuntimeError, match="repeated"):
        await KXBTC15MDiscovery(RepeatingClient()).active_contract(now=NOW)


def test_contract_clock_uses_authoritative_metadata_not_ticker_text():
    candidate = market("KXBTC15M-WHATEVER", minutes=7)
    assert contract_close_time(candidate) == NOW + timedelta(minutes=7)
    assert seconds_to_expiration(candidate, NOW) == pytest.approx(420)
    assert authoritative_target_price(candidate) is None
    assert authoritative_settlement_reference(candidate) is None
    candidate["strike_dollars"] = "100000.50"
    candidate["settlement_reference"] = "CF Benchmarks"
    assert authoritative_target_price(candidate) == 100000.5
    assert authoritative_settlement_reference(candidate) == "CF Benchmarks"


def test_contract_metadata_requires_documented_market_and_series_fields():
    candidate = market()
    candidate.update({
        "floor_strike": 100000, "cap_strike": 100000, "strike_type": "greater",
        "rules_primary": "The value uses the named source.", "rules_secondary": "At close time.",
    })
    series = {"settlement_sources": [{"name": "CF Benchmarks", "url": "https://example.invalid"}]}
    parsed = parse_contract_metadata(candidate, series)
    assert parsed is not None
    assert parsed.target_price == 100000
    assert parsed.yes_is_above_target
    assert parsed.settlement_source == "CF Benchmarks"
    assert parse_contract_metadata({**candidate, "cap_strike": 100001}, series) is None
    assert parse_contract_metadata(candidate, {"settlement_sources": []}) is None
    assert parse_contract_metadata({**candidate, "rules_primary": ""}, series) is None
    below = parse_contract_metadata({**candidate, "strike_type": "less_or_equal"}, series)
    assert below is not None and not below.yes_is_above_target


def test_snapshot_delta_sequence_and_staleness_fail_closed():
    book = fresh_book()
    assert book.apply_delta({"side": "no", "price": .30, "delta": 2, "sequence": 11}, received_at=10)
    assert book.executable_quantity("YES", .70) == pytest.approx(8)
    assert not book.apply_delta({"side": "no", "price": .30, "delta": 1, "sequence": 13}, received_at=11)
    assert not book.fresh(5, now=11)


def test_current_kalshi_websocket_fixed_point_schema_is_processed():
    book = ReconstructedOrderBook()
    assert book.apply_snapshot({"type": "orderbook_snapshot", "seq": 2, "msg": {
        "yes_dollars_fp": [["0.4000", "4.00"]], "no_dollars_fp": [["0.3000", "6.00"]],
    }}, received_at=1)
    assert book.apply_delta({"type": "orderbook_delta", "seq": 3, "msg": {
        "side": "no", "price_dollars": "0.3000", "delta_fp": "2.00",
    }}, received_at=2)
    assert book.executable_quantity("YES", .70) == pytest.approx(8)


def test_unified_yes_price_orderbook_does_not_invert_yes_liquidity():
    book = ReconstructedOrderBook()
    assert book.apply_snapshot({"seq": 1, "msg": {
        # With use_yes_price=true, a NO bid at 0.70 is an executable YES ask
        # at 0.70, not 0.30.
        "yes_dollars_fp": [["0.40", "2"]], "no_dollars_fp": [["0.70", "3"]],
    }}, received_at=1, unified_yes_price=True)
    assert book.executable_quantity("YES", .70) == pytest.approx(3)
    assert book.executable_quantity("YES", .69) == 0
    assert book.executable_quantity("NO", .60) == pytest.approx(2)


def test_yes_and_no_executable_liquidity_use_opposing_bids():
    book = fresh_book()
    # YES costs 1 - NO bid: 70 cents.  NO costs 1 - YES bid: 60 cents.
    assert book.executable_quantity("YES", .70) == pytest.approx(6)
    assert book.executable_quantity("YES", .69) == 0
    assert book.executable_quantity("NO", .60) == pytest.approx(4)


def _decision(*, reference=101.0, kalshi_reference=101.0, received_at=None, book=None, target=100.0, minutes=10, history=None):
    current = time.monotonic() if received_at is None else received_at
    return KXBTC15MSignalEngine(KXBTC15MSettings(min_confidence=.1, min_net_edge=.01)).evaluate(
        market=market(minutes=minutes), target_price=target,
        reference=ReferencePrice(reference, current, "compatible", kalshi_reference),
        price_history=history or [98, 98.5, 99, 99.5, 100, 100.5], book=book or fresh_book(),
        yes_bid=.60, yes_ask=.65, no_bid=.30, no_ask=.35, fee_rate=.01, slippage_rate=.005,
        now=time.monotonic(), wall_clock=NOW,
    )


def test_incompatible_external_reference_is_rejected():
    decision = _decision(reference=103, kalshi_reference=100)
    assert decision.action == "NO_TRADE"
    assert "incompatible" in decision.reason


def test_stale_book_and_near_expiration_are_blocked():
    stale = fresh_book()
    stale.received_at = time.monotonic() - 20
    assert "stale" in _decision(book=stale).reason
    assert "cutoff" in _decision(minutes=0.5).reason


def test_strong_up_and_down_logic_never_trades_on_weak_edge():
    up = _decision(reference=101.0, target=100.0)
    assert up.side == "YES"
    assert up.p_up > up.p_down
    down = _decision(reference=99.0, kalshi_reference=99.0, target=100.0, history=[102, 101.5, 101, 100.5, 100, 99.5])
    assert down.side == "NO"
    assert down.p_down > down.p_up
    assert _decision(reference=100.001, kalshi_reference=100.001, target=100.0).action == "NO_TRADE"


def test_add_hold_reduce_policy_is_conservative():
    decision = _decision(reference=101.0, target=100.0)
    assert position_action(existing_side=None, existing_quantity=0, decision=decision, entry_probability=None) == decision.action
    assert position_action(existing_side="YES", existing_quantity=1, decision=decision, entry_probability=.99) == "DO_NOT_ADD"
    opposing = _decision(reference=99.0, target=100.0)
    assert position_action(existing_side="YES", existing_quantity=1, decision=opposing, entry_probability=.5) in {"HOLD", "REDUCE_EXIT"}


def test_settings_default_to_disabled_execution():
    settings = KXBTC15MSettings()
    assert not settings.enabled
    assert not settings.live_execution_enabled


@pytest.mark.asyncio
async def test_execution_adapter_cannot_submit_until_both_dedicated_switches_are_enabled():
    class Service:
        async def execute(self, intent):  # pragma: no cover - must not be reached
            raise AssertionError("disabled adapter attempted a submission")

    metadata = parse_contract_metadata({**market(),
        "floor_strike": 100000, "cap_strike": 100000, "strike_type": "greater",
        "rules_primary": "Official rule.", "rules_secondary": "Official calculation.",
    }, {"settlement_sources": [{"name": "Official source"}]})
    assert metadata is not None
    decision = _decision(reference=101.0, target=100.0)
    with pytest.raises(RuntimeError, match="disabled"):
        await KXBTC15MExecutionAdapter(Service(), KXBTC15MSettings()).submit_if_allowed(
            metadata=metadata, decision=decision, quantity=1, limit_price=.65,
            position_id=1, environment="production",
        )
