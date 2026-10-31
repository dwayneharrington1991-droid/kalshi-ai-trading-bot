import asyncio
import math
from datetime import datetime, timedelta, timezone

import pytest

from src.agents.consensus import ConsensusEngine
from src.agents.models import AgentResult, MarketAnalysisRequest, ShadowAction
from src.agents.orchestrator import MultiAgentOrchestrator, ShadowConfigurationError
from src.agents.shadow_service import build_existing_agent_executors


def request():
    timestamp = "2026-01-01T00:00:00+00:00"
    return MarketAnalysisRequest(
        request_id=MarketAnalysisRequest.deterministic_id("MKT", timestamp, "v1"),
        market_id="MKT", title="Will it happen?", rules="Authoritative rule",
        category="test", yes_price=.45, no_price=.55, bid_ask_spread=.02,
        volume=1000, expiration_timestamp="2026-01-02T00:00:00+00:00",
        time_remaining_seconds=86400, analysis_timestamp=timestamp,
        configuration_version="v1", single_model_analysis={"probability_yes": .6},
    )


def payload(probability=.6, **values):
    result = {
        "probability_yes": probability, "confidence": .7,
        "evidence": ["fixture evidence"], "uncertainty": ["fixture uncertainty"],
        "assumptions": [], "evidence_sufficient": True,
        "recommendation": "HOLD", "model_name": "fake",
    }
    result.update(values)
    return result


def executors(overrides=None, tracker=None):
    overrides = overrides or {}
    tracker = tracker or {"active": 0, "maximum": 0}
    result = {}
    for index, role in enumerate(("forecaster", "bull_analyst", "bear_analyst", "news_analyst", "risk_manager")):
        async def execute(req, role=role, index=index):
            tracker["active"] += 1
            tracker["maximum"] = max(tracker["maximum"], tracker["active"])
            try:
                await asyncio.sleep(.001)
                value = overrides.get(role, payload(.55 + index * .01))
                if isinstance(value, BaseException):
                    raise value
                if callable(value):
                    return await value(req)
                if role == "risk_manager":
                    value = dict(value, risk_level="LOW", veto=False)
                return value
            finally:
                tracker["active"] -= 1
        result[role] = execute
    return result


@pytest.mark.asyncio
async def test_all_agents_succeed_and_order_is_deterministic():
    result = await MultiAgentOrchestrator(executors()).analyze(request())
    assert result.status == "completed"
    assert [item.agent_name for item in result.outputs] == [
        "forecaster", "bull_analyst", "bear_analyst", "news_analyst", "risk_manager"
    ]
    assert result.trading_impact == "NONE"
    assert result.consensus.recommendation in ShadowAction


@pytest.mark.asyncio
async def test_partial_and_required_agent_failures_abstain():
    one = await MultiAgentOrchestrator(executors({"bull_analyst": RuntimeError("SECRET")}), max_retries=0).analyze(request())
    assert sum(item.success for item in one.outputs) == 4
    required = await MultiAgentOrchestrator(executors({"forecaster": RuntimeError("down")}), max_retries=0).analyze(request())
    assert required.consensus.recommendation == ShadowAction.ABSTAIN
    risk = await MultiAgentOrchestrator(executors({"risk_manager": RuntimeError("down")}), max_retries=0).analyze(request())
    assert risk.consensus.recommendation == ShadowAction.ABSTAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1])
async def test_invalid_probabilities_fail_closed(bad):
    result = await MultiAgentOrchestrator(
        executors({"forecaster": payload(bad)}), max_retries=0
    ).analyze(request())
    failed = next(item for item in result.outputs if item.agent_name == "forecaster")
    assert not failed.success
    assert failed.error_category == "malformed_response"
    assert result.consensus.recommendation == ShadowAction.ABSTAIN


@pytest.mark.asyncio
async def test_contradictory_and_malformed_metadata_fail_closed():
    for invalid in (
        payload(.7, probability_no=.7),
        payload(.7, uncertainty="not-a-list"),
        payload(.7, token_count=-1),
    ):
        result = await MultiAgentOrchestrator(
            executors({"forecaster": invalid}), max_retries=0
        ).analyze(request())
        failed = next(item for item in result.outputs if item.agent_name == "forecaster")
        assert not failed.success
        assert failed.error_category == "malformed_response"


