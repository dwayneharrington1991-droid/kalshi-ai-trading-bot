from pathlib import Path

from src.learning.confidence_optimizer import ConfidenceOptimizer
from src.learning.pattern_memory import PatternMemory
from src.learning.performance_tracker import PerformanceTracker
from src.learning.safety_gate import LearningSafetyGate
from src.learning.strategy_ranker import StrategyRanker
from src.learning.trade_memory import TradeMemory, TradeMemoryRecord


def build_database(path: Path) -> None:
    memory = TradeMemory(str(path))

    for index in range(25):
        pnl = 0.10 if index < 18 else -0.06

        trade_id = memory.record_open(
            TradeMemoryRecord(
                market_id=f"TEST-{index}",
                strategy="safe_compounder",
                side="no",
                entry_price=0.75,
                quantity=1,
                confidence=0.85,
                edge=0.12,
                category="test",
                reason="Learning-system test",
            )
        )

        memory.record_close(
            trade_id,
            exit_price=0.75 + pnl,
            realized_pnl=pnl,
        )


def test_learning_modules(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    build_database(database)

    summary = PerformanceTracker(str(database)).overall_summary()
    assert summary.total_trades == 25
    assert summary.wins == 18
    assert summary.losses == 7

    ranking = StrategyRanker(
        str(database),
        minimum_sample_size=20,
    ).rank()
    assert ranking
    assert ranking[0]["strategy"] == "safe_compounder"

    confidence = ConfidenceOptimizer(
        str(database),
        minimum_samples=20,
    ).analyze()
    assert confidence
    assert confidence[0]["eligible_for_use"] is True

    patterns = PatternMemory(str(database)).analyze(
        minimum_samples=20
    )
    assert patterns

    paper_decision = LearningSafetyGate(
        minimum_closed_trades=20
    ).evaluate(
        closed_trades=25,
        strategy_multiplier=1.10,
        confidence_multiplier=1.10,
        paper_mode=True,
    )
    assert paper_decision["allowed"] is True
    assert paper_decision["multiplier"] <= 1.10

    live_decision = LearningSafetyGate(
        minimum_closed_trades=20
    ).evaluate(
        closed_trades=25,
        strategy_multiplier=1.10,
        confidence_multiplier=1.10,
        paper_mode=False,
    )
    assert live_decision["allowed"] is False
    assert live_decision["multiplier"] == 1.0
