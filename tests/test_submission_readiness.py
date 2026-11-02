from datetime import datetime
from decimal import Decimal
import json
import sqlite3

import pytest

from src.orders.execution_service import (
    ExecutionSafetyConfig, ExecutionSafetyError, OrderIntent, VerifiedExecutionService,
)
from src.orders.readiness import (
    CheckStatus, PRODUCTION_ACKNOWLEDGEMENT, ReadinessContext,
    SubmissionReadinessEvaluator, sufficient_order_balance, valid_order_price,
)
from src.orders.reconciler import OrderReconciler
from src.orders.repository import OrderRepository
from src.utils.database import DatabaseManager, Position
from src.strategies.unified_trading_system import UnifiedAdvancedTradingSystem


pytestmark = pytest.mark.asyncio


class NoNetworkClient:
    environment = "demo"

    async def place_order(self, **kwargs):
        raise AssertionError("network submission must not occur")

    async def get_balance(self):
        raise AssertionError("balance call must not occur for failed preflight")


@pytest.fixture
async def repository(tmp_path):
    manager = DatabaseManager(str(tmp_path / "readiness.db"))
    await manager.initialize()
    return OrderRepository(manager.db_path)


def context(**overrides):
    values = dict(
        live_mode=True,
        authoritative_execution_enabled=True,
        reconciliation_enabled=True,
        reconciliation_shadow_mode=True,
        kill_switch=False,
        configured_environment="demo",
        client_environment="demo",
        reconciliation_max_age_seconds=60,
    )
    values.update(overrides)
    return ReadinessContext(**values)


def check(report, name):
    return next(item for item in report.checks if item.name == name)


async def make_healthy(repository):
    run_id = await repository.start_reconciliation_run("test")
    await repository.finish_reconciliation_run(run_id, "completed")


async def test_paper_mode_is_not_a_live_submission_failure(repository):
    report = await SubmissionReadinessEvaluator(repository).evaluate(
        context(live_mode=False), scope="startup"
    )
    assert report.overall == "PAPER"
    assert check(report, "Authoritative execution").status == CheckStatus.NOT_APPLICABLE
    assert "NOT_APPLICABLE Live submission" in report.format_summary()


async def test_live_defaults_are_blocked_without_network(repository):
    report = await SubmissionReadinessEvaluator(repository).evaluate(context(
        authoritative_execution_enabled=False,
        reconciliation_enabled=False,
        kill_switch=True,
    ))
    assert report.overall == "BLOCKED"
    assert check(report, "Authoritative execution").status == CheckStatus.DISABLED
    assert check(report, "Order reconciliation").status == CheckStatus.DISABLED
    assert check(report, "Kill switch").status == CheckStatus.FAIL


async def test_fully_ready_demo_configuration(repository):
    await make_healthy(repository)
    report = await SubmissionReadinessEvaluator(repository).evaluate(context())
    assert report.overall == "READY"
    assert not report.blocking_checks


@pytest.mark.parametrize("overrides,name", [
    ({"kill_switch": True}, "Kill switch"),
    ({"reconciliation_enabled": False}, "Order reconciliation"),
])
async def test_individual_configuration_blockers(repository, overrides, name):
    await make_healthy(repository)
    report = await SubmissionReadinessEvaluator(repository).evaluate(context(**overrides))
    assert check(report, name).status in {CheckStatus.FAIL, CheckStatus.DISABLED}
    assert report.overall == "BLOCKED"


async def test_stale_reconciliation_and_critical_alert(repository):
    await make_healthy(repository)
    import aiosqlite
    async with aiosqlite.connect(repository.db_path) as db:
        await db.execute("UPDATE reconciliation_runs SET completed_at='2000-01-01T00:00:00+00:00'")
        await db.commit()
    stale = await SubmissionReadinessEvaluator(repository).evaluate(context())
    assert check(stale, "Reconciliation freshness").status == CheckStatus.FAIL
    await make_healthy(repository)
    await repository.record_alert("critical", "test_critical", details="safe")
    alerted = await SubmissionReadinessEvaluator(repository).evaluate(context())
    assert check(alerted, "Critical reconciliation alerts").status == CheckStatus.FAIL


