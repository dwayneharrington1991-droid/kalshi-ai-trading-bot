# Pre-registration v5: retrain on consistently computed (post-fix) inputs — protocol fixed 2026-11-22

Why: L13/L16. Every model so far was trained on stream inputs computed by the pre-9a9a6ea reset-window
code; production now computes them over full windows (and R10 would change them again). The only
lasting cure is a model trained and served on one computation. Clean labelled data exists only from
2026-11-20 20:30 UTC, and every such opening is reserved for forward tests v1-v4 until they decide, so
retraining now would contaminate them. This document fixes the recipe BEFORE any of that data is used.

## Data
- Training: openings with `stream_computation == "full-window-v1"`
  (`opening_models.dataset(db, asof, stream_computation="full-window-v1")`), opens_at in
  [2026-11-20 20:30, T_TRAIN_END), T_TRAIN_END = 2026-11-24 06:00 UTC (after test v4's ~200 openings,
  when v1-v4 have all decided). About 320 openings.
- If R10 is enabled in production before T_TRAIN_END, the computation changes again; v5 then trains
  only on openings after that switch and T_TRAIN_END moves to switch + 4 days.
- Forward holdout: openings with opens_at >= T_TRAIN_END + 30 min, never used for any choice.
  Decision at >= 300 openings (about 3 days).

## Candidates (fixed now; no other variants will be counted)
1. `post_ridge`: ridge-logistic on all 87 inputs + missing flags, standardised on the training rows,
   lambda chosen by 5-fold chronological inner CV on training rows only (grid 1, 3, 10, 30, 100).
2. `post_gbm`: HistGradientBoosting depth 2, 150 iterations, learning rate 0.05, min leaf 20.
3. `post_ensemble`: mean of 1 and 2.
4. `dist`: logistic on `distance_scaled` only (training rows) — the strongest simple model pre-fix.
All are refit every 48 openings on all eligible openings settled before each refit (walk-forward).

## Decision rules (prespecified)
- Primary (Brier): a candidate passes if the 95% bootstrap CI (3000, seed 5) of
  Brier(candidate) - Brier(live) lies entirely below 0 AND Brier(candidate) < Brier(Kalshi mid).
- Accuracy/coverage (Goal A): calls = the candidate's most confident openings at a prespecified
  coverage of 33% (top third by |P - 0.5|, ranked using thresholds fixed from training rows only).
  Report accuracy with a Wilson 95% CI. 80% is a research target, not a pass condition; a claim of
  >= 80% requires the CI's lower bound >= 0.70 at >= 30% coverage over >= 300 openings.
- Calibration (Goal B): calibration slope and reliability bins reported; confidence shown to users
  must come from out-of-sample calibration only.
- Holm correction across the 4 candidates.
- A pass permits a guarded deployment proposal (shadow first, then explicit approval); never automatic.
