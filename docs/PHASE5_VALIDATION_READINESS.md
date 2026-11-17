# Phase 5 accuracy-validation readiness

This release adds a read-only evidence layer to the approved Phase 4 research
pipeline. It does not train, promote, activate, deploy, or change a model or
threshold.

## What the report proves

`report_A_settled_audit.json` now reconciles the supplied 20-correct/9-wrong
overall claim and 19-correct/7-wrong most-recent-26 claim against immutable
opening records joined to non-conflicting official outcomes. It reports the
exact as-of timestamp, model-version cohorts, directional-call and coverage
Wilson intervals, and whether the running model is represented. A matching
arithmetic total is explicitly not treated as a single-model result when the
records contain multiple model identities.

The report also provides conservative sample planning at the worst-case 50%
rate: 93, 167, and 381 directional calls for an approximately 10, 7.5, and 5
percentage-point 95% Wilson half-width, respectively. These are precision
planning figures, not proof of model superiority. Candidate comparisons retain
the Phase 4 floors of 100 paired calls and 200 sealed-holdout contracts, while
the actual paired discordances, confidence intervals, and Holm-adjusted tests
remain decisive.

## Threshold and candidate research

The fixed 0.55 policy remains the only policy evaluated on the sealed holdout.
Alternative thresholds are evaluated on development data only. Phase 5 adds an
accuracy/coverage Pareto frontier and the change in calls and accuracy relative
to 0.55, but deliberately emits no “optimal” threshold. The six preregistered
candidates, matched-contract comparisons, matched coverage, and Holm family
remain unchanged.

## Profitability limitation

Actual profitability cannot be inferred without contemporaneous executable
pre-opening quotes (and actual fills). When such quotes are absent the report
returns `NOT_MEASURABLE`; when present it labels results counterfactual and
after-cost rather than realized. This prevents directional accuracy from being
misreported as a tradable edge.

## Readiness decision

`PASS_RESEARCH_ONLY` means the evidence run is internally complete. It never
means a candidate is promotable. `BLOCKED_FOR_PROMOTION` includes the exact
reasons, such as unreconciled claims, mixed model versions, insufficient sealed
evidence, or missing executable quotes. A future candidate still requires a
fresh chronological, sealed, identical-contract comparison and separate human
approval; the research runner has no promotion path.