@pytest.mark.parametrize("values,name", [
    ({"total_markets": 10, "eligible_markets": 0}, "Eligible markets"),
    ({"opportunity_count": 3, "allocation_count": 0, "allocated_dollars": 0}, "Portfolio allocation"),
    ({"existing_position_blocked": True}, "Existing position"),
    ({"sufficient_balance": False, "available_balance": 10, "required_balance": 20}, "Sufficient balance"),
    ({"valid_quote": False, "price": 1.0}, "Quote and price"),
])
async def test_cycle_observation_failures_are_visible(repository, values, name):
    await make_healthy(repository)
    report = await SubmissionReadinessEvaluator(repository).evaluate(context(**values))
    assert check(report, name).status == CheckStatus.FAIL


async def test_output_never_contains_acknowledgement_or_credentials(repository):
    await make_healthy(repository)
    secret = "super-secret-api-key"
    report = await SubmissionReadinessEvaluator(repository).evaluate(context(
        configured_environment="production", client_environment="production",
        production_acknowledgement=PRODUCTION_ACKNOWLEDGEMENT,
    ))
    rendered = report.format_summary() + repr(report.to_dict())
    assert PRODUCTION_ACKNOWLEDGEMENT not in rendered
    assert secret not in rendered


async def test_evaluator_matches_verified_service_gate_decision(repository):
    manager = DatabaseManager(repository.db_path)
    position_id = await manager.add_position(Position(
        market_id="MKT", side="YES", entry_price=.4, quantity=1,
        timestamp=datetime.now(), status="pending", live=False,
    ))
    safety = ExecutionSafetyConfig(
        live_mode=True, authoritative_execution_enabled=False,
        reconciliation_enabled=True, kill_switch=False,
    )
    evaluator_report = await SubmissionReadinessEvaluator(repository).evaluate(context(
        authoritative_execution_enabled=False
    ))
    service = VerifiedExecutionService(
        repository, NoNetworkClient(), OrderReconciler(repository, NoNetworkClient()), safety
    )
    with pytest.raises(ExecutionSafetyError, match="authoritative execution is disabled"):
        await service.execute(OrderIntent("MKT", "YES", "buy", 1, .4, "limit", position_id, "demo"))
    assert evaluator_report.overall == "BLOCKED"


async def test_quote_and_balance_predicates_are_shared_and_fail_closed():
    assert valid_order_price(.5)
    assert not valid_order_price(0)
    assert not valid_order_price(1)
    assert not valid_order_price("malformed")
    assert sufficient_order_balance(100, .5, 2, "buy")
    assert not sufficient_order_balance(99, .5, 2, "buy")
    assert not sufficient_order_balance("malformed", .5, 2, "buy")


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("no such table: reconciliation_runs"),
])
async def test_reconciliation_database_failures_are_blocked_without_details(error):
    class FailingRepository:
        async def get_reconciliation_health(self, max_age_seconds):
            raise error

    report = await SubmissionReadinessEvaluator(FailingRepository()).evaluate(context())
    health = check(report, "Latest reconciliation health")
    assert health.status == CheckStatus.BLOCKED
    assert report.overall == "BLOCKED"
    assert str(error) not in report.format_summary()


async def test_legacy_database_without_reconciliation_tables_fails_closed(tmp_path):
    path = tmp_path / "legacy.db"
    import aiosqlite
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY)")
        await db.commit()
    report = await SubmissionReadinessEvaluator(OrderRepository(str(path))).evaluate(context())
    assert check(report, "Latest reconciliation health").status == CheckStatus.BLOCKED


async def test_structured_log_is_json_serializable_and_stable(repository):
    await make_healthy(repository)
    report = await SubmissionReadinessEvaluator(repository).evaluate(context(
        allocated_dollars=Decimal("12.34"), allocation_count=1,
    ))
    payload = report.to_dict()
    assert payload["event"] == "trading_submission_readiness"
    assert json.loads(json.dumps(payload))["overall"] == report.overall


