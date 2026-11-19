# 8768 combined opening AI, speed work, and feed audit (shadow only)

Nothing here is deployed. The live 8768 model, its 87 inputs, its 55% threshold, its journal and its settings are unchanged. The bot is signals-only.

## 1. Combined decision engine (`src/signals_display/combined_opening.py`)

### Base probabilities

All of these are available at the opening:

- **existing:** the running AI's immutable recorded opening probability.
- **logistic:** the wide regularised logistic model.
- **logistic_aug and gbdt_aug:** the same model families, also trained on retrospective candle-only rows. Those rows use candles that had closed and been received by each opening, plus official outcomes. They are used only for training, never for scoring.

### Combiners

These six were pre-registered:

1. existing only
2. logistic only
3. mean of existing and logistic
4. mean of existing and logistic_aug
5. learned stacking: L2 logistic on the base logits, plus whether the models agree and how far apart they are
6. volatility-regime-aware stacking

The stacker for each contract is fitted only on earlier out-of-fold rows whose labels were available before that contract opened.

### Data segments

The segments are fixed by the first shadow run, so the 43-market holdout is never used to select anything again:

| Segment | Contracts | Use |
|---|---|---|
| Development | Closing before that holdout started | Every choice is made here: combiner (lowest development log loss), operating threshold, accuracy-target thresholds. |
| Previous holdout | The 43 markets already viewed in aggregate | Reported descriptively only. |
| Fresh | Closing after the first run's as-of time | Unseen by anyone. Scored once by the frozen procedure. |

### Decision and report

- **Decision:** UP, DOWN or WAIT, with a calibrated probability and a short reason, for example "combined 0.64; existing 0.62; logistic 0.58; models agree".
- **Operating threshold:** the lowest threshold whose development calls are credibly better than chance (Wilson lower bound above 50%, at least 20 calls). That gives the most correct calls that still show a credible edge. If no threshold qualifies, the engine waits.
- **The report also gives:**
  - accuracy, Wilson 95% interval, correct and wrong calls, coverage, Brier score, log loss and calibration bins for each model on identical contracts;
  - accuracy when the models agree versus disagree;
  - net expected value after Kalshi spread, fees and slippage;
  - an exploratory test of which indicator families add value (each family removed in turn).

**Tests (synthetic data only):**
- Flipping the fresh or previous-holdout labels never changes a development choice.
- The stacker learns only from earlier rows.
- Retrospective rows are never scored.
- The family test finds the family that carries the signal.
- The command-line tool runs end to end on the real journal schema without changing the journal.

## 2. Speed: exact Monte Carlo optimisation (`live_model.simulate_paths`)

The scalp Monte Carlo simulation was the largest single cost in a live tick. The rewrite:

- draws the same seeded random numbers, in the same order, with the same early exits and the same order of arithmetic;
- hoists work that doesn't change between seconds: the entry fee is computed once per path, and the seconds before entry only advance the price;
- inlines the fee formula with the identical sequence of operations.

**Verification:** `tests/test_live_model_simulation_exact.py` compares it with the original loop, copied verbatim, on 120 parameter sets that cover target, stop, timeout and no-entry outcomes. Every set must match exactly (`==`) and consume the same number of random draws.

**Measured locally (offline benchmark, `scripts/benchmark_signals.py`, 80 predictions × 2 runs each):**

| Live prediction with journal | Before | After |
|---|---|---|
| Median | 105–109 ms | 88–94 ms |
| p95 | 122–124 ms | 108–133 ms |

That's a 12–19% lower median, with identical outputs.

The simulation itself runs 1.4–1.7× faster. What's left is mostly Python's random number generator. A NumPy version would change the random numbers, and therefore the outputs, so it was not done.

**The larger speed gain is already built:** the pending 8768 upgrade (`c0471e5`) measured prediction p50 at 192 ms against 1,698 ms on Oracle. The audited 1,143 ms comes from the old build that is still running.

## 3. Feed and indicator audit (existing code; nothing duplicated)

### Settlement reference

Kalshi's API rule text is the source of truth. The settlement source is the CF Benchmarks BRTI index, averaged over 60 seconds, as recorded and fingerprinted in each opening snapshot (`docs/KXBTC15M_RULE_SOURCES.md`). Coinbase, Kraken and Binance prices are labelled `ESTIMATED_REFERENCE` and are never treated as the settlement price.

### Feeds

| Requested feed | Status in code |
|---|---|
| Kalshi WebSocket order-book deltas | **Exists:** `kalshi_market_stream.py` (read-only key, fail-closed schema). |
| Kalshi ticker and public trades | Not subscribed. A candidate for a disabled-by-default addition only if the order-flow family proves useful. |
| Coinbase ticker, market trades, heartbeats | **Exists:** `streaming.py`. Aggressor side is inverted correctly, because Coinbase reports the maker side. |
| Coinbase level 2 book | Not subscribed. The Kraken checksum- and sequence-verified book supplies book features today. |
| Kraken spot book and perpetual | **Exist:** book via `advanced_flow.py` and `cross_exchange_flow.py`; perpetual via `coverage.py`, used for basis. |
| Binance spot trades and depth | **Not added.** Binance.com refuses US-hosted clients, and the Oracle VM is in a US region, so it would be neither reliable nor compliant. Binance.US is a separate, thinner venue. |

This development sandbox's proxy refuses Kalshi and Coinbase, so live feed health can only be verified on Oracle. The audit script reports collector and worker health there.

### Indicators

These requested indicators already exist as frozen opening inputs or live features:

- order-book imbalance, microprice and micro-price deviation;
- CVD, aggressive trade imbalance and trade intensity;
- replenishment and depth changes;
- effective spread;
- cross-exchange divergence and lead-lag;
- realised volatility and volatility regime;
- momentum and acceleration;
- VWAP deviation, EMA distances and gaps, RSI, MACD and Bollinger bands;
- time remaining and distance to strike;
- Kalshi pre-open probability.

The family test in the combined report measures, on 8768's own data, which of these families actually help.

## 4. What limits accuracy today

1. **Sample size.** The 8768 journal has about 214 contracts eligible for training. At that size a 90% claim cannot be verified: it needs on the order of 100 calls at about 96% on unseen markets. The fresh segment grows by about 96 markets a day. Retrospective candle rows add training data, but only for candle features.
2. **The previous holdout (43 markets) was weak:** logistic 25/43, ensemble 21/43. Those numbers can't separate the models from chance. That's why final confirmation uses fresh markets only.
