# Independent AI research review (Codex)

Scope: review of `claude/accuracy-research` at `f7d1672`, excluding Oracle
deployment, HTTP 503 handling, and candle ingestion owned by Claude. This
review changes no live threshold, artifact, registry, journal, or service.

## Confirmed defects repaired

1. **Post-open feature leakage:** the research-only `kalshi_preopen_mid_logit`
   used the immutable forecast generation time. A delayed forecast could
   therefore select a quote received after the contract opened. It now uses
   the recorded market opening as its hard upper bound.
2. **Unbounded quote age:** the same feature could select an arbitrarily old
   quote. It now requires a quote from the 60 seconds immediately before the
   opening.
3. **Inner-CV distribution leakage:** L2 selection standardized every early
   fold with the full later training window. Each inner fold now fits its
   preprocessor on that fold's past-only fitting rows. The final model still
   fits its preprocessor on the complete outer training partition.
4. **Sparse-coverage comparison crash:** a candidate with partial Kalshi-price
   coverage was compared to baseline rows outside its coverage and could raise
   `KeyError`. Paired tests now use the exact common holdout contracts.
5. **Malformed execution quotes:** invalid snapshot asks could reach the fee
   and P&L calculation. They are now rejected rather than interpreted as an
   executable price.
6. **Historical-call overstatement:** the research report's probability
   threshold table is counterfactual, not the record of calls actually shown.
   The report now separately scores only immutable, explicitly calibrated
   UP/DOWN decisions and labels the threshold table's scope.
7. **Scoreboard status allowlist:** an unknown or missing confidence status
   previously counted as a verified call. Only explicitly calibrated statuses
   now qualify.

## What the code establishes—and does not

- The current live artifact uses only the feature names frozen in that
  artifact. Newly collected EMA/trend/volume fields do not affect it until a
  separately trained candidate passes chronological validation and promotion.
- Feature calculations being present is not evidence of predictive value.
  `opening_research` tests candidate groups with expanding walk-forward folds,
  label purging, a development-only threshold choice, and a later holdout.
- The statistical opening baseline naturally stays near 50% when distance to
  strike is small relative to forecast uncertainty. Probabilities around
  51–54% may therefore reflect weak signal rather than a display defect.
- No production Oracle journal or trained artifact is present in this
  execution environment. Real sample counts, real confidence-band accuracy,
  and after-cost profitability are **not verified by this review**. The CLI now
  includes `authoritative_dataset_coverage` so an Oracle-side read-only run
  distinguishes official settlements from eligible frozen openings.
- Retrospective candle reconstructions remain explicitly separate from live
  immutable calls. They cannot be presented as forward performance.

## Required evidence before integration or promotion

Run the read-only research command against the authoritative Oracle journal and
retain the generated report. Review:

- `authoritative_dataset_coverage` and exclusion reasons;
- `recorded_live_openings.actual_displayed_decisions`;
- holdout metrics and Wilson intervals, not development metrics;
- Holm-adjusted comparisons against both the statistical baseline and the
  existing bot;
- executable-quote coverage and the count using an assumed fee multiplier;
- confidence-band sample sizes and calibration from the rigorous protocol.

No candidate should be activated from this research branch. Automated trading
remains outside the code path and prohibited.