@pytest.mark.asyncio
async def test_timeout_fallback_rate_limit_circuit_and_concurrency():
    async def slow(_request):
        await asyncio.sleep(.1)
        return payload()

    fallback = {"forecaster": lambda req: asyncio.sleep(0, result=payload(.7))}
    runner = MultiAgentOrchestrator(
        executors({"forecaster": slow}), fallback_executors=fallback,
        timeout_seconds=.01, max_retries=0, max_concurrency=2,
    )
    result = await runner.analyze(request())
    assert next(o for o in result.outputs if o.agent_name == "forecaster").fallback_used

    tracker = {"active": 0, "maximum": 0}
    await MultiAgentOrchestrator(executors(tracker=tracker), max_concurrency=2).analyze(request())
    assert tracker["maximum"] <= 2

    broken = MultiAgentOrchestrator(executors({"forecaster": RuntimeError("rate")}), max_retries=0, circuit_failure_threshold=1)
    first = await broken.analyze(request())
    second = await broken.analyze(request())
    assert next(o for o in first.outputs if o.agent_name == "forecaster").error_category == "rate_limit"
    assert next(o for o in second.outputs if o.agent_name == "forecaster").error_category == "circuit_open"

    recovered = MultiAgentOrchestrator(
        executors({"forecaster": RuntimeError("down")}), max_retries=0,
        circuit_failure_threshold=1, circuit_cooldown_seconds=0,
    )
    await recovered.analyze(request())
    recovered.executors["forecaster"] = executors()["forecaster"]
    after_cooldown = await recovered.analyze(request())
    assert next(o for o in after_cooldown.outputs if o.agent_name == "forecaster").success


@pytest.mark.asyncio
async def test_secret_sanitization_and_fabricated_tool_claim():
    secret = "sk-THIS_IS_A_SECRET_VALUE"
    raw = payload(.6, evidence=[secret], authorization="Bearer also-secret")
    result = await MultiAgentOrchestrator(executors({"forecaster": raw})).analyze(request())
    output = next(o for o in result.outputs if o.agent_name == "forecaster")
    assert secret not in output.sanitized_json()
    fabricated = await MultiAgentOrchestrator(
        executors({"forecaster": payload(.6, claims_live_tool_access=True)}), max_retries=0
    ).analyze(request())
    assert not next(o for o in fabricated.outputs if o.agent_name == "forecaster").success


def test_consensus_weighting_veto_weak_evidence_and_invalid_weights():
    req = request()
    outputs = [
        AgentResult(req.request_id, "forecaster", "1", "f", req.analysis_timestamp, True, .8, .2, ["e"], evidence_sufficient=True),
        AgentResult(req.request_id, "bull_analyst", "1", "b", req.analysis_timestamp, True, .6, .9, ["e"], evidence_sufficient=True),
        AgentResult(req.request_id, "risk_manager", "1", "r", req.analysis_timestamp, True, .5, .5, ["e"], evidence_sufficient=True, veto=True),
    ]
    consensus = ConsensusEngine({"forecaster": 3, "bull_analyst": 1, "risk_manager": 1}).build(req, outputs)
    assert consensus.probability_yes == pytest.approx(.7)
    assert consensus.recommendation == ShadowAction.ABSTAIN
    assert consensus.process_confidence != consensus.probability_yes
    with pytest.raises(ValueError):
        ConsensusEngine({"forecaster": -1})
    with pytest.raises(ValueError, match="duplicate"):
        ConsensusEngine(min_successful=1).build(req, [outputs[0], outputs[0]])
    with pytest.raises(ValueError):
        ConsensusEngine({"a": 1e308, "b": 1e308})


def test_trading_impact_configuration_is_impossible():
    with pytest.raises(ShadowConfigurationError):
        MultiAgentOrchestrator.assert_shadow_configuration(True, True)


@pytest.mark.asyncio
async def test_existing_agent_adapter_is_strict_and_news_does_not_guess():
    calls = []

    async def completion(*args, **kwargs):
        calls.append(kwargs.get("query_type"))
        return "not json"

    adapters = build_existing_agent_executors(completion)
    news = await adapters["news_analyst"](request())
    assert news["evidence_sufficient"] is False
    assert calls == []
    with pytest.raises(ValueError):
        await adapters["forecaster"](request())
    assert calls == ["forecaster"]


@pytest.mark.asyncio
async def test_cancellation_cleans_up_agent_tasks():
    active = 0
    cleaned = asyncio.Event()

    async def waits(_request):
        nonlocal active
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            if active == 0:
                cleaned.set()

    runner = MultiAgentOrchestrator({role: waits for role in (
        "forecaster", "bull_analyst", "bear_analyst", "news_analyst", "risk_manager"
    )})
    task = asyncio.create_task(runner.analyze(request()))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cleaned.wait(), timeout=1)
    assert active == 0
