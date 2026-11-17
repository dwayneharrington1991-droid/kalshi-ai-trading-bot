"""Read-only diagnostics of the deployed opening artifact. RESEARCH ONLY.

Answers, from the artifact file and the journal's eligible frozen openings:

* is every ensemble component loadable and producing finite probabilities;
* which of the model's input features are actually present at opening time
  (an absent input contributes only through mean imputation and its missing
  indicator, i.e. it is effectively silent);
* how much the ensemble probability moves when each input or each component
  is removed (drop-one sensitivity on real recorded inputs);
* whether the recorded live probability is reproduced from the frozen inputs;
* how the model scored on openings after its own label cutoff (out-of-sample
  for this artifact), against its statistical baseline on the same contracts.

The artifact is only read. Nothing is retrained, rewritten or activated.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from . import opening_models, rigorous_evaluation as rigor
from .model import timestamp
from .scores import score

DIAGNOSTICS_FORMAT = "kxbtc-opening-artifact-diagnostics-v1"
SENSITIVITY_ROWS = 300
SILENT_COVERAGE = 0.5


def _inputs(model):
    return list((model.get("preprocessor") or {}).get("names") or [])


def _components(model):
    if model.get("kind") == "opening_ensemble":
        return list(zip(model["mixture_weights"], model["components"]))
    return [(1.0, model)]


def _predict(model, row, features=None):
    return opening_models.predict(model, features if features is not None else row["features"],
                                  baseline_probability=row["baseline"])


def _mix(parts, weights):
    total = sum(weights)
    return sum(w * p for w, p in zip(weights, parts)) / total if total else None


def diagnose(artifact, rows):
    model = artifact["model"]
    parts = _components(model)
    union = sorted({name for _, member in parts for name in _inputs(member)})
    structure = [{
        "kind": member.get("kind"), "feature_set": member.get("feature_set"),
        "inputs": len(_inputs(member)), "mixture_weight": weight,
        "active": weight > 0,
    } for weight, member in parts]
    report = {
        "format": DIAGNOSTICS_FORMAT,
        "model_id": artifact.get("model_id"),
        "model_kind": model.get("kind"),
        "components": structure,
        "component_count": len(parts),
        "active_components": sum(weight > 0 for weight, _ in parts),
        "input_feature_count": len(union),
        "rows_examined": len(rows),
    }
    if not rows:
        report["status"] = "NO_ELIGIBLE_FROZEN_OPENINGS"
        return report

    # Operational: every component yields a finite probability on real inputs.
    per_component, errors = [[] for _ in parts], [0] * len(parts)
    ensemble = []
    for row in rows:
        values = []
        for index, (_, member) in enumerate(parts):
            try:
                p = _predict(member, row)
                if not math.isfinite(p) or not 0 <= p <= 1:
                    raise ValueError("non-finite probability")
            except (ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError):
                errors[index] += 1
                p = None
            values.append(p)
            if p is not None:
                per_component[index].append(p)
        ensemble.append(_predict(model, row))
    for index, entry in enumerate(structure):
        ps = per_component[index]
        mean = sum(ps) / len(ps) if ps else None
        entry.update(
            prediction_errors=errors[index],
            mean_probability=mean,
            probability_std=math.sqrt(sum((p - mean) ** 2 for p in ps) / len(ps)) if ps else None,
            share_within_0_45_0_55=sum(0.45 <= p <= 0.55 for p in ps) / len(ps) if ps else None,
            correlation_with_ensemble=_corr(ps, ensemble) if len(ps) == len(ensemble) else None,
        )
    mean_e = sum(ensemble) / len(ensemble)
    report["ensemble_output"] = {
        "mean_probability": mean_e,
        "probability_std": math.sqrt(sum((p - mean_e) ** 2 for p in ensemble) / len(ensemble)),
        "share_within_0_45_0_55": sum(0.45 <= p <= 0.55 for p in ensemble) / len(ensemble),
        "min": min(ensemble), "max": max(ensemble),
    }

    # Input coverage at opening time.
    coverage = {name: sum(type(r["features"].get(name)) in (int, float)
                          and math.isfinite(r["features"][name]) for r in rows) / len(rows)
                for name in union}
    report["input_coverage"] = {
        "silent_inputs_below_50pct": sorted(n for n, c in coverage.items() if c < SILENT_COVERAGE),
        "never_present": sorted(n for n, c in coverage.items() if c == 0),
        "present_in_every_row": sum(c == 1 for c in coverage.values()),
        "by_feature": dict(sorted(coverage.items())),
    }

    # Drop-one sensitivity on the most recent rows (real recorded inputs).
    sample = rows[-SENSITIVITY_ROWS:]
    base = [_predict(model, r) for r in sample]
    sensitivity = {}
    for name in union:
        moved = []
        for row, p in zip(sample, base):
            if row["features"].get(name) is None:
                moved.append(0.0)
                continue
            dropped = {k: v for k, v in row["features"].items() if k != name}
            moved.append(abs(_predict(model, row, dropped) - p))
        sensitivity[name] = sum(moved) / len(moved)
    ranked = sorted(sensitivity.items(), key=lambda kv: -kv[1])
    report["feature_sensitivity"] = {
        "method": "mean |p(ensemble) - p(ensemble with the input removed)| over recent eligible openings",
        "rows": len(sample),
        "top_20": ranked[:20],
        "no_effect_inputs": sorted(n for n, v in sensitivity.items() if v < 1e-9),
        "inputs_moving_probability_over_0_5pp": sum(v >= 0.005 for v in sensitivity.values()),
    }
    if len(parts) > 1:
        weights = [w for w, _ in parts]
        comps = [[_predict(member, r) for _, member in parts] for r in sample]
        drop = {}
        for index, (_, member) in enumerate(parts):
            kept = [w if j != index else 0.0 for j, w in enumerate(weights)]
            moved = [abs((_mix(c, kept) if sum(kept) else p) - p) for c, p in zip(comps, base)]
            drop[f"{index}:{member.get('kind')}"] = sum(moved) / len(moved)
        report["component_drop_sensitivity"] = drop

    # Reproduction of recorded live probabilities from frozen inputs. Only an
    # opening the live engine labelled as made by THIS artifact can be expected
    # to reproduce; openings made by the statistical fallback (e.g. while a
    # restarted process had not yet re-loaded the artifact) or by another
    # artifact are counted separately instead of being reported as failures.
    expected_model = "opening-candidate/" + str(artifact.get("model_id"))
    checked, other_models, mismatches = [], {}, []
    for row, p in zip(rows, ensemble):
        recorded = row.get("recorded_up")
        if type(recorded) not in (int, float):
            continue
        made_by = row.get("recorded_model") or "UNRECORDED"
        if made_by != expected_model:
            other_models[made_by] = other_models.get(made_by, 0) + 1
            continue
        difference = abs(recorded - p)
        checked.append(difference)
        if difference > 1e-6:
            mismatches.append({"ticker": row["ticker"], "opens_at": row.get("opens_at"),
                               "recorded_up": recorded, "recomputed_up": p, "difference": difference})
    report["reproduction"] = {
        "expected_recorded_model": expected_model,
        "openings_made_by_this_artifact": len(checked),
        "exactly_reproduced_within_1e_6": sum(d <= 1e-6 for d in checked),
        "max_abs_difference": max(checked) if checked else None,
        "mismatches": sorted(mismatches, key=lambda m: -m["difference"])[:20],
        "openings_made_by_other_models": other_models,
        "reading": ("A mismatch on an opening made by this artifact means the stored frozen inputs or "
                    "baseline do not reproduce the live call: a real defect to investigate. Openings "
                    "made by other models are not expected to reproduce."),
    }

    # Out-of-sample for this artifact: openings after its label cutoff.
    try:
        cutoff = timestamp(artifact["labels_available_through"])
        oos = [(p, r) for p, r in zip(ensemble, rows) if timestamp(r["opens_at"]) > cutoff]
    except (KeyError, TypeError, ValueError, AttributeError):
        cutoff, oos = None, []
    entry = {"labels_available_through": artifact.get("labels_available_through"), "contracts": len(oos)}
    if len(oos) >= 2:
        y = [r["outcome"] for _, r in oos]
        model_p = [p for p, _ in oos]
        base_p = [r["baseline"] for _, r in oos]
        entry.update(
            artifact={k: score(model_p, y)[k] for k in ("accuracy", "accuracy_interval", "brier", "log_loss",
                                                        "expected_calibration_error")},
            statistical_baseline={k: score(base_p, y)[k] for k in ("accuracy", "brier", "log_loss")},
            artifact_better_log_loss=rigor.paired_superiority_test(
                rigor._log_losses(model_p, y), rigor._log_losses(base_p, y)),
        )
    report["out_of_sample_for_this_artifact"] = entry
    report["status"] = "DIAGNOSED"
    return report


def _corr(a, b):
    n = len(a)
    if n < 2:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def summary_lines(report):
    lines = [f"MODEL {report.get('model_id')} kind={report.get('model_kind')} components={report.get('component_count')} "
             f"active={report.get('active_components')} inputs={report.get('input_feature_count')} rows={report.get('rows_examined')}"]
    for c in report.get("components", []):
        lines.append(f"  component {c['kind']} set={c.get('feature_set')} inputs={c['inputs']} weight={c['mixture_weight']:.3f} "
                     f"errors={c.get('prediction_errors')} std={c.get('probability_std')} corr={c.get('correlation_with_ensemble')}")
    cov = report.get("input_coverage") or {}
    if cov:
        lines.append(f"  inputs silent(<50% present)={len(cov['silent_inputs_below_50pct'])} never_present={len(cov['never_present'])}")
    sens = report.get("feature_sensitivity") or {}
    if sens:
        lines.append(f"  inputs with no effect={len(sens['no_effect_inputs'])} moving>0.5pp={sens['inputs_moving_probability_over_0_5pp']} "
                     f"top={[(n, round(v, 4)) for n, v in sens['top_20'][:5]]}")
    if report.get("component_drop_sensitivity"):
        lines.append(f"  component drop sensitivity={ {k: round(v, 4) for k, v in report['component_drop_sensitivity'].items()} }")
    rep = report.get("reproduction") or {}
    if rep:
        lines.append(f"  reproduction {rep['exactly_reproduced_within_1e_6']}/{rep['openings_made_by_this_artifact']} "
                     f"max_diff={rep['max_abs_difference']} other_models={rep['openings_made_by_other_models']}")
        for m in rep["mismatches"][:5]:
            lines.append(f"    mismatch {m['ticker']} recorded={m['recorded_up']:.4f} recomputed={m['recomputed_up']:.4f}")
    oos = report.get("out_of_sample_for_this_artifact") or {}
    if oos.get("contracts", 0) >= 2:
        lines.append(f"  OOS n={oos['contracts']} artifact brier={oos['artifact']['brier']:.4f} baseline brier={oos['statistical_baseline']['brier']:.4f} "
                     f"p(artifact better logloss)={oos['artifact_better_log_loss']['p_value']:.3f}")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asof")
    args = parser.parse_args()
    try:
        rigor._refuse_foreign_file(args.output, DIAGNOSTICS_FORMAT)
        asof = timestamp(args.asof) if args.asof else datetime.now(timezone.utc)
        artifact = json.loads(args.artifact.read_text())
        _loaded, load_reason = opening_models.load_artifact(args.artifact)
        from .opening_research import load

        with closing(sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = load(db, asof)
        report = diagnose(artifact, rows)
        report["artifact_path"] = str(args.artifact)
        report["loads_with_this_code"] = _loaded is not None
        report["load_reason"] = load_reason
        from .model_registry import atomic_json

        atomic_json(args.output, report)
        print("MODEL_DIAGNOSTICS_BEGIN")
        for line in summary_lines(report):
            print(line)
        print("MODEL_DIAGNOSTICS_END")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
