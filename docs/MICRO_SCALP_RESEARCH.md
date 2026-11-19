# Micro-scalp research: run 3 and the candidate-strategy study

**PAPER SIMULATION AND RESEARCH ONLY.** No order was placed, nothing was
deployed, and port 8768 was not touched. No result here shows live
profitability.

Data used:
- **Historical:** 200 contiguous settled contracts, 2026-11-17 01:30 → 2026-11-19 03:30 UTC (50 h).
- **Fresh recording A:** 8 contracts, 7 settled, 03:31 → 05:21 UTC, with order-book depth and existing-AI state.
- **BTC reference:** Coinbase BTC-USD public trade tape as 1-second bars (189,137 bars). It is a proxy, not the settlement index.
- **Fresh recording B:** started 05:21 UTC and **kept untouched** as the next final holdout. Its first contract overlaps recording A and must be excluded.

## Verdict: **FAIL**. No credible edge found; all candidates rejected.

| Question | Answer |
|---|---|
| Baseline rerun (200 contracts, identical rules) | Gated strategy: **0 trades** (blocked by the skill gate, as designed). Every trading variant lost money; bootstrap P(net ≤ 0) = 1.00 for all historical variants. |
| Candidate study (6 historical families, 17 configurations) | **All 6 families rejected before the test split.** No configuration was net-positive on both train and validation. The test split (last 50 contracts) was **never evaluated**, and the holdout ledger is empty. |
| Why the 30s forecast fails | Kalshi prices are BTC with sub-second lag (87% of 30s variance explained by the same-window BTC fair-value move). Past BTC moves barely predict future Kalshi moves (correlation 0.04). Even perfect hindsight averages only +2.8¢ per 30s round trip after costs. |
| Existing AI | Better Brier score than the market on **4 contracts** (0.088 vs 0.103). That's too few to mean anything. As a scalp signal it lost money. Not testable on holdout B (its AI source stopped). |
| Profitability claim | **None.** |

## 1. Baseline rerun (identical parameters and validation rules)

Same `StrategyConfig`/`RiskConfig` as run 2: $1,000 simulated, 1% worst-case
risk per trade, Kalshi taker fees, 1.0s latency, 2.0s immediate-or-cancel,
±1¢ limit tolerance, 25% participation, 1¢ extra slippage on historical
print-evidence fills. Only the loader changed (streaming, with identical
event order, verified by test).

| Variant | Data | Trades | W / L | Win rate (95% CI) | Net P&L (95% CI) | Fees | Max DD | Trades/hour |
|---|---|---|---|---|---|---|---|---|
| Gated model (strategy) | 200 hist. | 0 | – | – | $0.00 | $0.00 | $0.00 | 0.00 |
| Cost-blind diagnostic | 200 hist. | 79 | 18 / 61 | 22.8% (14.9–33.2%) | −$74.90 (−$99.25 to −$50.20) | $45.07 | $76.56 | 1.58 |
| Momentum baseline | 200 hist. | 80 | 8 / 72 | 10.0% (5.1–18.5%) | −$76.06 (−$101.87 to −$54.55) | $42.43 | $76.28 | 1.60 |
| Gated model, no AI | fresh A | 0 | – | – | $0.00 | $0.00 | $0.00 | 0.00 |
| Gated model + AI inputs | fresh A | 2 | 0 / 2 | 0% | −$1.86 | $0.66 | $1.86 | 1.13 |
| Existing AI used directly | fresh A | 11 | 2 / 9 | 18.2% | −$4.53 | $5.59 | $6.74 | 6.19 |
| Momentum baseline | fresh A | 16 | 3 / 13 | 18.8% | −$13.93 (−$19.65 to −$4.99) | $7.12 | $14.99 | 9.01 |
| **No trading** | both | 0 | – | – | **$0.00** | $0.00 | $0.00 | – |

**Development vs holdout halves (historical):**
- Cost-blind diagnostic: −$50.36 on 58 trades, then −$24.54 on 21 trades.
- Momentum: −$48.50 on 55 trades, then −$27.56 on 25 trades.
- Both halves lose for both variants.

