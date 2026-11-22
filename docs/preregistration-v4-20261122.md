# Pre-registration v4: live model with quantile-aligned inputs (frozen 2026-11-22 03:30 UTC)

Why: regression ledger L13 / `docs/diagnosis-20261122-input-skew.md`. Since deployment 9a9a6ea the 30
micro/flow inputs are computed over complete windows and sit 2-6 SD from the live model's training data.
Reverting the healthy feed fixes is not acceptable, and retraining needs post-fix outcomes that are all
reserved for tests v1-v3 until they decide. Alignment repairs the input mismatch without any outcome.

- **Alignment**: `research_artifacts/feature_alignment_20261122.json`, built by
  `scripts/build_feature_alignment.py` from input values only: reference = 1,127 training-period openings
  (2026-10-30 .. 11-12, old journal), recent = 122 openings from 2026-11-20 20:30 to 2026-11-22 03:00.
  Each input: value -> quantile among recent inputs -> same quantile of training-period present values.
- **Outcome-free check (in-sample window)**: logistic component mean P(UP) 0.612 -> 0.531 (training
  0.473, pre-fix live 0.504); ensemble mean P(UP) 0.516 -> 0.494 (training 0.490); UP/DOWN calls 38/18 ->
  21/25 of 122; mean |z| of the 30 inputs 1.65 -> 0.65 (training 0.41).
- **Candidate**: `aligned` = live artifact `b50b9dc30f6d664c`, no refit, on aligned inputs.
- **Data**: openings with `opens_at >= 2026-11-22 03:30 UTC` (after the alignment window), immutable live
  opening records with an official outcome.
- **Decision**: at >= 200 forward openings (about 2026-11-24 05:30 UTC). Passes only if the 95% bootstrap
  CI (3000, seed 5) of Brier(aligned) - Brier(live) lies entirely below 0. Accuracy at 0.55, log loss,
  mean P(UP) and UP/DOWN splits reported, not decisive.
- A pass is not a deployment: production changes need your explicit approval.

Score (Windows):

    powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\kalshi-bot-dev\repo\scripts\Run-ForwardEvalV4.ps1"
