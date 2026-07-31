from datetime import datetime, timezone

import pytest

from src.intelligence import TradeDecisionRecord, TradeIntelligenceStore


@pytest.mark.asyncio
async def test_signal_execution_exit_round_trip(tmp_path):
    db_path = str(tmp_path / "test.db")
    store = TradeIntelligenceStore(db_path)
    decision = TradeDecisionRecord(
        market_id="TEST-123",
        side="NO",
        strategy="test_strategy",
        signal_timestamp=datetime.now(timezone.utc),
        proposed_price=0.62,
        proposed_quantity=2,
        edge=0.08,
        confidence=0.75,
        agent_scores={"bull": 0.2, "bear": 0.8},
        live_mode=False,
    )

    decision_id = await store.record_decision(decision)
    assert decision_id > 0

    await store.mark_execution(
        decision_id,
        status="filled",
        order_id="order-1",
        executed_price=0.61,
        executed_quantity=2,
    )
    await store.mark_exit(
        decision_id,
        exit_price=0.72,
        realized_pnl=0.22,
        exit_reason="take_profit",
        outcome=True,
    )

    summary = await store.strategy_summary()
    assert summary[0]["strategy"] == "test_strategy"
    assert summary[0]["decisions"] == 1
    assert summary[0]["executed"] == 1
    assert summary[0]["wins"] == 1
    assert summary[0]["total_pnl"] == pytest.approx(0.22)
