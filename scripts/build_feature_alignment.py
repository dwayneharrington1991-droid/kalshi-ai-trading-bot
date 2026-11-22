"""Build the frozen L13 feature alignment from INPUT VALUES ONLY (no outcomes are read).

  python scripts/build_feature_alignment.py --reference old_slim.json --recent journal_dump.json \
      --artifact live_artifact.json --out research_artifacts/feature_alignment_20261122.json

reference: openings of the live model's training period (old journal, 2026-10-30 .. 11-12);
recent:    openings with RECENT_START <= opens_at < RECENT_END (after deployment 9a9a6ea).
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.signals_display import feature_alignment  # noqa: E402

RECENT_START = datetime(2026, 11, 20, 20, 30, tzinfo=timezone.utc)   # first opening after 9a9a6ea
RECENT_END = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)


def skewed_inputs(artifact):
    names = artifact["model"]["components"][0]["preprocessor"]["names"]
    return [n for n in names
            if n.startswith("micro_") or n in ("advanced_trade_intensity", "advanced_effective_spread")]


def feature_rows(path, start=None, end=None):
    d = json.load(open(path))
    rows, seen = [], set()
    for row in d["btc_engine_opening"]["rows"]:          # outcomes table is never opened
        ticker, payload = row[1], json.loads(row[-1])
        values = (payload.get("frozen_features") or {}).get("values") or {}
        opens = datetime.fromisoformat(str(payload["opens_at"]).replace("Z", "+00:00"))
        if ticker in seen or not values or (start and opens < start) or (end and opens >= end):
            continue
        seen.add(ticker)
        rows.append(values)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reference", required=True)
    ap.add_argument("--recent", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    artifact = json.load(open(a.artifact))
    ref = feature_rows(a.reference)
    new = feature_rows(a.recent, RECENT_START, RECENT_END)
    out = feature_alignment.build(ref, new, skewed_inputs(artifact), metadata={
        "model_id": artifact.get("model_id"), "reference_openings": len(ref), "recent_openings": len(new),
        "recent_window": [RECENT_START.isoformat(), RECENT_END.isoformat()],
        "built_from": "input values only; no outcomes"})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps({"inputs_aligned": len(out["maps"]), "reference_openings": len(ref), "recent_openings": len(new)}))


if __name__ == "__main__":
    main()
