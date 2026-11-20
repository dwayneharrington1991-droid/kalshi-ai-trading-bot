# Opening model: why the edge faded (research, 2026-11-20)

Read-only analysis of the live 8768 journal exported at 04:47 UTC on 2026-11-20 (389 openings,
388 settled, 302 with Kalshi quotes at the opening). Nothing in production was changed. No model
was promoted. All figures are out-of-sample for the live candidate `b50b9dc30f6d664c`, whose
training labels end 2026-11-12 06:30 UTC.

## Live model, chronological blocks (Brier, lower is better)

| Period | n | Model | Past-only base rate | Market mid at open | Calls (>=0.55 / <=0.45) | Call accuracy | After-cost P&L per $1 contract at the open ask |
|---|---|---|---|---|---|---|---|
| All | 306 | 0.2427 | 0.2490 | 0.2474 (n=297) | 104 (34%) | 64.4% | +0.089 (95% CI -0.010 to +0.183, n=101) |
| First half | 153 | 0.2373 | 0.2509 | 0.2473 | 45 (29%) | 73.3% | +0.200 (CI +0.066 to +0.320) |
| Second half (from 2026-11-18 ~07:30) | 153 | 0.2481 | 0.2471 | 0.2476 | 59 (39%) | 57.6% | +0.009 (CI -0.115 to +0.133) |
| Last block (from 2026-11-19 11:00) | 66 | 0.2512 | 0.2424 | 0.2546 | 33 (50%) | 54.5% | -0.018 |

Fees use Kalshi's quadratic taker fee (0.07 x P x (1-P), rounded up to the cent), bought at the
quoted ask a median 2.7 s after the open. Small samples: every interval above is wide. The first
half's positive result is not evidence that the second half will recover.

## What changed

- **Discrimination decayed while confidence rose.** Correlation between the model's P(UP) and the
  outcome by 60-opening block: +0.31, +0.23, +0.26, +0.16, +0.02. Over the same blocks the spread of
  P(UP) grew (sd 0.052 to 0.069), so coverage rose from about 30% to 50% and accuracy fell.
- **Inputs moved outside the training range.** Mean absolute standardized value of the 15 most heavily
  weighted logistic inputs rose between halves, for example `micro_30s_return_fraction` 0.39 to 0.80,
  `micro_30s_velocity` 0.33 to 0.66, and `advanced_trade_intensity`, whose contribution sd grew about
  4x (0.040 to 0.158). Larger inputs push probabilities further from 0.5 without adding information.
- **Base rate moved.** UP settled 37% of the time in the last 60 openings (about 2 SE below 50%).
  The model's mean P(UP) stayed near 0.50.

## Checks that found no defect

- Leakage: all 320 frozen feature sets have `max_input_received_at` <= cutoff == opens_at, and no
  history was received after the open.
- Labels: 0 mismatches between `forecast_outcomes` and `learning_contract_reviews`.
- The scoreboard counts only displayed UP/DOWN calls. `learning_contract_reviews` records the
  probability-implied side (including WAIT openings) for retraining. That is intended, not a scoring bug.
- Every opening is stored with status `DELAYED`, because the code marks any record generated after the
  open as DELAYED, and the threshold is only published after the open. It is a label, not a fault.

## Challengers (walk-forward, refit every opening on outcomes known before it; 169 test openings from 2026-11-18 07:30)

| Model | Brier | Diff vs live (95% CI) | Calls | Accuracy | P&L per contract |
|---|---|---|---|---|---|
| Live candidate | 0.2467 | - | 62 | 59.7% | +0.029 |
| Past-only base rate | 0.2461 | -0.011 to +0.010 | - | - | - |
| Logistic, 12 core features, C=0.1 | 0.2458 | -0.018 to +0.018 | 121 | 57.9% | +0.019 |
| Logistic, all 320 features, C=0.01 | 0.2651 | -0.006 to +0.045 | 139 | 54.7% | +0.014 |
| Gradient boosting, 12 features | 0.2571 | -0.008 to +0.029 | 125 | 57.6% | +0.021 |
| Platt recalibration of live P(UP) | 0.2466 | -0.007 to +0.007 | 104 | 57.7% | +0.020 |
| Live P(UP) shrunk halfway to 0.5 | 0.2475 | -0.004 to +0.005 | 17 | 52.9% | -0.041 |

**None is credibly better than the live model, so all are rejected.** The lifecycle's own retrain
also returns NO_CANDIDATE every cycle. That is correct with this little data.

## Recommendations (not implemented; each needs your approval)

1. Treat the live calls as unproven. Recent evidence shows no edge after costs.
2. Research scale-free inputs (returns and flow divided by recent realized volatility) so a volatility
   regime change does not inflate confidence. Validate in shadow mode only.
3. Collect more data. About 100 calls per 4 days cannot separate a 5-point accuracy edge from luck.