**Forecast accuracy (scalp model, out of sample):**
- Historical: R² +0.024 against a no-change forecast, with a 59.4% direction hit rate, over its final 3,000 scored forecasts. The 45 would-be trades the gate blocked happened while validated skill was below the threshold, and there were 0 gate violations.
- Fresh A: R² −0.042 without AI and −0.048 with AI. Momentum's forecast scores R² −0.73 historically and −0.92 fresh.
- (The AI-direct "6.1% hit rate" counts periods with no AI signal, i.e. zero forecasts, as misses. Use its R² of +0.012 instead.)

**Calibration (settlement probability vs official outcome):**
- Historical (100 train contracts, every 30s): market mid Brier 0.1476, BTC fair-value model 0.1487, 50/50 blend 0.1475. The market is already as well calibrated as an independent BTC model.
- Fresh A (4 contracts with AI): existing AI 0.0877 vs market 0.1027. That's statistically meaningless; at least 30 contracts are required.

**Risk limits** were active throughout. Many losing validation runs stop near
−$30, which is consistent with the 3% daily-loss halt. Real-time limits cut
those runs short, which also reduces trade counts.

## 2. Why the 30-second model has almost no predictive power (TRAIN only)

Source: `src.micro_scalp.research.investigate`, 14,269 samples across 100
train contracts. Validation and test were not used.

1. **Kalshi BTC-15m prices are almost entirely BTC.** The change in BTC fair
   value over the same 30 seconds explains **86.8%** of the Kalshi mid's 30s
   variance (correlation 0.932).
2. **There is no exploitable lag at 1s latency.** BTC's past 10s/30s return
   correlates only 0.04/0.03 with the next 30s Kalshi move, and the fair-value
   gap only 0.025. By the time a 1-second-latency trader sees a BTC move, the
   Kalshi book has already absorbed it.
3. **Predicting the next 30s Kalshi move means predicting BTC's next 30s.**
   That is close to a random walk. The best single feature correlations are
   about 0.06 (30s momentum, +0.056; 60s volatility, −0.060). Signals that
   weak cannot overcome the costs.
4. **Costs exceed even the hindsight ceiling's margin of safety.**
   - A random-entry 30s taker round trip averages **−2.9¢** after spread and fees.
   - Picking the better side *with perfect hindsight* averages only **+2.8¢**.
   - A real forecast with hit rate p captures roughly (2p − 1) of that ceiling, and still pays full costs.
5. **The moves are large and directionless.** The 30s mid change has a 8.9¢
   standard deviation (12.8¢ at 60s, 17.8¢ at 120s), so tiny coefficients
   produce slightly negative out-of-sample R². This is the same diagnosis as
   run 2, now confirmed on 2.3× more data and on BTC itself.

## 3. Candidate-strategy study (pre-registered protocol)

Protocol (`src/micro_scalp/research/protocol.py`, fixed before any run):
- **Splits:** train = contracts 1–100, validation = 101–150, test = 151–200, all chronological.
- **Selection:** parameters come from a small pre-set grid and are chosen on train/validation only. A family reaches test only if its configuration is net-positive on **both** train and validation with at least 30 validation trades.
- **One test per family:** each family gets at most one test evaluation, enforced by an append-only ledger that refuses a modified configuration on the same holdout.
- **"Credible edge" on test** requires at least 100 trades, net > 0, contract-cluster bootstrap 95% lower bound > 0, and profit factor ≥ 1.2. It must then also not lose on the untouched fresh holdout B.
- **Same costs as the baseline:** every candidate runs through the baseline engine's own execution, fee, sizing and risk code (`ResearchEngine` subclass). The baseline defaults are unchanged.

