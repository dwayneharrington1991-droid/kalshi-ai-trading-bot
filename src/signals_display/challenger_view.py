"""Second AI opening call for the dashboard: the v4 challenger (research only, NOT the live call).

The challenger is the live artifact applied to the SAME frozen opening inputs of the SAME market after the
30 skewed micro/flow inputs are quantile-aligned (research_artifacts/feature_alignment_20261122.json,
built from input values only; pre-registered forward test v4). It is computed from the immutable opening
record, so it is fixed for the market once made, and it never changes the live call, alerts, scoreboard,
model, threshold or records.
"""
import json
from pathlib import Path

from . import feature_alignment
from .consensus import combine
from .opening_models import predict

ALIGNMENT = Path(__file__).resolve().parents[2] / "research_artifacts" / "feature_alignment_20261122.json"
THRESHOLD = 0.55
LABEL = "CHALLENGER AI (v4 aligned inputs) - research only, NOT VALIDATED; the live call above is the official one"


def _direction(p):
    return "UP" if p >= THRESHOLD else "DOWN" if p <= 1 - THRESHOLD else "WAIT"


class ChallengerView:
    def __init__(self, artifact, alignment_path=ALIGNMENT):
        self.artifact = artifact if isinstance(artifact, dict) else None
        try:
            self.alignment = json.loads(Path(alignment_path).read_text())
        except (OSError, ValueError):
            self.alignment = None
        self.cache = {}

    def call(self, opening):
        opening = opening or {}
        ticker = opening.get("ticker")
        base = {"label": LABEL, "ticker": ticker, "forward_test": "v4 (decides after 200 openings from 2026-11-22 03:30 UTC)"}
        if not self.artifact or not self.alignment:
            return {**base, "status": "UNAVAILABLE", "reason": "challenger artifact or alignment not loaded"}
        values = (opening.get("frozen_features") or {}).get("values")
        live = opening.get("up")
        baseline = opening.get("baseline_probability_up")
        if not ticker or not isinstance(values, dict) or not isinstance(live, (int, float)) \
                or not isinstance(baseline, (int, float)):
            return {**base, "status": "WAITING", "reason": "no frozen opening record for this market yet"}
        key = (ticker, opening.get("generated_at"))
        if key not in self.cache:
            try:
                model = self.artifact["model"]
                reproduced = predict(model, values, baseline_probability=baseline)
                if abs(reproduced - live) > 1e-9:
                    self.cache[key] = {**base, "status": "UNAVAILABLE",
                                       "reason": "live probability not reproducible from the frozen record; not shown"}
                else:
                    p = float(predict(model, feature_alignment.apply(values, self.alignment), baseline_probability=baseline))
                    self.cache[key] = {**base, "status": "READY", "up": p, "down": 1 - p, "direction": _direction(p),
                                       "live_up": float(live), "live_direction": _direction(float(live)),
                                       "agrees_with_live": _direction(p) == _direction(float(live)),
                                       "generated_from": opening.get("generated_at"),
                                       "closes_at": opening.get("closes_at"),
                                       **combine(float(live), p)}
            except Exception as exc:                       # research display only
                self.cache[key] = {**base, "status": "UNAVAILABLE", "reason": type(exc).__name__}
            if len(self.cache) > 64:
                self.cache.pop(next(iter(self.cache)))
        return self.cache[key]
