# Codex Phase 3 independent AI audit

Scope: repository-only review based on `6c19623655640b7aa5fc3906abb080e56e4161c2`
and the plan-only child `a9f0c9b7376f570e66234a54b52230ac81ecccc0`.
No Oracle journal, runtime artifact, service, or port was accessed.

## Report A: what the supplied counts establish

The supplied Report A totals reconcile exactly:

| Measure | Result |
|---|---:|
| Settled immutable opening records | 1,186 |
| Directional calls | 374 |
| Correct / wrong | 178 / 196 |
| Directional accuracy | 47.5936% |
| Wilson 95% interval | 42.58%–52.65% |
| WAIT | 812 |
| Directional coverage | 31.53% |
| WAIT rate | 68.47% |

This is not evidence of a directional advantage: 50% lies inside the interval,
and the point estimate is below 50%. Zero directional calls in the sealed
Report A holdout means Report A supplies no untouched directional-accuracy
estimate for the active policy. It does not prove that 70–90% selective
accuracy is impossible; it proves that the claim is currently unsupported.

No usable pre-open Kalshi quotes means market-baseline superiority and
after-fee profitability are **not measurable** from Report A. They must not be
inferred from spot direction or mid prices.

## Confirmed code findings

1. **The “87 features” description is stale.** `features.FEATURES` currently
   defines 536 candidate names: 22 price-core fields, 243 multi-hour candle
   fields, five opening trend/volume fields, and hundreds of microstructure and
   advanced-indicator fields. Defined does not mean observed or active.
2. **The production opening pool can be high-dimensional.** Its configured
   sets contain 22 (`price_core`), 28 (`price_technical`), 265
   (`multi_hour_candlestick`), and 536 (`all_receipt_proven`) names. Each
   retained feature also gets a missingness indicator, so coefficient count can
   approach `2F+1`. Fold-local availability and regularization reduce this,
   but 1,186 settled records do not establish that the largest family
   generalizes.
3. **Sparse feature families could be mislabeled as successful ablations.** A
   research candidate whose incremental inputs failed the 50% preprocessor
   coverage rule silently collapsed to the compact feature set. Its result
   could then be reported under “technicals”, “flow”, or another family even
   though none of that family's fields were retained. The research report now
   marks such candidates `INSUFFICIENT_INCREMENTAL_FEATURE_COVERAGE` and omits
   their misleading comparison.
4. **Historical failure diagnostics included calls that were never verified
   for display.** The top failed-call list used direction alone, while the
   official metric correctly required an allowed calibrated confidence status.
   The list now applies the same displayability and direction/probability
   consistency rules.
5. **Invalid numeric probabilities could contaminate summary metadata.** NaN
   or out-of-range values were admitted to confidence distributions because
   they are Python floats. They are now excluded and counted explicitly.
6. **The research family exceeded its preregistered limit.** Eight fitted
   candidates were present despite the plan's maximum of six. The shadow
   research family is now fixed at six. Pre-open Kalshi probability remains a
   named baseline, and volatility regime remains descriptive, avoiding extra
   post-result candidate proliferation.

## Component inventory

| Component | Implemented | Eligible to affect a fitted opening | Runtime active |
|---|---|---|---|
| Logistic regression | Yes | Yes, if its fold-local preprocessor retains inputs | Oracle artifact required to verify |
| Ridge regression | Yes | Yes, same constraint | Oracle artifact required to verify |
| Gradient-boosted stumps | Yes, bounded to 30 rounds | Shadow evaluation requires at least 500 opening labels | Oracle artifact required to verify |
| Volatility baseline | Yes | Independent baseline and possible ensemble member | Historical row labels alone cannot prove runtime use |
| Ensemble | Yes, validation-only nonnegative weights | Only selected through chronological gates | Oracle artifact required to verify weights |
| EMA / trend | Defined and captured where receipt-proven | Only if coverage survives fold-local preprocessing | Not inferred from code presence |
| RSI / MACD / VWAP / Bollinger | Defined | Same | Not inferred from code presence |
| Volume / candle patterns | Defined, including eight opening windows | Same | Not inferred from code presence |
| Order flow / CVD / order book | Defined; freshness-gated | Expected to be sparse historically | Not inferred from code presence |
| Calibration | Platt and isotonic implementations exist | Calibration data are chronologically separated | Live method requires artifact inspection |
| WAIT policy | Validation-selected symmetric threshold exists | Threshold must be frozen before untouched test | Report A holdout issued zero calls |

`opening_research.intelligence_inventory()` now reports, for the actual journal
run, configured count, observed count, half-coverage eligibility, and contracts
with any feature for each family. It explicitly refuses to claim runtime
weights from historical rows.

## Ranked improvement hypotheses

1. **Highest evidence: repair measurement coverage.** Continue immutable
   pre-open quote capture. Without it, neither market-relative skill nor
   executable after-cost edge can be established.
2. **Highest evidence: prefer compact models until ablations pass.** With only
   1,186 settled rows, allowing hundreds of inputs creates a large overfitting
   surface. Require each added family to improve chronological block log loss,
   survive Holm correction, and avoid worse calibration.
3. **Measure feature availability before fitting.** Sparse flow and
   microstructure fields must report insufficient history, not silently become
   mean-imputed or be credited for compact-model performance.
4. **Treat confidence clustering near 50–55% as information.** It is consistent
   with weak signal and calibration shrinkage. Do not sharpen it artificially.
5. **Test selective accuracy only on a sealed holdout.** A 70–90% claim needs
   both useful coverage and a credible interval. The plan's 80% claim gate
   (at least 100 calls and Wilson lower bound at least 80%) remains appropriate.
6. **Regime analysis stays exploratory** until a preregistered regime candidate
   beats the compact model across chronological blocks; otherwise it expands
   the multiple-testing surface.

## Comparison points for Report B

Report B should make these directly comparable:

- exact eligible/settled/excluded counts and exclusion reasons;
- 178/196 historical displayed-call reconciliation;
- all-opening Brier, log loss, calibration/ECE, and disjoint confidence bands;
- feature-family observed and half-coverage counts from the new inventory;
- number of candidates withheld for sparse incremental inputs;
- compact versus each active family on identical chronological contracts;
- pre-open quote coverage and market-baseline sample count;
- sealed-holdout directional count, accuracy, Wilson interval, and coverage;
- runtime artifact kind, component weights, preprocessor names, and checksum.

Until the Oracle journal and loaded artifact are read under the approved
read-only procedure, actual model weights, Report B results, and 70–90%
accuracy remain **NOT VERIFIED**.
