# Pre-registered forward test v2 (frozen 2026-11-20, counts openings from 2026-11-21 00:00 UTC)

Research only. Production (`b1469ea`, model `b50b9dc30f6d664c`, threshold 0.55) is unchanged. Test v1
(`c08fb8c`, from 2026-11-20 06:15 UTC) is independent and untouched.

## Why
Research round v2 (`scripts/research_opening_v2.py`) added the live model's training-period journal
(1,186 openings, 2026-10-30 to 11-12) to the pre-freeze data: 1,452 openings in total. On the live
model's out-of-sample period (312 openings, 2026-11-16 21:15 to 11-20 06:00), the best challengers
were level with the live model, but none was credibly better:

| Model | Brier | Minus live (95% CI) | Log loss | Calib. slope | Calls | Accuracy | UP / DOWN accuracy |
|---|---|---|---|---|---|---|---|
| Live model | 0.2425 | - | 0.6781 | 1.63 | 106 | 65.1% | 59% / 68% |
| Ensemble (core16 ridge, all-87 ridge, GBM) | 0.2413 | -0.0057 to +0.0035 | 0.6755 | 1.21 | 155 | 60.0% | 56% / 62% |
| `distance_scaled` only | 0.2419 | -0.0061 to +0.0051 | 0.6784 | 1.20 | 99 | 66.7% | 52% / 73% |
| Gradient boosting (depth 2) | 0.2426 | -0.0063 to +0.0060 | 0.6782 | 1.17 | 137 | 63.5% | 65% / 63% |
| Stability-selected ridge | 0.2441 | -0.0059 to +0.0088 | 0.6814 | 0.74 | 167 | 60.5% | 56% / 62% |
| Kalshi opening mid | 0.2470 | -0.0024 to +0.0127 | 0.6875 | 0.74 | 108 | 60.2% | 51% / 66% |
| Past UP rate | 0.2478 | | 0.6888 | | - | - | - |
| Momentum (15m and 60m returns) | 0.2485 | | 0.6901 | | 80 | 51.2% | |

About ten models were tried, so the best point estimate is biased upward. Only unseen openings can
decide.

## Frozen definitions (`scripts/forward_eval_v2.py`, model code in `scripts/research_opening_v2.py`)
- Candidates: `ensemble`, `gbm`, `stable_ridge`, refit every 12 openings on every opening settled at
  least 30 minutes before.
- Only openings with `opens_at >= 2026-11-21T00:00Z` count. At least 200 are required (about
  2026-11-23 02:00 UTC).
- Pass rule: the 95% bootstrap interval of (candidate Brier minus live Brier) is entirely below zero.
  Passing makes a candidate eligible for a separate promotion review. Nothing is deployed
  automatically.
- Secondary, descriptive: accuracy of live UP/DOWN calls that the ensemble agrees with, against all
  live calls. Exploratory pre-freeze result: 68.7% on 83 confirmed calls vs 65.1% on all 106. This was
  chosen after seeing that data, so it is only a hypothesis for the forward period.
- Live-model accuracy by confidence on the pre-freeze out-of-sample period: 42.6% (|P-0.5| < 0.02,
  n=94), 58.9% (0.02–0.05, n=112), 64.4% (0.05–0.08, n=59), 66.0% (>= 0.08, n=47). The ranking is
  informative but the probabilities are too timid (calibration slope 1.63).

## Also tested in round v2 and rejected (not registered)
- Regime-aware core16 (inputs split by 24 h volatility above or below the training median): Brier 0.2430
  vs live 0.2425 (CI -0.0048 to +0.0058).
- Mean-reversion ridge on inputs selected on development data only: 0.2450 (CI -0.0047 to +0.0096).
- Platt-recalibrated core16: 0.2493 (worse).
- Momentum baseline: 0.2485 (worse than the past UP rate).
- Inference cost: linear models about 7 µs per opening; GBM a few ms. Speed is not a constraint.

## How to score v2 (after about 2026-11-23 02:00 UTC)
`powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\kalshi-bot-dev\repo\scripts\Run-ForwardEvalV2.ps1"`
