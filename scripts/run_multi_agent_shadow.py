#!/usr/bin/env python3
"""Run one offline multi-agent shadow cycle from a local JSON fixture."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.models import MarketAnalysisRequest
from src.agents.orchestrator import MultiAgentOrchestrator
from src.agents.repository import AgentRepository
from src.agents.shadow_service import MultiAgentShadowService
from src.utils.database import DatabaseManager


async def run(fixture_path: str, db_path: str) -> dict:
    payload = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    request = MarketAnalysisRequest(**payload["request"])
    outputs = payload["agent_outputs"]

    def make(role):
        async def execute(_request):
            return outputs[role]
        return execute

    await DatabaseManager(db_path).initialize()
    orchestrator = MultiAgentOrchestrator({name: make(name) for name in outputs})
    service = MultiAgentShadowService(orchestrator, AgentRepository(db_path), "offline")
    result = await service.analyze(request, cycle_id="bounded-offline")
    return {
        "market_id": request.market_id,
        "recommendation": result.consensus.recommendation.value,
        "probability_yes": result.consensus.probability_yes,
        "risk_veto": result.consensus.risk_veto,
        "trading_impact": "NONE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.fixture, args.database)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

