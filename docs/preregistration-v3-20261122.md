# Pre-registration v3: live model without the post-9a9a6ea input skew (frozen 2026-11-22 01:00 UTC)

Why: `docs/diagnosis-20261122-input-skew.md`. Evaluator: `scripts/forward_eval_v3.py` (frozen by the commit
that adds this file; the runner refuses to score if it changes). Tests v1 (`c08fb8c`) and v2 (`2d4d007`)
are unchanged and independent.

- **Data**: openings with `opens_at >= 2026-11-22 01:00 UTC` from the live 8768 journal, immutable live
  opening records with an official outcome. No refit; nothing after each opening is used.
- **Candidates**: `masked` (live ensemble, the 30 skewed `micro_*`, `advanced_trade_intensity`,
  `advanced_effective_spread` inputs treated as missing) and `no_logistic` (live ensemble without its
  logistic component, weights renormalised).
- **Comparator**: the live model's stored P(UP) (`b50b9dc30f6d664c`).
- **Decision**: at >= 200 forward openings (about 2026-11-24 03:00 UTC). A candidate passes only if the
  95% bootstrap CI (3000 resamples, seed 5) of Brier(candidate) - Brier(live) lies entirely below 0.
  Accuracy at the 0.55 threshold, log loss and UP/DOWN splits are reported, not decisive.
- **A pass is not a deployment**: any production change needs your explicit approval.
- No outcome after 2026-11-20 06:00 was used to choose these candidates.

How to score (Windows):

    powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\kalshi-bot-dev\repo\scripts\Run-ForwardEvalV3.ps1"