Families tested on historical data:
- **Mean reversion**: fade the last 30s move.
- **Momentum**: follow the last 30s move, at three strengths.
- **Taker order flow**: follow the buy/sell aggressor imbalance.
- **Volatility regime**: mean reversion only in low- or high-volatility periods, with cut-offs from train terciles.
- **BTC fair-value gap**: expect the mid to move toward a Gaussian final-average model on the Coinbase BTC proxy.
- **Fair-value hold-to-settlement**: buy when BTC fair value exceeds the ask by a margin, then hold to settlement.

Result: **every family was REJECTED_BEFORE_TEST.**

The closest call was **fair-value hold** with a 10¢ margin. It made +$87.37 on
train (49 trades, profit factor 1.46), then lost $29.93 on validation (28
trades, profit factor 0.68). That swing is what binary payouts on a few dozen
trades look like, not evidence of an edge.

Not testable with valid data:
- **Order-book imbalance and liquidity changes:** Kalshi publishes no historical depth. Only the ~15 recorded contracts have it, too few to select and validate.
- **AI-vs-market gap:** the existing AI was recorded on only 8 contracts.

These families were run only as **exploratory** checks on fresh A (never used
for selection). Book imbalance almost never clears the cost hurdle (0–1
trades). The AI gap lost $3.13 over 7 trades.

| Family | Config | Train trades | Train net | Val trades | Val net | Val win rate | Val PF | Status |
|---|---|---|---|---|---|---|---|---|
| mean_reversion | {"k": 0.25} | 23 | $-45.21 | 21 | $-32.09 | 10% | 0.054 | REJECTED_BEFORE_TEST |
| mean_reversion | {"k": 0.5} | 32 | $-44.29 | 19 | $-30.27 | 11% | 0.005 | REJECTED_BEFORE_TEST |
| mean_reversion | {"k": 1.0} | 54 | $-60.69 | 21 | $-31.08 | 0% | 0.0 | REJECTED_BEFORE_TEST |
| momentum_k | {"k": 0.25} | 43 | $-39.08 | 43 | $-30.20 | 19% | 0.103 | REJECTED_BEFORE_TEST |
| momentum_k | {"k": 0.5} | 48 | $-41.98 | 30 | $-30.60 | 7% | 0.053 | REJECTED_BEFORE_TEST |
| momentum_k | {"k": 1.0} | 55 | $-48.50 | 44 | $-32.00 | 20% | 0.29 | REJECTED_BEFORE_TEST |
| flow | {"k": 0.02} | 0 | $0.00 | 0 | $0.00 | – | – | REJECTED_BEFORE_TEST |
| flow | {"k": 0.05} | 1 | $-0.28 | 1 | $-5.24 | 0% | 0.0 | REJECTED_BEFORE_TEST |
| flow | {"k": 0.1} | 31 | $-56.89 | 12 | $-31.46 | 8% | 0.09 | REJECTED_BEFORE_TEST |
| btc_fair_gap | {"k": 0.25} | 2 | $-2.24 | 7 | $-9.96 | 14% | 0.037 | REJECTED_BEFORE_TEST |
| btc_fair_gap | {"k": 0.5} | 61 | $-39.63 | 40 | $-30.66 | 18% | 0.058 | REJECTED_BEFORE_TEST |
| btc_fair_gap | {"k": 1.0} | 57 | $-43.27 | 37 | $-30.22 | 14% | 0.049 | REJECTED_BEFORE_TEST |
| fair_value_hold | {"margin": 0.03} | 22 | $-60.41 | 37 | $-29.89 | 62% | 0.762 | REJECTED_BEFORE_TEST |
| fair_value_hold | {"margin": 0.06} | 55 | $-59.48 | 34 | $-30.87 | 65% | 0.717 | REJECTED_BEFORE_TEST |
| fair_value_hold | {"margin": 0.1} | 49 | $87.37 | 28 | $-29.93 | 64% | 0.675 | REJECTED_BEFORE_TEST |
| vol_regime_mr | {"k": 0.5, "vol_regime": "low", "vol_cut": 0.061} | 24 | $-35.71 | 14 | $-21.71 | 7% | 0.025 | REJECTED_BEFORE_TEST |
| vol_regime_mr | {"k": 0.5, "vol_regime": "high", "vol_cut": 0.095} | 31 | $-42.10 | 17 | $-30.64 | 0% | 0.0 | REJECTED_BEFORE_TEST |

