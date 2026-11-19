# Live 8768 export analysis (2026-11-19)

Source files (read-only, from the Oracle export at 2026-11-19 22:19 UTC; production build `a269d08`, listener pid 1997010):
- `live8768_openings_20261119-151952.csv` (300 official openings, 2026-11-16 17:15 to 2026-11-19 22:00 UTC)
- `live8768_learning_20261119-151952.json`
- `live8768_export_20261119-151952.txt`

Nothing here changes production. All figures are computed from the CSV in chronological order.

## Scoreboard reconciliation

| Scope | Directional calls | Correct | Accuracy | 95% Wilson interval |
|---|---|---|---|---|
| Candidate model `opening-candidate/b50b9dc30f6d664c` | 90 | 58 | 64.4% | 54.1–73.6% |
| Fallback model `opening24h-zero-drift-final-average-v1` | 9 | 3 | 33.3% | 12.1–64.6% |
| All UP/DOWN rows | 99 | 61 | 61.6% | 51.8–70.6% |

The 90 candidate calls match the dashboard counter (58 correct, 32 wrong). The 99 UP/DOWN rows include 9 fallback calls, which the dashboard does not count.

## Coverage

- 300 official openings: 294 eligible, 6 ineligible (`OPENING_PROBABILITY_UNAVAILABLE`).
- Directional calls: 99 of 294 eligible = 33.7% coverage. Candidate-only: 90 of 294 = 30.6%.
- WAIT: 188 rows. UNCLEAR: 13 rows (outcome split 7 DOWN / 6 UP, no directional value).

## Losing streaks and recent trend

- Longest run of wrong directional calls: **5** (chronological).
- Accuracy of the first 50 directional calls: 66%. Accuracy of the last 40: **55%**, close to a coin flip.
- Last 20 directional results (W=correct, L=wrong): `WLWWWWLLWWWWLLLLLWLL`.

The recent decline is the most important finding. Over 300 openings the sample is too small to call it a trend, but the direction is against the model.

## Direction bias

- DOWN calls: 44 of 69 correct (63.8%).
- UP calls: 17 of 30 correct (56.7%).
- Base rate: 45% of eligible openings settled UP (132 of 294), so a model that always says DOWN would be right about 55% of the time. The DOWN edge over that baseline is small and not significant on this sample.

## Calibration

Probability bins on eligible rows (actual UP rate versus the predicted bin):

| Predicted UP probability | n | Actual UP rate |
|---|---|---|
| 0.3–0.4 | 10 | 0.30 |
| 0.4–0.5 | 163 | 0.41 |
| 0.5–0.6 | 111 | 0.51 |
| 0.6–0.7 | 10 | 0.60 |

Most probabilities sit in 0.4–0.6. Inside 0.5–0.6 the actual UP rate is about 0.51, so the model has little resolving power there. The bins are too coarse to say how the calls above the 0.55 UP threshold perform on their own; that split is not done yet.

## Regime

| Regime | Directional n | Correct | Accuracy |
|---|---|---|---|
| Rising volatility | 18 | 12 | 66.7% |
| Falling volatility | 11 | 7 | 63.6% |
| Similar volatility | 70 | 42 | 60.0% |

No regime shows a clear edge; all intervals are wide (about ±20 points).

## Learning state

- `retrain_decision`: SKIPPED_NOT_ENOUGH_NEW_DATA; new settled markets: 0 at the last cycle.
- Promotion: NOT_PROMOTED in every recent cycle. No candidate was promoted.
- History: 50 PROMOTION_BLOCKED (ACTIVATION_DISABLED, earlier) and 12 NOT_PROMOTED. History chain verified.

## What this means

- The 64.4% candidate accuracy is real on this sample, but its 95% interval starts at 54%, so it is not clearly better than a coin flip. The recent 40-call window is at 55%.
- The requested 80–90% accuracy is not supported by this data. Reaching it would need far more evidence and a different model, not a threshold change.
- Next steps that the data supports: a chronological out-of-sample test of the candidate against the fallback and a constant-DOWN baseline, using only data after the training cutoff; more calls to narrow the interval; and checking why the last 40 calls declined (feed freshness and candidate feature inputs are the first things to check).

## Not done

- No walk-forward model comparison has been run. The export has 300 rows; a chronological split leaves too few directional calls for a reliable model selection.
- The fee/spread-aware profitability check needs the executable quote fields, which are not in this export.

## Walk-forward experiment (rejected)

Rule tested: for each eligible opening after the first 60, pick a confidence threshold t from {0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.10} that maximised accuracy on openings that had already settled before this open (strict: closes_at <= opens_at), require at least 20 such calls, then call UP/DOWN from the candidate probability when |p - 0.5| >= t.

| Scope | Calls | Correct | Accuracy | 95% Wilson |
|---|---|---|---|---|
| Threshold rule (walk-forward) | 70 | 49 | 70.0% | 58.5–79.5% |
| Recorded candidate direction, same 70 openings | 70 | 46 | 65.7% | 54.0–75.8% |
| Threshold rule, first half of its calls | 35 | 27 | 77.1% | |
| Threshold rule, second half | 35 | 22 | 62.9% | |

Verdict: rejected. The paired gain is 3 calls out of 70, well inside sampling noise. The threshold is chosen from seven options on the same export, and the accuracy falls from 77% to 63% between halves. Not promoted, not deployed.

Constant-DOWN baseline over all 294 eligible openings: 162 correct, 55.1% (49.4–60.7%).
