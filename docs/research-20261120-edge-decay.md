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
- **Inputs became larger than in training** (computed with the model's own transform, where a missing
  input is neutral plus a missing flag). The logistic component's logit spread grew from 0.42 to 0.56
  between halves. Largest contributors: `distance_scaled` (mean |z| 0.57 to 1.15), the 5 s, 15 s and
  30 s micro returns and velocities (about 0.4 to 0.7), and `return_30m`. `distance_scaled` stayed
  predictive (correlation with UP +0.18 early, +0.16 late). The micro inputs lost most of their
  relationship with the outcome, so their larger values added confidence without information.
- **Missing inputs.** The microstructure stream is often empty at the open: 169 of 320 openings lacked 10 or
  more of the 87 inputs (04:00 to 04:30 today: 28 to 30 missing, on both builds). The model handles this
  correctly (neutral value plus a missing flag), but those openings rely on fewer real inputs.
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
| Volatility-scaled returns + flow, logistic C=0.05 | 0.2554 | -0.007 to +0.025 | 106 | 55.7% | - |
| `distance_scaled` only, logistic | 0.2393 | -0.023 to +0.009 | 115 | 62.6% | - |
| `distance_scaled` + `return_30m` | 0.2419 | -0.022 to +0.014 | 123 | 61.8% | - |

**None is credibly better than the live model, so all are rejected.** The one-input `distance_scaled`
model has the best point estimate, but its interval includes zero, and it was the best of several
variants tried, so its advantage is probably overstated. It is the most promising shadow candidate. The lifecycle's own retrain
also returns NO_CANDIDATE every cycle. That is correct with this little data.

## Recommendations (not implemented; each needs your approval)

1. Treat the live calls as unproven. Recent evidence shows no edge after costs.
2. A first scale-free challenger (returns divided by 1h realized volatility) was worse than the live
   model (table above). A rebuilt full ensemble with clipped or rank-transformed inputs is the next
   experiment. It must run in shadow mode until it beats the live model out of sample.
3. Collect more data. About 100 calls per 4 days cannot separate a 5-point accuracy edge from luck.

## Follow-up (06:00 UTC export, 392 settled openings)

Evaluated on the same 113 openings (2026-11-18 22:30 to 2026-11-20 05:30, UP rate 45%). Each prediction
uses only openings settled before it.

| Model | Brier | Diff vs live (95% CI) | 0.55-rule trades | P&L per contract |
|---|---|---|---|---|
| Live candidate | 0.2489 | - | 46 | +0.058 |
| Past-only base rate | 0.2488 | -0.013 to +0.013 | 32 | +0.029 |
| `distance_scaled` only | 0.2442 | -0.025 to +0.017 | 84 | +0.007 |
| Live price_core 16 inputs, strong L2 | 0.2390 | -0.032 to +0.013 | 87 | -0.001 |
| All 87 inputs + missing flags, strong L2 | 0.2436 | -0.021 to +0.011 | 78 | +0.017 |
| Chosen at each opening from past results only | 0.2404 | -0.032 to +0.015 | 89 | -0.003 |

- Choosing the challenger at each opening from past results only removes the "best of many" bias.
  It lowers Brier but does not improve P&L after costs. No candidate is credibly better.
- Kalshi's opening price vs the live model, refit before each opening (n=181): the market price alone
  scored 0.2435 and the market price plus the live model 0.2497 (difference CI -0.010 to +0.023).
  The live model adds no measurable information beyond the price you would pay.
- An edge-based rule (trade only when the model probability beats the ask plus fee by 3 cents) did
  not help any model. The live model went from +0.058 to +0.018.
- Next step: the pre-registered forward test in docs/preregistration-20261120.md (frozen 06:15 UTC,
  decision after 200 new openings).
