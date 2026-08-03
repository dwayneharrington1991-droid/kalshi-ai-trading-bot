"""Strict, execution-independent models for multi-agent shadow analysis."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ShadowAction(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    HOLD = "HOLD"
    ABSTAIN = "ABSTAIN"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def _unit(value: Optional[float], name: str, *, nullable: bool = False) -> Optional[float]:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")
    return value


@dataclass(frozen=True)
class MarketAnalysisRequest:
    request_id: str
    market_id: str
    title: str
    rules: str
    category: str
    yes_price: float
    no_price: float
    bid_ask_spread: float
    volume: float
    expiration_timestamp: str
    time_remaining_seconds: int
    analysis_timestamp: str
    configuration_version: str
    open_interest: Optional[float] = None
    current_position: Dict[str, Any] = field(default_factory=dict)
    account_exposure: Dict[str, Any] = field(default_factory=dict)
    single_model_analysis: Dict[str, Any] = field(default_factory=dict)
    context: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.request_id or not self.market_id or not self.title:
            raise ValueError("request_id, market_id, and title are required")
        _unit(self.yes_price, "yes_price")
        _unit(self.no_price, "no_price")
        if self.bid_ask_spread < 0 or self.volume < 0 or self.time_remaining_seconds < 0:
            raise ValueError("spread, volume, and time remaining cannot be negative")
        single_probability = self.single_model_analysis.get(
            "probability_yes", self.single_model_analysis.get("probability")
        )
        if single_probability is not None:
            _unit(single_probability, "single_model_probability")

    @classmethod
    def deterministic_id(cls, market_id: str, timestamp: str, version: str) -> str:
        return hashlib.sha256(f"{market_id}|{timestamp}|{version}".encode()).hexdigest()[:32]

    def prompt_payload(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    request_id: str
    agent_name: str
    agent_version: str
    model_name: str
    timestamp: str
    success: bool
    probability_yes: Optional[float] = None
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    uncertainty: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error_category: Optional[str] = None
    latency_seconds: float = 0.0
    token_count: Optional[int] = None
    estimated_cost: Optional[float] = None
    sanitized_response: Optional[str] = None
    fallback_used: bool = False
    evidence_sufficient: bool = False
    risk_level: Optional[RiskLevel] = None
    veto: bool = False
    recommendation: ShadowAction = ShadowAction.ABSTAIN
    invalidation_factors: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.confidence = _unit(self.confidence, "confidence") or 0.0
        self.probability_yes = _unit(
            self.probability_yes, "probability_yes", nullable=True
        )
        if self.success and self.agent_name != "news_analyst" and self.probability_yes is None:
            raise ValueError("successful analytical result requires probability_yes")
        if self.latency_seconds < 0:
            raise ValueError("latency cannot be negative")
        if self.token_count is not None and (
            isinstance(self.token_count, bool) or not isinstance(self.token_count, int)
            or self.token_count < 0
        ):
            raise ValueError("token_count must be a nonnegative integer")
        if self.estimated_cost is not None and (
            isinstance(self.estimated_cost, bool)
            or not isinstance(self.estimated_cost, (int, float))
            or not math.isfinite(float(self.estimated_cost)) or self.estimated_cost < 0
        ):
            raise ValueError("estimated_cost must be finite and nonnegative")
        if self.sanitized_response:
            self.sanitized_response = self.sanitized_response[:4000]

    @classmethod
    def failure(cls, request_id: str, name: str, model: str, category: str) -> "AgentResult":
        return cls(
            request_id=request_id, agent_name=name, agent_version="1",
            model_name=model, timestamp=datetime.now(timezone.utc).isoformat(),
            success=False, error_category=category, warnings=["agent unavailable"],
        )

    def sanitized_json(self) -> str:
        payload = asdict(self)
        payload["risk_level"] = self.risk_level.value if self.risk_level else None
        payload["recommendation"] = self.recommendation.value
        return json.dumps(payload, sort_keys=True)


@dataclass(frozen=True)
class DebateSummary:
    contradictions: List[str]
    unsupported_claims: List[str]
    majority_view: str
    minority_views: List[str]


@dataclass(frozen=True)
class ConsensusPrediction:
    request_id: str
    market_id: str
    probability_yes: float
    probability_no: float
    process_confidence: float
    disagreement_score: float
    evidence_quality_score: float
    uncertainty_score: float
    recommendation: ShadowAction
    expected_edge: float
    risk_veto: bool
    explanation: str
    created_at: str

    def __post_init__(self) -> None:
        for name in (
            "probability_yes", "probability_no", "process_confidence",
            "disagreement_score", "evidence_quality_score", "uncertainty_score",
        ):
            _unit(getattr(self, name), name)
        if not math.isclose(self.probability_yes + self.probability_no, 1.0, abs_tol=1e-9):
            raise ValueError("YES and NO probabilities must sum to one")


@dataclass(frozen=True)
class AnalysisRunResult:
    request: MarketAnalysisRequest
    outputs: List[AgentResult]
    debate: DebateSummary
    consensus: ConsensusPrediction
    status: str
    trading_impact: str = "NONE"
