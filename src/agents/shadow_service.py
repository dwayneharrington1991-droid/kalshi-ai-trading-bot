"""Shadow-only adapter around existing agent prompts and model completion interface."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional

from .bear_researcher import BearResearcher
from .bull_researcher import BullResearcher
from .forecaster_agent import ForecasterAgent
from .models import AnalysisRunResult, MarketAnalysisRequest
from .news_analyst_agent import NewsAnalystAgent
from .orchestrator import MultiAgentOrchestrator
from .repository import AgentRepository
from .risk_manager_agent import RiskManagerAgent


class MultiAgentShadowService:
    """Produces durable comparisons and has no execution/allocation interface."""

    def __init__(self, orchestrator: MultiAgentOrchestrator, repository: AgentRepository, environment: str):
        self.orchestrator = orchestrator
        self.repository = repository
        self.environment = environment
        self.logger = logging.getLogger("multi_agent_shadow")

    async def analyze(self, request: MarketAnalysisRequest, cycle_id: Optional[str] = None) -> AnalysisRunResult:
        result = await self.orchestrator.analyze(request)
        await self.repository.store_analysis(result, environment=self.environment, cycle_id=cycle_id)
        single = request.single_model_analysis.get("probability_yes", request.single_model_analysis.get("probability"))
        self.logger.info(
            "MULTI_AGENT_SHADOW market=%s single_probability=%s multi_probability=%.4f consensus=%s risk_veto=%s disagreement=%.4f trading_impact=NONE",
            request.market_id, single, result.consensus.probability_yes,
            result.consensus.recommendation.value, result.consensus.risk_veto,
            result.consensus.disagreement_score,
        )
        return result


def build_existing_agent_executors(
    completion: Callable[..., Awaitable[Optional[str]]],
) -> Dict[str, Callable[[MarketAnalysisRequest], Awaitable[Mapping[str, Any]]]]:
    agents = {
        "forecaster": ForecasterAgent(), "bull_analyst": BullResearcher(),
        "bear_analyst": BearResearcher(), "news_analyst": NewsAnalystAgent(),
        "risk_manager": RiskManagerAgent(),
    }

    def executor(role: str):
        async def run(request: MarketAnalysisRequest) -> Mapping[str, Any]:
            agent = agents[role]
            if role == "news_analyst" and not request.context:
                return {
                    "confidence": 0.0, "evidence": [],
                    "uncertainty": ["no reliable supplied context"],
                    "assumptions": [], "evidence_sufficient": False,
                    "recommendation": "ABSTAIN", "model_name": agent.model_name,
                }
            market = request.prompt_payload()
            market["days_to_expiry"] = request.time_remaining_seconds / 86400
            prompt = agent._build_user_prompt(market, {"shadow_mode": True})
            raw = await completion(
                prompt, strategy="multi_agent_shadow", query_type=role,
                market_id=request.market_id,
            )
            if not isinstance(raw, str):
                raise ValueError("model response unavailable")
            match = re.fullmatch(r"\s*```(?:json)?\s*(\{.*\})\s*```\s*", raw, re.DOTALL)
            payload = json.loads(match.group(1) if match else raw)
            if not isinstance(payload, dict):
                raise ValueError("model response must be an object")
            normalized = dict(payload)
            if "probability" in normalized:
                normalized["probability_yes"] = normalized["probability"]
            if role == "news_analyst":
                normalized.setdefault("confidence", normalized.get("relevance", 0.0))
                normalized.setdefault("evidence", normalized.get("key_factors", []))
            if role == "risk_manager":
                normalized.setdefault("probability_yes", request.yes_price)
                normalized.setdefault("confidence", 0.5)
                score = normalized.get("risk_score", 10)
                normalized.setdefault("risk_level", "CRITICAL" if score >= 8 else "HIGH" if score >= 6 else "MEDIUM" if score >= 3 else "LOW")
                normalized.setdefault("veto", not bool(normalized.get("should_trade", False)))
                deterministic_risks = []
                if not request.rules.strip():
                    deterministic_risks.append("ambiguous or missing settlement wording")
                if any(term in request.title.lower() for term in ("collection", "combined", "aggregate")):
                    deterministic_risks.append("aggregate or collection market")
                if request.volume < 100:
                    deterministic_risks.append("low liquidity")
                if request.bid_ask_spread > 0.15:
                    deterministic_risks.append("wide spread")
                if request.yes_price <= 0.01 or request.yes_price >= 0.99:
                    deterministic_risks.append("extreme price")
                if request.time_remaining_seconds < 3600:
                    deterministic_risks.append("short time to settlement")
                if request.account_exposure.get("stale_quote"):
                    deterministic_risks.append("stale quote")
                if request.account_exposure.get("correlated") or request.account_exposure.get("concentrated"):
                    deterministic_risks.append("correlated or concentrated exposure")
                if request.account_exposure.get("unresolved_reconciliation_alerts"):
                    deterministic_risks.append("unresolved reconciliation alerts")
                if request.current_position.get("duplicate_position") or request.current_position.get("duplicate_intent"):
                    deterministic_risks.append("duplicate position or intent")
                if deterministic_risks:
                    normalized["veto"] = True
                    normalized["risk_level"] = "CRITICAL"
                    normalized.setdefault("warnings", []).extend(deterministic_risks)
            normalized.setdefault("evidence", normalized.get("key_arguments", []))
            normalized.setdefault("uncertainty", normalized.get("risk_factors", []))
            normalized.setdefault("assumptions", [])
            normalized.setdefault("evidence_sufficient", bool(normalized.get("evidence")))
            normalized.setdefault("recommendation", "ABSTAIN")
            normalized["model_name"] = agent.model_name
            return normalized
        return run

    return {role: executor(role) for role in agents}
