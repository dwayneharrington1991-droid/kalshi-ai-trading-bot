# Accuracy versus coverage on non-reserved unseen openings (2026-11-22)

312 walk-forward openings, 2026-11-16 21:15 .. 2026-11-20 06:00 UTC (before every forward-test freeze),
UP rate 0.439. Each model's own most-confident share of openings (by |P - 0.5|); Wilson 95% CI.
Script: research/r3/coverage.py (local). About a dozen models are compared, so the best cell is optimistic.

| Model | top 10% | top 20% | top 33% | top 50% | all |
|---|---|---|---|---|---|
| live (b50b9dc) | 21/31 = 67.7% (50-81) | 43/62 = 69.4% (57-79) | 67/103 = 65.0% (55-74) | 103/156 = 66.0% (58-73) | 175/312 = 56.1% |
| ensemble (v2) | 64.5% | 61.3% | 61.2% | 60.3% | 189/312 = 60.6% |
| gbm (v2) | 67.7% | 64.5% | 65.0% | 60.9% | 58.7% |
| distance only | 22/31 = 71.0% (53-84) | 66.1% | 66.0% | 63.5% | 59.9% |
| 50/50 blend | 64.5% | 66.1% | 65.0% | 60.9% | 59.6% |
| Kalshi mid at open | 60.0% | 52.5% | 58.0% | 59.9% | 55.8% |
| base rate | 61.3% | 59.7% | 57.3% | 57.1% | 56.1% |

Findings
- No model reaches 80% at any coverage; the best cells are 66-71% with lower CI bounds near 50-57%.
- Accuracy rises with confidence for the live model, but its probabilities are under-dispersed:
  P 0.43 -> 33% UP observed, P 0.52 -> 46% UP (DOWN side stronger than stated, UP side weaker).
- Kalshi's opening price is not a strong forecaster here either (Brier 0.2470 vs base rate 0.2478).
- 80% verified accuracy at meaningful coverage is not supported by any data available today. The next
  credible step is v5 (`docs/preregistration-v5-retrain-20261122.md`): a model trained and served on one
  input computation, judged on a fresh 300-opening holdout.