| Exploratory fresh (8 contracts, already seen) | Trades | W/L | Net | Fees | PF |
|---|---|---|---|---|---|
| {"family": "mean_reversion", "k": 0.5} | 31 | 10/21 | $-24.58 | $22.56 | 0.358 |
| {"family": "momentum_k", "k": 0.25} | 6 | 2/4 | $-1.82 | $1.69 | 0.379 |
| {"family": "flow", "k": 0.02} | 0 | 0/0 | $0.00 | $0.00 | – |
| {"family": "btc_fair_gap", "k": 0.25} | 0 | 0/0 | $0.00 | $0.00 | – |
| {"family": "fair_value_hold", "margin": 0.03} | 7 | 6/1 | $11.45 | $1.33 | 2.237 |
| {"family": "vol_regime_mr", "k": 0.5, "vol_regime": "low", "vol_cut": 0.061} | 0 | 0/0 | $0.00 | $0.00 | – |
| {"family": "book_imbalance", "k": 0.02} | 0 | 0/0 | $0.00 | $0.00 | – |
| {"family": "book_imbalance", "k": 0.05} | 1 | 1/0 | $0.06 | $0.11 | – |
| {"family": "ai_gap", "k": 0.25} | 0 | 0/0 | $0.00 | $0.00 | – |
| {"family": "ai_gap", "k": 0.5} | 7 | 3/4 | $-3.13 | $3.98 | 0.498 |

## 4. Safeguards used

- **Isolation:** experiments live in `src/micro_scalp/research/`. Baseline `Engine`, `StrategyConfig` defaults and `backtest.py` rules are unchanged; the baseline ignores the new BTC events (tested).
- **No look-ahead:**
  - a BTC 1s bar is usable only after its second closes plus 0.25s;
  - Kalshi events are used only after they arrive;
  - investigation labels are computed separately and never used as trading inputs;
  - the investigation tool refuses the test split.
- **CPU and memory:** at most 3 worker processes at `nice 10–15`, with a 6 GB address-space cap each. Observed RSS was 0.1–0.26 GB per process; the streaming loader keeps about two contracts in memory.
- **Collection:** the fresh recorder A finished on its own and was never interrupted. Its file is preserved (an unchanged copy is in `reports/`). Holdout B is recording separately with the settlement-dedup fix (5 settlements, 0 duplicates so far).
- **Disk:** about 0.7 GB under `data/micro_scalp/` (git-ignored), with about 59 GB free.
- **Incident:** the sandbox signals server (port 8790; the source of existing-AI state) stopped at its 2-hour background limit. Holdout B therefore has AI state for only its first ~7 minutes. It was not restarted.

## 5. Recommended next research direction

1. **Stop pursuing 30-second taker scalps on KXBTC15M.** The data shows the
   price is BTC with sub-second lag, and costs exceed even the
   perfect-foresight margin. More parameter search here would mostly
   overfit.
2. **Build a long, continuous recording first.** Record existing-AI state,
   the BTC reference and Kalshi depth over at least **300 contracts (≈3
   days)**. Run it in a separate, supervised process, not a 2-hour
   background job.
3. **Then test settlement-probability edges, not scalps.**
   - Compare the existing AI against the market mid on ≥30 (ideally ≥300) contracts with Brier score and a reliability curve.
   - Only if the AI is clearly better calibrated, test hold-to-settlement entries on a fresh holdout. They pay one fee instead of two.
   - Expect high variance: about 300 trades are needed before such an edge could clear the bootstrap.
4. **Explore passive (maker) execution** once enough recorded depth exists.
   Capturing the spread instead of paying it is the only structural way
   around the cost wall. It needs a queue-position fill model validated
   against recorded trades before any P&L is believable.
5. **Keep holdout B and the historical test split untouched** until a
   candidate passes train/validation under the same protocol.

