# Micro-scalp simulator — evaluation results (run 2)

> **Superseded by run 3:** see [`MICRO_SCALP_RESEARCH.md`](MICRO_SCALP_RESEARCH.md) (200 historical contracts, full fresh recording, candidate-strategy study). This file keeps the run-2 results for reference.

**PAPER SIMULATION ONLY.** No order was placed. Simulated results are not live
profitability, and none of the numbers below is evidence of a live edge.

- Code: branch `feature/btc-micro-scalp-paper-sim`, starting from `5cd341f`.
- Run: 2026-11-19 04:19 UTC. Data collection was still running and was not interrupted.
- Account: $1,000 simulated, 1% worst-case risk per trade, Kalshi taker fees on
  every fill, 1.0s latency, 2.0s immediate-or-cancel window, ±1¢ limit tolerance,
  25% liquidity participation, plus 1¢ extra slippage on historical fills.
- Reproduce: `python -m src.micro_scalp.backtest --historical data/micro_scalp/historical --fresh <snapshot>.jsonl --out report.json`,
  then `python -m src.micro_scalp.report_md report.json`.

## Verdict: **FAIL** (for trading readiness)

| Area | Result |
|---|---|
| Tests | **PASS**: 54/54 micro-scalp tests (the original 50 plus 4 new); 1009 passed, 9 skipped in the full repo suite. |
| No look-ahead / chronology | **PASS**: decisions only see events before their timestamp; labels train only after the 30s horizon; fills require evidence after order arrival (all tested). |
| Safety (paper only) | **PASS**: no live order path; guard, GET-only client and port checks are all tested. |
| 1,000-forecast gate | **PASS with a caveat**: 0 violations; it blocked 662 historical and 17 fresh would-be trades. Its R² > 0 test still let 4 historical trades through at R² ≈ +0.003, and all 4 lost. |
| Edge / profitability | **FAIL**: every trading variant lost money after fees, both historical and fresh. The cluster-bootstrap 95% CI for historical net P&L is entirely below $0 for every variant (P(net ≤ 0) = 1.00). |
| Does AI help? | **Not shown.** Adding AI inputs made the out-of-sample forecast worse (R² −0.086 vs −0.034 on the same 2,337 fresh forecasts). AI used directly lost $4.53 over 11 fresh trades. |
| Sample size | **Too small to establish an edge.** 88 historical contracts (~22 h) and only 3 settled fresh contracts. No single variant reached 100 trades (the largest was 54); the cost-blind diagnostic reached 55 across historical and fresh. |

### Headline numbers

| Variant | Data | Trades | Wins / losses | Win rate | Net P&L | Fees | Max DD | Trades per active hour |
|---|---|---|---|---|---|---|---|---|
| Gated model (the actual strategy) | historical | 4 | 0 / 4 | 0% | −$19.64 | $3.49 | $19.64 | 0.18 |
| Cost-blind diagnostic (model) | historical | 50 | 10 / 40 | 20.0% | −$48.93 | $28.69 | $49.04 | 2.27 |
| Momentum baseline (+1¢ hurdle) | historical | 52 | 6 / 46 | 11.5% | −$46.25 | $24.47 | $46.25 | 2.36 |
| Momentum baseline, cost-blind | historical | 54 | 10 / 44 | 18.5% | −$59.98 | $28.92 | $61.29 | 2.45 |
| Gated model, no AI | fresh | 0 | – | – | $0.00 | $0.00 | $0.00 | 0 |
| Gated model with existing-AI inputs | fresh | 1 | 0 / 1 | 0% | −$0.93 | $0.27 | $0.93 | 1.29 |
| Existing AI used directly | fresh | 11 | 2 / 9 | 18.2% | −$4.53 | $5.59 | $6.74 | 14.21 |
| Momentum baseline | fresh | 5 | 0 / 5 | 0% | −$5.72 | $2.90 | $6.25 | 6.46 |
| **No-trade baseline** | both | 0 | – | – | **$0.00** | $0.00 | $0.00 | 0 |

The no-trade baseline beat every trading variant.

Opportunity rate: historically the model's raw forecast cleared the +1¢
after-cost hurdle **30.4 times per active hour**. With no forecasting skill
behind it, those are noise. After the skill gate, **0.27 BUY signals per hour**
remained.

## Why out-of-sample R² is negative

All figures come from every scored out-of-sample forecast
(`data/micro_scalp/reports/r2_investigation.json`).

1. **There is essentially no linear predictability over 30 seconds.** Across
   66,049 historical forecasts, the correlation between forecast and outcome is
   **0.006**. Even rescaling the forecasts with the best factor chosen in
   hindsight only reaches R² = **0.0002**. This is not a scaling bug: the
   signal is not there.
