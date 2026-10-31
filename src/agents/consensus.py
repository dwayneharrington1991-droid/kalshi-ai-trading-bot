"""Deterministic consensus for shadow recommendations only."""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

from .models import AgentResult, ConsensusPrediction, MarketAnalysisRequest, ShadowAction


class ConsensusEngine:
    def __init__(self, weights: Optional[Dict[str, float]] = None, min_successful: int = 3):
        self.weights = weights or {}
        self.min_successful = min_successful
        if any(not math.isfinite(v) or v <= 0 for v in self.weights.values()):
            raise ValueError("weights must be finite and positive")
        if self.weights and not math.isfinite(sum(self.weights.values())):
            raise ValueError("weight sum must be finite")

    def build(
        self, request: MarketAnalysisRequest, outputs: Iterable[AgentResult],
        *, require_forecaster: bool = True, require_risk: bool = True,
    ) -> ConsensusPrediction:
        ordered = sorted(outputs, key=lambda item: item.agent_name)
        names_all = [item.agent_name for item in ordered]
        if len(names_all) != len(set(names_all)):
            raise ValueError("duplicate agent result")
        successful = [item for item in ordered if item.success and item.probability_yes is not None]
        names = {item.agent_name for item in successful}
        risk = next((item for item in ordered if item.agent_name == "risk_manager"), None)
        veto = bool(risk and risk.success and risk.veto)
        insufficient = (
            len(successful) < self.min_successful
            or (require_forecaster and "forecaster" not in names)
            or (require_risk and (not risk or not risk.success))
        )
        if successful:
            weighted = [(item, self.weights.get(item.agent_name, 1.0)) for item in successful]
            denominator = sum(weight for _, weight in weighted)
            probability = sum(item.probability_yes * weight for item, weight in weighted) / denominator
            disagreement = statistics.pstdev(item.probability_yes for item in successful)
            confidence = sum(item.confidence for item in successful) / len(successful)
            evidence_quality = sum(1.0 if item.evidence_sufficient else 0.0 for item in successful) / len(successful)
        else:
            probability, disagreement, confidence, evidence_quality = 0.5, 1.0, 0.0, 0.0
        disagreement = min(1.0, disagreement)
        uncertainty = min(1.0, max(disagreement, 1.0 - evidence_quality))
        edge = probability - request.yes_price
        weak = insufficient or evidence_quality < 0.5 or uncertainty > 0.5
        if veto or weak:
            action = ShadowAction.ABSTAIN
        elif edge >= 0.05:
            action = ShadowAction.BUY_YES
        elif edge <= -0.05:
            action = ShadowAction.BUY_NO
        else:
            action = ShadowAction.HOLD
        explanation = (
            "ABSTAIN: required evidence or agent availability is insufficient"
            if insufficient else "ABSTAIN: risk veto" if veto
            else f"Shadow consensus {action.value}; no trading impact"
        )
        return ConsensusPrediction(
            request_id=request.request_id, market_id=request.market_id,
            probability_yes=probability, probability_no=1.0 - probability,
            process_confidence=max(0.0, min(1.0, confidence * (1.0 - disagreement))),
            disagreement_score=disagreement, evidence_quality_score=evidence_quality,
            uncertainty_score=uncertainty, recommendation=action,
            expected_edge=edge, risk_veto=veto, explanation=explanation,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
