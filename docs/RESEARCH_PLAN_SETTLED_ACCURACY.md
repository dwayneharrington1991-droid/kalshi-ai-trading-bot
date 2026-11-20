# Read-only research plan: settled-market opening accuracy

Status: **PLAN ONLY. No results are reported here.** Every number below is a
pre-registered threshold or rule, not a measurement. Results come only from
running the read-only command against the Oracle journals.

Scope: this branch (`claude/research-plan-readonly`) holds the plan only. The
deployment release is `claude/final-candle-integration` @ `6c19623`, and
nothing here changes it.

## 0. Ground rules (enforced, not aspirational)

| Rule | How it is enforced |
|---|---|
| Read-only | Journals opened `mode=ro` via SQLite URI. Code runs from a `git archive` temp dir. No service process is touched. |
| No trading | The research code imports no order client. `execution_capability` stays `False`. |
| No history rewrite | Immutable openings (`btc_engine_opening`, protected by UPDATE/DELETE triggers) are read, never written. |
| No leakage | Each feature must have `max_input_received_at <= opens_at`. Labels are purged by `label_available_at`. Fold-local preprocessing. |
| No silent activation | No artifact is written into any service path. Promotion needs the separate, explicit approval gate (section 6). |
| No fabrication | A section with too little data reports `INSUFFICIENT_DATA` with its count. It never extrapolates. |

## 1. Questions, in priority order

1. **Live settled accuracy.** How often was the frozen opening call shown to
   the user correct on settled contracts? This is the call as recorded at the
   time, not a re-prediction.
2. **Calibration.** When the opening said p, did UP happen at rate p?
3. **Chronological out-of-sample.** Under a strict walk-forward, does any
   candidate beat the baselines on data it never saw?
4. **Baselines.** The question is beating trivial predictors, not 50%.
5. **Fee-adjusted hypothetical P&L.** The same calls, priced at the real
   executable ask, with fees and slippage included.
6. **Feature families.** Do EMA, trend, volume, candle and order-flow
   features add out-of-sample information? Each is tested only where its
   history exists.

## 2. Data sources and eligibility

| Source | Used for | Eligibility gate |
|---|---|---|
| `btc_engine_opening` (8768, 8765, 8767 journals, largest first) | Q1, Q2 (the actual displayed opening) | Settled outcome present; `data_cutoff == opens_at`; frozen features with 1,440 bars |
| Settlement records | Labels | Official result only; `label_available_at` recorded |
| `opening_input_snapshots` | Feature reconstruction for Q3 and Q6 | Receipt-proven, `received_at <= opens_at` |
| `opening_quote_snapshots` (from −60 s to +120 s) | Q5 executable asks | First non-stale ask within 60 s after the call |
| `predictions` compact payload | Q5 fallback for older rows | Labels `fee_multiplier_assumed=true` when the fee was not recorded |
| Recorded candles (`record_candles`) | Q6 retrospective rows | `first_received_at <= opens_at`, 1,440 contiguous bars |

Coverage must be reported first: eligible contracts per source, the date
range, and the number rejected per gate with the reason. The tool's
`authoritative_dataset_coverage` already does this.

## 3. Metrics

- **Accuracy** on directional calls only, with a Wilson 95% interval. WAIT
  rate is reported separately. Coverage-accuracy at thresholds 0.50 to 0.90
  (the existing `threshold_table`).
- **Proper scores:** Brier and log loss on all openings, including WAIT.
  These cannot be gamed by abstaining.
- **Calibration:** reliability bins (10 equal-count bins) and ECE. A
  calibration slope and intercept come from a logistic recalibration fit,
  where slope 1.0 means calibrated. The fit is reported only, never applied.
- **High-confidence band:** accuracy where displayed confidence ≥ 0.80. The
  Wilson interval must be shown. **No claim of "80–90% accurate" unless the
  Wilson lower bound ≥ 0.80 with n ≥ 100.**

## 4. Protocol

1. **Lock a final holdout first.** The most recent 20% of eligible settled
   contracts, by `opens_at`, is sealed before any exploration. It is read
   once, at the end.
2. **Walk-forward on the remaining 80%.** Start after 170 contracts, in
   30-contract blocks. Training uses only contracts whose label time is before
   the test block's first opening. Preprocessing is fit per fold.