2. **The moves are large but directionless.** The 30s mid change has a standard
   deviation of 9.5¢ (6.1¢ in fresh order-book data), and 42% of 30s windows
   move 5¢ or more. The model's forecasts vary by only 1.0¢. Small, noisy
   coefficients fitted to big, unpredictable moves give slightly negative R².
   Halving the forecasts moves R² toward 0 (−0.0017) but never meaningfully
   above it.
3. **Simple momentum is worse than nothing.** "The last 30s move repeats" scores
   R² −0.91 historically and −1.22 fresh, with a correlation of −0.01 to −0.14.
   There is no exploitable trend effect, at most a weak reversal.
4. **The data source contributes noise.** Historical quotes are inferred from
   trade prints (Kalshi publishes no past depth), so the historical mid bounces
   between bid and ask. That partly explains why historical label noise
   (9.5¢) exceeds fresh order-book noise (6.1¢).
5. **Costs dwarf any plausible edge.** A round trip near 50¢ costs about 5–6¢
   (spread, 1¢ slippage, ~3.5¢ in fees). In the diagnostic, fees alone ($28.69)
   exceeded the gross loss before fees ($20.24).

## Is the existing AI adding value?

- **Inputs to the scalp model (fresh, paired on identical forecasts):** R²
  **−0.086 with AI** vs **−0.034 without**. The fitted weight on the OPENING
  forecast (−0.22) came from only 3–4 contracts and looks like overfitting.
