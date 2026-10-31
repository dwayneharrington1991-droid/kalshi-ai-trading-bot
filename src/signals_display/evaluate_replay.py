"""Walk-forward offline evaluation; no network or trading dependencies.

Fixed model/configuration, expanding observed warmup followed by chronological
holdout. Models only ingest observations/bar closes available at prediction time.
Official outcomes must arrive at/after the recorded contract close.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from .live_model import LiveEstimator, VERSION
from .model import timestamp
from .monitor import active_market, analyze, Journal


def walk_forward(frames, journal, *, training_frames=61, target=0.05, stop=0.10, horizon=60):
    model = LiveEstimator()
    prior = None
    closes = {}
    recorded = []
    for index, payload in enumerate(frames):
        now = timestamp(payload["now"])
        if prior is not None and now <= prior:
            raise ValueError("Chronological evaluation requires strictly increasing timestamps")
        prior = now
        for market in payload.get("markets", []):
            closes[market["ticker"]] = timestamp(market["closes_at"])
            if timestamp(market["observed_at"]) > now:
                raise ValueError("Future-dated quote cannot be used in a prediction")
        ref = payload.get("reference", {})
        if ref.get("observed_at") and timestamp(ref["observed_at"]) > now:
            raise ValueError("Future-dated reference cannot be used in a prediction")
        for ticker in payload.get("settlements", {}):
            if ticker not in closes or now < closes[ticker]:
                raise ValueError(
                    "Settlement outcome arrived before contract close or without metadata"
                )
        model.observe(payload, now)
        if index < training_frames:
            continue
        market = active_market(payload, now)
        result, info = model.estimate(payload, market, now, target, stop, horizon)
        state = analyze(
            {**payload, "mode": "REPLAY / SIMULATED"}, now, target, stop, horizon, result, info
        )
        # Persist prediction before applying any outcome from this frame.
        journal.step(state)
        recorded.append(
            {
                "timestamp": state["now"],
                "ticker": market["ticker"] if market else None,
                "probability_up": result.settlement_up,
                "scalp_up": result.scalp_up,
                "scalp_down": result.scalp_down,
                "direction": state["direction"],
                "scalp": state["scalp"],
            }
        )
        for ticker, outcome in payload.get("settlements", {}).items():
            journal.settle(ticker, outcome, "REPLAY / SIMULATED")
    return {
        "model_version": VERSION,
        "training_frames": training_frames,
        "holdout_frames": len(recorded),
        "method": "Fixed parameters; rolling past-only volatility; chronological holdout; outcomes applied only after predictions are recorded.",
        "predictions": recorded,
        "performance": journal.performance(),
        "edge_status": "edge unverified",
        "profitable_edge_demonstrated": False,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Offline chronological signals-only replay evaluation"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--training-frames", type=int, default=61)
    args = parser.parse_args()
    if args.training_frames < 1:
        parser.error("training-frames must be positive")
    raw = args.input.read_bytes()
    with tempfile.TemporaryDirectory() as folder:
        report = walk_forward(
            json.loads(raw),
            Journal(Path(folder) / "evaluation.db"),
            training_frames=args.training_frames,
        )
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        "Chronological evaluation saved. Edge unverified; synthetic replays cannot demonstrate live profitability."
    )


if __name__ == "__main__":
    main()