Reproduce:

```bash
python -m src.micro_scalp.backtest --historical data/micro_scalp/historical --fresh data/micro_scalp/reports/fresh_snapshot_full.jsonl --out data/micro_scalp/reports/eval_run3_baseline.json --workers 2
python -m src.micro_scalp.research.investigate --historical data/micro_scalp/historical --btc data/micro_scalp/btc/btc_1s.json --out data/micro_scalp/research/investigation_train.json --splits train
python -m src.micro_scalp.research.run --historical data/micro_scalp/historical --btc data/micro_scalp/btc/btc_1s.json --fresh data/micro_scalp/reports/fresh_snapshot_full.jsonl --investigation data/micro_scalp/research/investigation_train.json --out data/micro_scalp/research/study.json --ledger data/micro_scalp/research/holdout_ledger.jsonl --workers 3
```

---

## Appendix: full baseline run-3 tables

## Historical (settled contracts, before the fresh window)

Data: {"source": "historical", "markets": 200, "first": "2026-11-17T01:30:00+00:00", "last": "2026-11-19T03:30:00+00:00", "depth_available": false, "loader": "streaming (bounded memory); identical event order to the list loader"}

| Metric | market_only | DIAGNOSTIC_cost_blind | BASELINE_momentum | BASELINE_momentum_cost_blind | no-trade baseline |
|---|---|---|---|---|---|
| Completed trades | 0 | 79 | 80 | 64 | 0 |
| Wins | 0 | 18 | 8 | 7 | – |
| Losses | 0 | 61 | 72 | 57 | – |
| Net P&L (after fees) | $0.00 | $-74.90 | $-76.06 | $-89.81 | $0.00 |
| Gross P&L before fees | $0.00 | $-29.83 | $-33.63 | $-52.77 | $0.00 |
| Total fees | $0.00 | $45.07 | $42.43 | $37.04 | $0.00 |
| Win rate | – | 22.8% | 10.0% | 10.9% | – |
| Profit factor | – | 0.11 | 0.09 | 0.04 | – |
| Max drawdown | $0.00 | $76.56 | $76.28 | $89.83 | $0.00 |
| Ending equity | $1,000.00 | $925.10 | $923.94 | $910.19 | – |
| Win rate 95% CI | – | 14.9% – 33.2% | 5.1% – 18.5% | 5.4% – 20.9% | – |
| Net P&L 95% CI (cluster bootstrap) | – | $-99.25 – $-50.20 | $-101.87 – $-54.55 | $-120.37 – $-64.45 | $0.00 |
| P(net ≤ 0) (bootstrap) | – | 1.00 | 1.00 | 1.00 | – |
| Orders submitted / filled / partial / unfilled | 1 / 0 / 0 / 1 | 180 / 155 / 10 / 15 | 180 / 158 / 2 / 20 | 140 / 127 / 1 / 12 | 0 |
| Model out-of-sample R² vs no-change | 0.024 | 0.024 | -0.732 | -0.732 | – |
| Edge candidates / BUY signals / trades per active hour | 1.52 / 0.02 / 0.0 | 1.02 / 1.72 / 1.58 | 3.74 / 1.72 / 1.6 | 2.94 / 1.38 / 1.28 | 0 |
| Gate-blocked candidates / gate violations | 45 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | – |
| Direction hit rate (out of sample) | 59.4% | 59.4% | 54.7% | 54.7% | – |


### Development vs holdout