@pytest.mark.parametrize("overrides,expected", [
    ({"live_mode": False}, "live mode is not enabled"),
    ({"authoritative_execution_enabled": False}, "authoritative execution is disabled"),
    ({"reconciliation_enabled": False}, "reconciliation is disabled"),
    ({"kill_switch": True}, "live order submission kill switch is enabled"),
    ({"client_environment": "production"}, "Kalshi client environment does not match order intent"),
    ({"configured_environment": "production", "client_environment": "production"},
     "production execution acknowledgement is missing"),
])
async def test_exact_preflight_reason_parity(repository, overrides, expected):
    await make_healthy(repository)
    manager = DatabaseManager(repository.db_path)
    position_id = await manager.add_position(Position(
        market_id="PARITY", side="YES", entry_price=.4, quantity=1,
        timestamp=datetime.now(), status="pending", live=False,
    ))
    report_context = context(position_id=position_id, market_id="PARITY", side="YES",
                             action="buy", quantity=1, **overrides)
    report = await SubmissionReadinessEvaluator(repository).evaluate(
        report_context, scope="pre_submission"
    )
    assert report.blocking_checks[0].reason == expected
    client = NoNetworkClient()
    client.environment = overrides.get("client_environment", "demo")
    safety = ExecutionSafetyConfig(
        live_mode=overrides.get("live_mode", True),
        authoritative_execution_enabled=overrides.get(
            "authoritative_execution_enabled", True
        ),
        reconciliation_enabled=overrides.get("reconciliation_enabled", True),
        kill_switch=overrides.get("kill_switch", False),
        reconciliation_max_age_seconds=60,
    )
    service = VerifiedExecutionService(
        repository, client, OrderReconciler(repository, client), safety
    )
    service_intent = OrderIntent(
        "PARITY", "YES", "buy", 1, .4, "limit", position_id,
        overrides.get("configured_environment", "demo"),
    )
    with pytest.raises(ExecutionSafetyError) as raised:
        await service._preflight_gateway(service_intent, require_health=True)
    assert str(raised.value) == expected


async def test_unusual_repository_exception_does_not_leak_secret():
    secret = "PRIVATE_KEY_MATERIAL"

    class Repository:
        async def get_reconciliation_health(self, max_age_seconds):
            return {"healthy_status": True, "health_reason": "ok", "status": "completed",
                    "fresh": True, "freshness_reason": "ok", "age_seconds": 0,
                    "critical_alert_count": 0}

        async def assert_position_intent(self, *args):
            raise RuntimeError(secret)

    report = await SubmissionReadinessEvaluator(Repository()).evaluate(context(
        position_id=1, market_id="MKT", side="YES", action="buy", quantity=1,
    ), scope="pre_submission")
    rendered = report.format_summary() + repr(report.to_dict())
    assert secret not in rendered
    assert check(report, "Position intent").status == CheckStatus.BLOCKED


async def test_cycle_summary_emitted_once(monkeypatch):
    class Logger:
        def __init__(self):
            self.calls = []

        def info(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def error(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    class DB:
        db_path = "unused-in-paper-mode.db"

        async def get_market_count(self):
            return 4

        async def get_open_positions(self):
            return []

    system = UnifiedAdvancedTradingSystem.__new__(UnifiedAdvancedTradingSystem)
    system.db_manager = DB()
    system.kalshi_client = NoNetworkClient()
    system.logger = Logger()
    system._readiness_logged = False
    monkeypatch.setattr("src.config.settings.settings.trading.live_trading_enabled", False)
    await system._log_cycle_readiness([], None, None)
    await system._log_cycle_readiness([], None, None)
    assert len(system.logger.calls) == 2
    assert system.logger.calls[1][0][0] == "trading_submission_readiness"


async def test_cycle_diagnostic_exception_is_contained(monkeypatch):
    secret = "SECRET_DATABASE_PATH"

    class Logger:
        def __init__(self):
            self.messages = []

        def info(self, *args, **kwargs):
            self.messages.append(str(args))

        def error(self, *args, **kwargs):
            self.messages.append(str(args))

    class DB:
        db_path = "unused.db"

        async def get_market_count(self):
            raise RuntimeError(secret)

    system = UnifiedAdvancedTradingSystem.__new__(UnifiedAdvancedTradingSystem)
    system.db_manager = DB()
    system.kalshi_client = NoNetworkClient()
    system.logger = Logger()
    system._readiness_logged = False
    await system._log_cycle_readiness([], None, None)
    assert secret not in repr(system.logger.messages)
    assert system._readiness_logged is True
    assert any("trading_submission_readiness" in message
               for message in system.logger.messages)
