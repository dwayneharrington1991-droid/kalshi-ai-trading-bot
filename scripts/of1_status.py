"""OF1 status for the dashboard's three-model comparison (research only; run by the 3-hourly research timer).

Exports point-in-time records (export_orderflow_records.py), then for each target either reports COLLECTING DATA
(before the pre-registered eval_start, or too few scored rows) or runs the frozen evaluator and summarises
AI A (recorded production probability), AI B (+walls) and AI C (+walls+trades) on identical decisions.
Writes ~/dual-ai-alerts/of1_status.json atomically. Never changes production.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import export_orderflow_records as ex  # noqa: E402

HOME = Path.home() / "dual-ai-alerts"
SPECS = HERE.parent / "research_artifacts" / "of1"
MIN_SCORED = 100
LABELS = {"recorded_baseline": "AI A — current", "AI_B_walls": "AI B — + walls", "AI_C_walls_trades": "AI C — + walls + trades"}
TARGETS = {"opening_final": "Opening → final settlement", "first6": "FIRST 6 MIN (BTC open → minute 6)",
           "minute6_final": "MINUTE-6 → final settlement"}


def summarise(target, counts, spec_path, records_path, now):
    spec = json.loads(spec_path.read_text())
    row = {"title": TARGETS[target], **counts, "eval_start": spec["eval_start"], "eval_end": spec["eval_end"],
           "status": "COLLECTING DATA", "models": {}}
    if now < ex._ts(spec["eval_start"]) or counts["records"] < spec["min_train"]:
        row["reason"] = f"{counts['records']} point-in-time records so far; evaluation starts {spec['eval_start']}"
        return row
    import research_order_flow_value as rv
    rep = rv.evaluate(records_path, spec_path)
    scored = (rep.get("coverage") or {}).get("scored_records", 0)
    if rep["status"] != "RESEARCH_ONLY" or scored < MIN_SCORED:
        row["reason"] = f"{scored} scored decisions (need {MIN_SCORED})"
        return row
    row["status"] = "RESEARCH ONLY — NOT VALIDATED"
    row["scored"] = scored
    for name, label in LABELS.items():
        m = rep["metrics"][name]
        c = rep["comparisons"].get(name) or {}
        row["models"][name] = {"label": label, "correct": m["correct"], "wrong": m["wrong"], "wait": m["wait"],
                               "accuracy": m["accuracy"], "accuracy_ci95": m["accuracy_ci95"], "coverage": m["coverage"],
                               "brier": m["brier"], "log_loss": m["log_loss"],
                               "vs_ai_a_ci95": c.get("vs_recorded_baseline_ci95")}
    return row


def main():
    now = datetime.now(timezone.utc)
    out_dir = HOME / "of1"
    counts = ex.export(HOME / "sent.sqlite3", HOME / "orderbook_research.sqlite3", out_dir)
    status = {"generated_at": now.isoformat(), "test": "OF1 (pre-registered 2026-11-27)",
              "prospective_start": ex.START.isoformat(), "targets": {},
              "note": "Shadow research only. AI A is the unchanged production probability; nothing is promoted without approval."}
    for target in TARGETS:
        try:
            status["targets"][target] = summarise(target, counts[target], SPECS / f"spec_{target}.json",
                                                  out_dir / f"{target}.jsonl", now)
        except Exception as exc:                       # reported, never hidden
            status["targets"][target] = {"title": TARGETS[target], **counts.get(target, {}), "status": "ERROR",
                                         "reason": f"{type(exc).__name__}: {exc}"[:200], "models": {}}
    tmp = HOME / "of1_status.json.tmp"
    tmp.write_text(json.dumps(status, indent=1, default=str))
    os.replace(tmp, HOME / "of1_status.json")
    print(json.dumps({t: v["status"] + f" ({v.get('records', 0)} rec)" for t, v in status["targets"].items()}))


if __name__ == "__main__":
    main()
