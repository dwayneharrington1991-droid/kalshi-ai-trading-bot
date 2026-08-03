"""Bounded, failure-isolated orchestration for multi-agent shadow analysis."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from .consensus import ConsensusEngine
from .models import (
    AgentResult, AnalysisRunResult, DebateSummary, MarketAnalysisRequest,
    RiskLevel, ShadowAction,
)

AgentExecutor = Callable[[MarketAnalysisRequest], Awaitable[Mapping[str, Any]]]
REQUIRED_ROLES = ("forecaster", "bull_analyst", "bear_analyst", "news_analyst", "risk_manager")


class ShadowConfigurationError(RuntimeError):
    pass


class MultiAgentOrchestrator:
    def __init__(
        self, executors: Mapping[str, AgentExecutor], *, max_concurrency: int = 3,
        timeout_seconds: float = 30, max_retries: int = 1,
        min_successful_agents: int = 3, require_forecaster: bool = True,
        require_risk: bool = True, fallback_executors: Optional[Mapping[str, AgentExecutor]] = None,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 60,
    ):
        if max_concurrency < 1 or max_retries < 0:
            raise ValueError("invalid orchestration limits")
        self.executors = dict(executors)
        self.fallback_executors = dict(fallback_executors or {})
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.require_forecaster = require_forecaster
        self.require_risk = require_risk
        self.consensus = ConsensusEngine(min_successful=min_successful_agents)
        self.failure_counts: Dict[str, int] = {}
        self.circuit_opened_at: Dict[str, float] = {}
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds

    @staticmethod
    def assert_shadow_configuration(enabled: bool, can_affect_trading: bool) -> None:
        if can_affect_trading:
            raise ShadowConfigurationError("multi-agent trading impact is forbidden")

    async def analyze(self, request: MarketAnalysisRequest) -> AnalysisRunResult:
        tasks = [self._run_role(role, request) for role in REQUIRED_ROLES]
        outputs = list(await asyncio.gather(*tasks))
        consensus = self.consensus.build(
            request, outputs, require_forecaster=self.require_forecaster,
            require_risk=self.require_risk,
        )
        probabilities = [o.probability_yes for o in outputs if o.success and o.probability_yes is not None]
        contradictions = []
        if probabilities and max(probabilities) - min(probabilities) >= 0.4:
            contradictions.append("agent probability estimates materially conflict")
        unsupported = [o.agent_name for o in outputs if o.success and not o.evidence_sufficient]
        above = [o.agent_name for o in outputs if o.success and o.probability_yes is not None and o.probability_yes >= 0.5]
        below = [o.agent_name for o in outputs if o.success and o.probability_yes is not None and o.probability_yes < 0.5]
        debate = DebateSummary(
            contradictions=contradictions, unsupported_claims=unsupported,
            majority_view="YES" if len(above) > len(below) else "NO" if len(below) > len(above) else "SPLIT",
            minority_views=below if len(above) > len(below) else above,
        )
        status = "completed" if any(o.success for o in outputs) else "failed"
        return AnalysisRunResult(request, outputs, debate, consensus, status)

    async def _run_role(self, role: str, request: MarketAnalysisRequest) -> AgentResult:
        executor = self.executors.get(role)
        if executor is None:
            return AgentResult.failure(request.request_id, role, "unavailable", "unavailable")
        if self.failure_counts.get(role, 0) >= self.circuit_failure_threshold:
            opened = self.circuit_opened_at.get(role, time.monotonic())
            if time.monotonic() - opened < self.circuit_cooldown_seconds:
                return AgentResult.failure(request.request_id, role, "unavailable", "circuit_open")
            self.failure_counts[role] = 0
            self.circuit_opened_at.pop(role, None)
        start = time.monotonic()
        last_category = "agent_error"
        for attempt in range(self.max_retries + 1):
            try:
                async with self.semaphore:
                    raw = await asyncio.wait_for(executor(request), timeout=self.timeout_seconds)
                result = self._normalize(role, request.request_id, raw, time.monotonic() - start)
                self.failure_counts[role] = 0
                self.circuit_opened_at.pop(role, None)
                return result
            except asyncio.TimeoutError:
                last_category = "timeout"
            except (ValueError, TypeError, json.JSONDecodeError):
                last_category = "malformed_response"
                break
            except Exception as exc:
                last_category = "rate_limit" if (
                    "rate" in type(exc).__name__.lower() or "rate" in str(exc).lower()
                ) else "agent_error"
        fallback = self.fallback_executors.get(role)
        if fallback:
            try:
                async with self.semaphore:
                    raw = await asyncio.wait_for(fallback(request), timeout=self.timeout_seconds)
                result = self._normalize(role, request.request_id, raw, time.monotonic() - start)
                result.fallback_used = True
                return result
            except Exception:
                last_category = "fallback_failed"
        self.failure_counts[role] = self.failure_counts.get(role, 0) + 1
        if self.failure_counts[role] >= self.circuit_failure_threshold:
            self.circuit_opened_at[role] = time.monotonic()
        return AgentResult.failure(request.request_id, role, "unavailable", last_category)

    @staticmethod
    def _normalize(role: str, request_id: str, raw: Mapping[str, Any], latency: float) -> AgentResult:
        if not isinstance(raw, Mapping):
            raise ValueError("agent response must be an object")
        if raw.get("claims_live_tool_access") is True:
            raise ValueError("fabricated tool-use claim")
        claim_text = json.dumps(raw, default=str).lower()
        if any(claim in claim_text for claim in (
            "i browsed the web", "i searched the internet", "live web access",
            "i queried the live web",
        )):
            raise ValueError("fabricated tool-use claim")
        probability = raw.get("probability_yes", raw.get("probability"))
        probability_no = raw.get("probability_no")
        confidence = raw.get("confidence")
        if confidence is None:
            raise ValueError("confidence is required")
        if probability is not None and (
            isinstance(probability, bool) or not isinstance(probability, (int, float))
            or not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1
        ):
            raise ValueError("invalid probability")
        if probability is not None and probability_no is not None and (
            isinstance(probability_no, bool) or not isinstance(probability_no, (int, float))
            or not math.isfinite(float(probability_no))
            or not math.isclose(float(probability) + float(probability_no), 1.0, abs_tol=1e-6)
        ):
            raise ValueError("contradictory probabilities")
        evidence = raw.get("evidence", raw.get("supporting_factors", raw.get("key_factors", [])))
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        for field in ("uncertainty", "assumptions", "warnings", "invalidation_factors"):
            if not isinstance(raw.get(field, []), list):
                raise ValueError(f"{field} must be a list")
        risk_level = None
        veto = False
        if role == "risk_manager":
            risk_level = RiskLevel(str(raw.get("risk_level", "HIGH")).upper())
            veto = bool(raw.get("veto", True))
        sensitive = ("secret", "key", "credential", "authorization", "header", "signature", "pem")

        def scrub(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {
                    str(key): "[REDACTED]" if any(part in str(key).lower() for part in sensitive) else scrub(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, str) and (
                "-----BEGIN" in value or re.search(r"\b(?:sk|key)-[A-Za-z0-9_-]{8,}", value)
                or value.lower().startswith("bearer ") or value.lower().endswith(".pem")
            ):
                return "[REDACTED]"
            return value

        clean = scrub(raw)
        sanitized = json.dumps(clean, sort_keys=True, default=str)
        return AgentResult(
            request_id=request_id, agent_name=role, agent_version=str(raw.get("agent_version", "1")),
            model_name=str(raw.get("model_name", "configured")),
            timestamp=datetime.now(timezone.utc).isoformat(), success=True,
            probability_yes=float(probability) if probability is not None else None,
            confidence=float(confidence), evidence=[str(v) for v in clean.get("evidence", clean.get("supporting_factors", clean.get("key_factors", [])))],
            uncertainty=[str(v) for v in clean.get("uncertainty", [])],
            assumptions=[str(v) for v in clean.get("assumptions", [])],
            warnings=[str(v) for v in clean.get("warnings", [])],
            latency_seconds=latency, token_count=raw.get("token_count"),
            estimated_cost=raw.get("estimated_cost"), sanitized_response=sanitized,
            evidence_sufficient=bool(raw.get("evidence_sufficient", False)),
            risk_level=risk_level, veto=veto,
            recommendation=ShadowAction(str(raw.get("recommendation", "ABSTAIN")).upper()),
            invalidation_factors=[str(v) for v in raw.get("invalidation_factors", [])],
        )