- **Direct use** (expect the mid to move toward the AI's live probability):
  gross P&L was +$1.06 before fees over 11 fresh trades, but net was −$4.53
  after $5.59 of fees. Its forecast R² was −0.22.
- **As a settlement forecaster:** Brier score **0.047 (AI)** vs **0.057 (market
  mid)**, from 72 samples on just **3 contracts**. That direction is
  encouraging but statistically meaningless; at least 30 contracts are needed
  before drawing conclusions.
- **Conclusion:** there is no evidence yet that the existing AI improves
  micro-scalping. Its settlement-probability quality deserves continued
  measurement as data accumulates.

## Duplicate settlement records

The recorder fix (a `settled` set, so each contract's result is written once)
is in the code and now has a recorder-level test
(`test_recorder_writes_each_settlement_once`). The recorder process that is
currently running was started before the fix and keeps writing duplicates
(3,366 so far). It was not restarted, so collection would not be interrupted.
The loader ignores every duplicate (`duplicate_settlement: 3366`), so results
are unaffected. The fix applies from the recorder's next start.

## Gate behaviour in detail

- Blocked by design: 662 historical and 17 fresh moments where the forecast
  cleared the cost hurdle, but there were fewer than 1,000 scored forecasts or
  R² ≤ 0.
- Gate violations (a gated BUY without validated skill): **0** in every run.
  This is enforced in code, audited in the report, and covered by
  `test_gate_audit_counts_blocked_candidates_and_never_violates`.
- **Weakness:** "R² > 0 over the last 3,000 forecasts" can be crossed by chance.
  The 4 historical trades that passed did so while R² was about +0.003, and all
  4 lost. **Recommendation, not yet applied** (to avoid tuning on these
  results): require a statistically significant improvement, e.g. the lower
  95% bound of out-of-sample R² above 0, or R² ≥ 0.01.

## Limitations and missing data

- Historical: no published order-book depth, so fills come from trade-print
  evidence and quotes are inferred from prints. The tape stops 25s before
  close. The fetch was still running (88 of up to 200 contracts used).
- Fresh: 4 contracts recorded, 3 settled (~48 minutes). Too little for any
  conclusion about AI or edge.
- Existing AI states were not recorded historically, so AI is measurable only
  on fresh data.
- Queue position, hidden liquidity and how other traders react to our orders
  are unknown. Participation caps (25%) are an assumption.
- Trades within one contract are dependent. Intervals use a contract-level
  cluster bootstrap and are approximate. They do not account for model
  selection across the variants tested.

---

## Full tables (generated by `src.micro_scalp.report_md`)

## Historical (settled contracts, before the fresh window)

Data: {"source": "historical", "markets": 88, "markets_with_truncated_trade_tape": 0, "first": "2026-11-18T05:30:00Z", "last": "2026-11-19T03:30:00Z", "depth_available": false}

| Metric | market_only | DIAGNOSTIC_cost_blind | BASELINE_momentum | BASELINE_momentum_cost_blind | no-trade baseline |
|---|---|---|---|---|---|
| Completed trades | 4 | 50 | 52 | 54 | 0 |
| Wins | 0 | 10 | 6 | 10 | – |
| Losses | 4 | 40 | 46 | 44 | – |
| Net P&L (after fees) | $-19.64 | $-48.93 | $-46.25 | $-59.98 | $0.00 |
| Gross P&L before fees | $-16.15 | $-20.24 | $-21.78 | $-31.06 | $0.00 |
| Total fees | $3.49 | $28.69 | $24.47 | $28.92 | $0.00 |
| Win rate | 0.0% | 20.0% | 11.5% | 18.5% | – |
| Profit factor | 0.00 | 0.11 | 0.06 | 0.09 | – |
| Max drawdown | $19.64 | $49.04 | $46.25 | $61.29 | $0.00 |
| Ending equity | $980.36 | $951.07 | $953.75 | $940.02 | – |
| Win rate 95% CI | 0.0% – 49.0% | 11.2% – 33.0% | 5.4% – 23.0% | 10.4% – 30.8% | – |
| Net P&L 95% CI (cluster bootstrap) | $-38.04 – $-7.83 | $-75.96 – $-25.04 | $-60.80 – $-29.82 | $-88.30 – $-36.82 | $0.00 |
| P(net ≤ 0) (bootstrap) | 1.00 | 1.00 | 1.00 | 1.00 | – |
| Orders submitted / filled / partial / unfilled | 11 / 8 / 0 / 3 | 108 / 97 / 3 / 8 | 112 / 103 / 1 / 8 | 120 / 105 / 4 / 11 | 0 |
| Model out-of-sample R² vs no-change | 0.003 | 0.003 | -0.732 | -0.732 | – |
| Edge candidates / BUY signals / trades per active hour | 30.36 / 0.27 / 0.18 | 8.77 / 2.36 / 2.27 | 8.86 / 2.5 / 2.36 | 5.09 / 2.68 / 2.45 | 0 |
| Gate-blocked candidates / gate violations | 662 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | – |
| Direction hit rate (out of sample) | 51.6% | 51.6% | 54.7% | 54.7% | – |


### Development vs holdout

**market_only** (split at 2026-11-18T16:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 4 | $-19.64 | $3.49 | 0.0% | 0.00 | $-38.04 – $-7.83 |
| Holdout (second half) | 0 | $0.00 | $0.00 | – | – | – |

**DIAGNOSTIC_cost_blind** (split at 2026-11-18T16:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 41 | $-33.88 | $22.13 | 21.9% | 0.12 | $-49.95 – $-18.22 |
| Holdout (second half) | 9 | $-15.05 | $6.56 | 11.1% | 0.08 | $-36.67 – $-1.72 |

**BASELINE_momentum** (split at 2026-11-18T16:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 33 | $-30.22 | $16.13 | 12.1% | 0.08 | $-38.86 – $-21.11 |
| Holdout (second half) | 19 | $-16.03 | $8.34 | 10.5% | 0.04 | $-26.10 – $-5.11 |

**BASELINE_momentum_cost_blind** (split at 2026-11-18T16:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 36 | $-30.46 | $18.63 | 22.2% | 0.16 | $-41.65 – $-19.64 |
| Holdout (second half) | 18 | $-29.52 | $10.29 | 11.1% | 0.01 | $-54.12 – $-10.22 |


### Diagnostic trades by market condition (historical)

| Condition | Bucket | Trades | Net P&L | Win rate | Fees |
|---|---|---|---|---|---|
| volatility_60s | mid | 17 | $-22.11 | 5.9% | $9.30 |
| volatility_60s | high | 17 | $-16.13 | 23.5% | $10.85 |
| volatility_60s | low | 16 | $-10.69 | 31.2% | $8.54 |
| time_left | middle | 17 | $-14.42 | 23.5% | $8.51 |
| time_left | late | 16 | $-20.17 | 18.8% | $10.09 |
| time_left | early | 17 | $-14.34 | 17.6% | $10.09 |
| entry_price | mid | 19 | $-20.24 | 5.3% | $11.06 |
| entry_price | expensive | 17 | $-6.38 | 35.3% | $5.61 |
| entry_price | cheap | 14 | $-22.31 | 21.4% | $12.02 |
| side | up | 8 | $-8.03 | 12.5% | $5.66 |
| side | down | 42 | $-40.90 | 21.4% | $23.03 |


Exit reasons: {"model now expects": 1, "stop-loss": 40, "profit target": 8, "risk limit": 1}


Risk events: [[1794982172, "CONSECUTIVE_LOSS_PAUSE", 5], [1794986715, "CONSECUTIVE_LOSS_PAUSE", 5], [1794991725, "CONSECUTIVE_LOSS_PAUSE", 5], [1794999002, "DAILY_LOSS_HALT", 969.8], [1795049881, "CONSECUTIVE_LOSS_PAUSE", 5]]


## Fresh, unseen live recording

Data: {"source": "recorded_live", "file": "data/micro_scalp/reports/fresh_snapshot.jsonl", "record_counts": {"market": 4, "book": 2759, "gap": 81, "ai": 2863, "trade": 127774, "settlement": 3369, "duplicate_settlement": 3366}, "depth_available": true, "contracts": 4, "settled_contracts": 3, "first": "2026-11-19T03:31:19.868156+00:00", "last": "2026-11-19T04:19:02.749903+00:00"}

| Metric | market_only | with_existing_ai | AI_direct | BASELINE_momentum | no-trade baseline |
|---|---|---|---|---|---|
| Completed trades | 0 | 1 | 11 | 5 | 0 |
| Wins | 0 | 0 | 2 | 0 | – |
| Losses | 0 | 1 | 9 | 5 | – |
| Net P&L (after fees) | $0.00 | $-0.93 | $-4.53 | $-5.72 | $0.00 |
| Gross P&L before fees | $0.00 | $-0.66 | $1.06 | $-2.82 | $0.00 |
| Total fees | $0.00 | $0.27 | $5.59 | $2.90 | $0.00 |
| Win rate | – | 0.0% | 18.2% | 0.0% | – |
| Profit factor | – | 0.00 | 0.43 | 0.00 | – |
| Max drawdown | $0.00 | $0.93 | $6.74 | $6.25 | $0.00 |
| Ending equity | $1,000.00 | $999.07 | $995.47 | $994.28 | – |
| Win rate 95% CI | – | 0.0% – 79.3% | 5.1% – 47.7% | 0.0% – 43.5% | – |
| Net P&L 95% CI (cluster bootstrap) | – | – | – | – | $0.00 |
| P(net ≤ 0) (bootstrap) | – | – | – | – | – |
| Orders submitted / filled / partial / unfilled | 0 / 0 / 0 / 0 | 4 / 2 / 0 / 2 | 28 / 22 / 0 / 6 | 13 / 10 / 0 / 3 | 0 |
| Model out-of-sample R² vs no-change | -0.022 | -0.061 | -0.218 | -1.219 | – |
| Edge candidates / BUY signals / trades per active hour | 0.0 / 0.0 / 0.0 | 24.55 / 2.58 / 1.29 | 16.8 / 16.8 / 14.21 | 9.05 / 9.05 / 6.46 | 0 |
| Gate-blocked candidates / gate violations | 0 / 0 | 17 / 0 | 0 / 0 | 0 / 0 | – |
| Direction hit rate (out of sample) | 53.0% | 52.5% | 48.1% | 48.7% | – |


**Cost-blind diagnostics (fresh)**

| Metric | DIAGNOSTIC_cost_blind | DIAGNOSTIC_cost_blind_with_ai | AI_direct_cost_blind | BASELINE_momentum_cost_blind | no-trade baseline |
|---|---|---|---|---|---|
| Completed trades | 5 | 5 | 6 | 5 | 0 |
| Wins | 0 | 0 | 1 | 0 | – |
| Losses | 5 | 5 | 5 | 5 | – |
| Net P&L (after fees) | $-5.96 | $-5.96 | $-2.74 | $-6.15 | $0.00 |
| Gross P&L before fees | $-3.52 | $-3.52 | $0.44 | $-3.13 | $0.00 |
| Total fees | $2.44 | $2.44 | $3.18 | $3.02 | $0.00 |
| Win rate | 0.0% | 0.0% | 16.7% | 0.0% | – |
| Profit factor | 0.00 | 0.00 | 0.40 | 0.00 | – |
| Max drawdown | $6.11 | $6.11 | $4.69 | $6.15 | $0.00 |
| Ending equity | $994.04 | $994.04 | $997.26 | $993.85 | – |
| Win rate 95% CI | 0.0% – 43.5% | 0.0% – 43.5% | 3.0% – 56.4% | 0.0% – 43.5% | – |
| Net P&L 95% CI (cluster bootstrap) | – | – | – | – | $0.00 |
| P(net ≤ 0) (bootstrap) | – | – | – | – | – |
| Orders submitted / filled / partial / unfilled | 15 / 10 / 0 / 5 | 15 / 10 / 0 / 5 | 17 / 12 / 0 / 5 | 12 / 10 / 0 / 2 | 0 |
| Model out-of-sample R² vs no-change | -0.022 | -0.061 | -0.218 | -1.219 | – |
| Edge candidates / BUY signals / trades per active hour | 0.0 / 7.75 / 6.46 | 0.0 / 7.75 / 6.46 | 5.17 / 10.34 / 7.75 | 3.88 / 6.46 / 6.46 | 0 |
| Gate-blocked candidates / gate violations | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | – |
| Direction hit rate (out of sample) | 53.0% | 52.5% | 48.1% | 48.7% | – |


### Existing AI live probability vs outcomes (fresh)

- status: INSUFFICIENT_SAMPLE
- samples: 72
- contracts: 3
- brier_existing_ai_live: 0.0469
- brier_market_mid: 0.0566
- note: Lower is better. Samples within a contract share one outcome, so the effective sample size is the number of contracts. Confidence is not inferred from this unless contracts >= 30.