3. **Baselines** run on exactly the same contracts:
   - B0: always UP (the base rate).
   - B1: the Kalshi pre-open mid, as an implied probability. This is the
     market baseline; beating it is the real bar.
   - B2: the zero-drift Gaussian fallback (the current displayed fallback).
   - B3: the current live artifact's recorded probabilities.
   - B4: last contract's direction (persistence).
4. **Statistical tests.** A paired HLN-corrected Diebold–Mariano test on
   per-contract log-loss differences against B1 and B3. Holm step-down across
   every candidate × baseline pair at family alpha 0.05.
5. **Pre-registered candidate count ≤ 6.** Adding candidates after seeing
   results counts against the family, and the count is reported.

## 5. Feature-family assessment (Q6)

Each family is added to a fixed, small base. It is evaluated only where its
history meets the minimum count, otherwise it is reported
`INSUFFICIENT_HISTORY` with its n.

| Family | Features (existing names) | History available from | Minimum settled contracts |
|---|---|---|---|
| Trend / EMA | `OPENING_TREND_FEATURES` (`opening_ema*`) | All rows with 1,440 bars | 300 |
| Volume | `opening_volume_acceleration`, `relative_volume` | All rows with 1,440 bars | 300 |
| Candle structure | `candle_*`, `body_fraction` | All rows with 1,440 bars | 300 |
| Order flow | `spot_cvd_delta_fraction`, `spot_book_dominance`, pre-open flow | Only since pre-open flow capture shipped (recent) | 300; expect `INSUFFICIENT_HISTORY` initially |
| Kalshi quote | pre-open mid, spread | Only since `opening_quote_snapshots` | 300 |

A family "adds information" only if all of these hold:
- the Holm-adjusted DM test passes against the base without it;
- log loss improves on ≥ 60% of walk-forward blocks;
- the ECE does not get worse by more than 0.01.

Exploratory tercile tables (`condition_report`) are labelled EXPLORATORY and
never used for selection.

## 6. Fee-adjusted hypothetical profitability (Q5)

- Each directional call is priced at the executable ask of its side:
  `cost = ask + quantity_fee(ask, 1, fee_multiplier) + 0.01 slippage`.
  The payout is 1 if correct, else 0.
- Report per call: mean P&L, its 95% bootstrap interval (block bootstrap by
  day), hit rate versus break-even price, and max drawdown. Results are split
  by fee recorded or assumed.
- Calls with no executable ask in the window are **excluded and counted**,
  never filled at mid.
- Labelled **HYPOTHETICAL**. It is not a trade record and assumes 1-contract
  fills at the top of book.

## 7. Model activation decision rule (no automatic activation)

A replacement opening model may be proposed (never auto-activated) only if,
on the sealed holdout:

1. Holm-adjusted DM shows significantly lower log loss than **both** B1
   (market) and B3 (current live);
2. ECE ≤ the current live model's ECE;
3. fee-adjusted hypothetical P&L has a bootstrap lower bound > 0;
4. n ≥ 200 holdout contracts.

Otherwise the current model stays. The decision and the evidence go to the
user for a manual go/no-go. Visible confidence and thresholds are never
changed to look more certain.

## 8. How to run (read-only)

Use the existing one-paste command in `docs/ORACLE_READONLY_RESEARCH.md`
(it runs `deploy/oracle/research_readonly.sh` with `--intracontract
--retrospective`). It reports coverage, recorded-decision metrics,
calibration, walk-forward versus baselines with Holm, fee-adjusted results,
and per-family conditions.

### Gaps in the current tool, to add on this branch before the run

These are research-only and never ship in the deployment release.

1. Sealed final-holdout split (section 4.1). Today the holdout is inside
   `research()` but is not sealed before exploration.
2. B1 market-mid and B4 persistence baselines as named comparators.
3. HLN-corrected DM. The existing paired test should be checked against the
   HLN small-sample correction.
4. Day-block bootstrap interval on hypothetical P&L.
5. Per-family `INSUFFICIENT_HISTORY` gating with the minimum counts above.

## 9. Output and reporting

- A JSON report with SHA-256 of each journal read, the code commit, and the
  run timestamp.
- Every claim cites its n and interval. Sections below minimum n say so
  explicitly.
- If the Oracle journals are unavailable, the report states that nothing was
  measured. No placeholder numbers are inserted.