**market_only** (split at 2026-11-18T02:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 0 | $0.00 | $0.00 | – | – | – |
| Holdout (second half) | 0 | $0.00 | $0.00 | – | – | – |

**DIAGNOSTIC_cost_blind** (split at 2026-11-18T02:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 58 | $-50.36 | $33.33 | 25.9% | 0.15 | $-65.79 – $-33.16 |
| Holdout (second half) | 21 | $-24.54 | $11.74 | 14.3% | 0.03 | $-41.40 – $-11.62 |

**BASELINE_momentum** (split at 2026-11-18T02:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 55 | $-48.50 | $29.42 | 14.5% | 0.13 | $-70.46 – $-31.70 |
| Holdout (second half) | 25 | $-27.56 | $13.01 | 0.0% | 0.00 | $-40.30 – $-15.57 |

**BASELINE_momentum_cost_blind** (split at 2026-11-18T02:30:00+00:00)

| Segment | Trades | Net P&L | Fees | Win rate | Profit factor | Net 95% CI |
|---|---|---|---|---|---|---|
| Development (first half) | 37 | $-48.23 | $21.94 | 10.8% | 0.06 | $-64.08 – $-36.17 |
| Holdout (second half) | 27 | $-41.59 | $15.10 | 11.1% | 0.02 | $-67.53 – $-21.17 |


### Diagnostic trades by market condition (historical)

| Condition | Bucket | Trades | Net P&L | Win rate | Fees |
|---|---|---|---|---|---|
| volatility_60s | low | 26 | $-26.22 | 23.1% | $16.00 |
| volatility_60s | high | 27 | $-21.33 | 33.3% | $15.13 |
| volatility_60s | mid | 26 | $-27.35 | 11.5% | $13.94 |
| time_left | middle | 26 | $-33.54 | 11.5% | $15.68 |
| time_left | late | 26 | $-17.59 | 42.3% | $11.90 |
| time_left | early | 27 | $-23.77 | 14.8% | $17.49 |
| entry_price | expensive | 28 | $-15.65 | 32.1% | $9.88 |
| entry_price | mid | 26 | $-26.09 | 11.5% | $15.36 |
| entry_price | cheap | 25 | $-33.16 | 24.0% | $19.83 |
| side | up | 30 | $-29.00 | 23.3% | $18.61 |
| side | down | 49 | $-45.90 | 22.4% | $26.46 |


Exit reasons: {"profit target": 16, "stop-loss": 60, "model now expects": 1, "risk limit": 2}


Risk events: [[1794883069, "CONSECUTIVE_LOSS_PAUSE", 5], [1794887373, "CONSECUTIVE_LOSS_PAUSE", 5], [1794891368, "DAILY_LOSS_HALT", 969.93], [1794891369, "CONSECUTIVE_LOSS_PAUSE", 5], [1794961346, "CONSECUTIVE_LOSS_PAUSE", 5], [1794967702, "CONSECUTIVE_LOSS_PAUSE", 5], [1794974019, "CONSECUTIVE_LOSS_PAUSE", 5], [1794978516, "DAILY_LOSS_HALT", 940.27], [1795049881, "CONSECUTIVE_LOSS_PAUSE", 5]]


## Fresh, unseen live recording

Data: {"source": "recorded_live", "file": "data/micro_scalp/reports/fresh_snapshot_full.jsonl", "record_counts": {"market": 8, "book": 6205, "gap": 410, "ai": 6599, "trade": 266830, "settlement": 19830, "duplicate_settlement": 19823}, "depth_available": true, "contracts": 8, "settled_contracts": 7, "first": "2026-11-19T03:31:19.868156+00:00", "last": "2026-11-19T05:21:19.294705+00:00"}

| Metric | market_only | with_existing_ai | AI_direct | BASELINE_momentum | no-trade baseline |
|---|---|---|---|---|---|
| Completed trades | 0 | 2 | 11 | 16 | 0 |
| Wins | 0 | 0 | 2 | 3 | – |
| Losses | 0 | 2 | 9 | 13 | – |
| Net P&L (after fees) | $0.00 | $-1.86 | $-4.53 | $-13.93 | $0.00 |
| Gross P&L before fees | $0.00 | $-1.20 | $1.06 | $-6.81 | $0.00 |
| Total fees | $0.00 | $0.66 | $5.59 | $7.12 | $0.00 |
| Win rate | – | 0.0% | 18.2% | 18.8% | – |
| Profit factor | – | 0.00 | 0.43 | 0.08 | – |
| Max drawdown | $0.00 | $1.86 | $6.74 | $14.99 | $0.00 |
| Ending equity | $1,000.00 | $998.14 | $995.47 | $986.07 | – |
| Win rate 95% CI | – | 0.0% – 65.8% | 5.1% – 47.7% | 6.6% – 43.0% | – |
| Net P&L 95% CI (cluster bootstrap) | – | – | – | $-19.65 – $-4.99 | $0.00 |
| P(net ≤ 0) (bootstrap) | – | – | – | 1.00 | – |
| Orders submitted / filled / partial / unfilled | 0 / 0 / 0 / 0 | 5 / 4 / 0 / 1 | 28 / 22 / 0 / 6 | 40 / 32 / 0 / 8 | 0 |
| Model out-of-sample R² vs no-change | -0.042 | -0.048 | 0.012 | -0.921 | – |
| Edge candidates / BUY signals / trades per active hour | 0.0 / 0.0 / 0.0 | 34.91 / 1.13 / 1.13 | 7.32 / 7.32 / 6.19 | 52.93 / 12.39 / 9.01 | 0 |
| Gate-blocked candidates / gate violations | 0 / 0 | 60 / 0 | 0 / 0 | 0 / 0 | – |
| Direction hit rate (out of sample) | 52.4% | 56.5% | 6.1% | 50.9% | – |


**Cost-blind diagnostics (fresh)**

| Metric | DIAGNOSTIC_cost_blind | DIAGNOSTIC_cost_blind_with_ai | AI_direct_cost_blind | BASELINE_momentum_cost_blind | no-trade baseline |
|---|---|---|---|---|---|
| Completed trades | 27 | 15 | 6 | 32 | 0 |
| Wins | 8 | 4 | 1 | 9 | – |
| Losses | 19 | 11 | 5 | 23 | – |
| Net P&L (after fees) | $-17.27 | $-10.27 | $-2.74 | $-17.88 | $0.00 |
| Gross P&L before fees | $-1.33 | $-1.96 | $0.44 | $-0.49 | $0.00 |
| Total fees | $15.93 | $8.31 | $3.18 | $17.39 | $0.00 |
| Win rate | 29.6% | 26.7% | 16.7% | 28.1% | – |
| Profit factor | 0.34 | 0.22 | 0.40 | 0.34 | – |
| Max drawdown | $17.26 | $10.67 | $4.69 | $17.88 | $0.00 |
| Ending equity | $982.74 | $989.73 | $997.26 | $982.12 | – |
| Win rate 95% CI | 15.8% – 48.5% | 10.9% – 51.9% | 3.0% – 56.4% | 15.6% – 45.4% | – |
| Net P&L 95% CI (cluster bootstrap) | $-23.74 – $-9.32 | $-19.59 – $-0.38 | – | $-35.01 – $2.56 | $0.00 |
| P(net ≤ 0) (bootstrap) | 1.00 | 1.00 | – | 0.96 | – |
| Orders submitted / filled / partial / unfilled | 69 / 54 / 0 / 15 | 39 / 30 / 0 / 9 | 17 / 12 / 0 / 5 | 77 / 64 / 0 / 13 | 0 |
| Model out-of-sample R² vs no-change | -0.042 | -0.048 | 0.012 | -0.921 | – |
| Edge candidates / BUY signals / trades per active hour | 0.0 / 18.58 / 15.2 | 0.0 / 10.14 / 8.45 | 2.25 / 4.5 / 3.38 | 36.04 / 21.96 / 18.02 | 0 |
| Gate-blocked candidates / gate violations | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | – |
| Direction hit rate (out of sample) | 52.4% | 56.5% | 6.1% | 50.9% | – |


### Existing AI live probability vs outcomes (fresh)

- status: INSUFFICIENT_SAMPLE
- samples: 91
- contracts: 4
- brier_existing_ai_live: 0.0877
- brier_market_mid: 0.1027
- note: Lower is better. Samples within a contract share one outcome, so the effective sample size is the number of contracts. Confidence is not inferred from this unless contracts >= 30.

