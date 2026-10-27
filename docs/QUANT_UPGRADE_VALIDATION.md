# Quantitative dashboard upgrade and verification

This extends the existing signals display on `codex/kxbtc15m-signals-display`.
The running application remains a market-data and optional read-only account
monitor. It cannot submit, amend or cancel orders, manage broker positions, or
transfer money. Actual account results, user-reported trades and simulations
remain separate. Existing journal rows are not deleted, rewritten or backfilled.

## What is integrated

The opening prediction stays first, with the live prediction directly underneath.
Both use the active contract's verified threshold, close and final-average rule.
The original opening record is immutable, including its real generation time,
data cutoff, input receipts and any delay. Rollover creates a new record rather
than changing the previous one. Missing opening inputs remain unavailable.

The near-50/50 diagnosis is explicit: the opening statistical fallback assumes
zero drift and an opening proxy/index basis. Its mean can be approximately the
threshold, producing a genuinely near-0.5 probability. Floating-point differences
previously allowed a directional label even when both displayed percentages
rounded to 50.0. Display ties are now **NEUTRAL**; raw probabilities and old saved
labels remain unchanged. Directional accuracy uses the same display policy,
while Brier, log loss and calibration retain original numerical precision.
Opening diagnostics identify the exact model, fallback, eligible sample count,
history, cutoff and promotion blocker. The live ensemble is not silently reused
as an opening model.

All twelve families are connected to **Details → ADVANCED MARKET INTELLIGENCE**:

| Family | Implemented analysis and limits |
| --- | --- |
| RSI | Wilder RSI14 on genuine closed 30s, 1m, 5m, 15m and 1h bars; momentum, overbought/oversold, confirmed-pivot divergences and reversal crossings. |
| MACD | Standard 12/26/9 and faster 6/13/5 shadow configuration; histogram, momentum, crossovers and confirmed-pivot divergence with seeded warmup. |
| VWAP | Actual executed price × quantity, rolling and uninterrupted-capture-session VWAP, deviation, slope, crossings and volume-weighted momentum. A capture session is not represented as a fully observed UTC day. |
| Bollinger | 20-period population-standard-deviation bands, configurable deviations, bandwidth, %B, expansion/contraction and breakout descriptors. |
| EMA | SMA-seeded 9/21/50/200 with an additional full period of warmup; supported timeframes, alignment, slope, distance, acceleration and crossovers. |
| Volume profile | Real executed volume in configurable price buckets/window; POC, high/low-volume nodes, contiguous 70% observed-bin value area and concentration near the actual threshold. |
| Order flow | Identified executed aggressor buys/sells, pressure, intensity, large-trade threshold and momentum. Inferred/unknown direction is identified rather than presented as authoritative. |
| Order book | Executable best quotes, spread, top and multi-level depth/imbalance where available, depth-weighted price, concentration and microprice. Top-only data never becomes invented twenty-level depth. |
| CVD | Actual executed buy/sell quantities, session and window delta, slope, acceleration and descriptive price/CVD divergence; never inferred from depth. |
| Microstructure | Available spread/microprice/depth, contemporaneous pre-trade effective spread, arrivals, realized volatility, descriptive impact, observed replenishment, quote frequency and deterioration. |
| Ensemble | Existing logistic/ridge/statistical candidates plus dedicated opening shadow learning and bounded boosted stumps when enough opening labels exist; chronological purging, ablations, explicit versions, holdout ledger and no automatic activation. Distribution drift uses reference-only quantiles and minute snapshots. |
| Calibration | Brier, log loss, ECE, reliability bins/diagram, volatility groups and descriptive intervals; isotonic PAVA, monotone Platt and identity compared on chronological shadow calibration-fit/selection/untouched-test splits. No automatically activated calibrator. |

Each family shows its reading, interpretation, source timestamp/age, availability
and actual contribution status. Full bounded readings and the reliability diagram
load only when expanded. A listed model input is marked contributing only if it
is actually available and fresh. New feature values enter the existing candidate
vector and are journaled before outcomes; they do not change a probability unless
a compatible explicitly selected artifact uses them. Strongest validated advanced
factors currently remain unavailable. A bullish reading is not an entry signal.

Spot and derivative quantities stay source-specific. Coinbase maker-side trade
directions are inverted to obtain the aggressor. Independent quote/trade channel
timestamps and sequences prevent delayed trades from being discarded or rewinding
newer quotes. Completed 30s trade bars require a later trade-channel event past
their close, with its actual receipt recorded as availability. Missing volume,
identifiers, continuity or receipt proof withholds dependent features. Bounded
capture truncation is disclosed rather than passed off as a complete window.

The scalp monitor is a separate visible panel. It reports engine/data/model/
liquidity status and the exact blockers, both hypothetical directions, actual
exposure and configured limits. Its configurable diagnostic criteria can tighten
the defaults; they cannot bypass model validation or alter account risk limits.
The recommendation remains WAIT while no qualifying execution edge is verified.

New normalized book observations support on-demand paper replay, realistic manual
delay from actual recording time, executable asks/bids, visible depth and partial
fills, spread, slippage and rounded fees. Model probability is reused only when
quantity, target, stop, horizon, delay, slippage and fees match its frozen event.
Missing intervals remain unobserved, not wins. Official settlement results do not
prove an unobserved scalp path. No simulation is an actual fill.

## Real evidence available during development

The read-only local journal audit found **80 official settlement contracts**, below
the **170 minimum** for the existing live-model comparison. It found **0 eligible
new frozen-feature opening contracts** for opening-specific learning. Old live
forecasts cannot be relabeled as historical opening feature vectors. Consequently
no upgraded classifier, ensemble weight or calibrator was promoted. Random forest,
LightGBM/XGBoost and other additional model searches are deferred; the sample does
not justify their added selection complexity. Boosted opening stumps are shadow
only and require at least 500 eligible opening contracts before evaluation.

The default normalized paper journal has no historical records predating this
upgrade. An explicitly enabled bounded legacy replay accepted 928 timestamp-proven
books across 2 contracts/16 observed windows, with 71 missing-book exclusions.
It found **0 candidate opportunities and 0 candidate trades**. An always-UP
counterfactual had 7 simulated trades, only 5 fully observed, 2 unobserved and
8 missed fills. Its observed-only net result was +$0.09. That tiny, incomplete,
dependent comparison is **not evidence of a profitable edge**. No candidate
accuracy, success rate or useful calibration can be established from these data.

**Edge unverified.** The retained statistical fallback is experimental, not a
validated profitable baseline. The above local journal counts are not a claim
about the contents of the user's separately running Codespace.

Read-only reproducible evaluations:

```bash
.venv/bin/python3 -m src.signals_display.opening_models --journal signals_monitor.db --output /tmp/opening-shadow.json --registry /tmp/opening-shadow-registry --walk-forward
.venv/bin/python3 -m src.signals_display.learning --journal signals_monitor.db --output /tmp/live-shadow.json --registry /tmp/live-shadow-registry --walk-forward
.venv/bin/python3 -m src.signals_display.scalp_paper --journal signals_monitor.db --output /tmp/scalp-paper.json
```

## Tests, performance and remaining live checks

Run the complete offline repository suite safely from the repository root:

```bash
.venv/bin/python3 scripts/test_offline.py
```

The harness excludes live tests, removes credential variables only from its test
process, bypasses dotenv loading, forces trading safeguards and blocks external
DNS/connections. Loopback remains available for mocked HTTP/browser tests. Tests
cover every new indicator, source units, malformed/missing inputs, sequence gaps,
reconnects, delayed messages, cutoff/receipt leakage, immutable openings, rollover,
stale caches and API snapshots, realistic partial fills, database preservation,
read-only routes, desktop/mobile layout, lazy rendering and reliability diagrams.
Existing legacy warnings include an older async unittest that is not awaited;
that old test is not presented as execution coverage.

Final full-suite result: **846 passed, 8 live tests deselected, 11 existing
warnings in 40.62 seconds**. All 12 desktop/mobile browser tests passed within
that run. `git diff --check` also passed.

Measured offline browser/processing timings and a reproducible benchmark are in
[MARKET_ENGINE_BENCHMARKS.md](MARKET_ENGINE_BENCHMARKS.md). They exclude real network
and Codespace forwarding latency. The benchmark never touches the existing
journal or production account. Main HTTP state omits advanced analysis; Details
load it separately. Technical work is cached at closed-bar boundaries, trade
buffers are bounded, and historical evaluation runs in background workers.

Current development-environment public Kalshi and Coinbase GET probes fail with
**ProxyError**. Production authentication, fresh live prices, actual live account
positions and real rollover cannot be reverified here. Their mocked integration
tests pass, but production health must be checked in the running Codespace.
No credentials were requested, printed, committed, or sent in those public probes.
The launcher still verifies documented read-only scopes before enabling account
monitoring; missing, unknown, trading or transfer scopes keep it blocked.

Official live CF BRTI remains unavailable without a supported index feed. Exchange
proxies and their opening-basis/final-average assumptions remain explicit. No paid
service was added. A 24h one-minute preload cannot supply 200 hourly EMA periods;
those long warmups remain unavailable. Real 30s indicators warm from captured
trades rather than inventing half-minute history. Liquidation clusters, hidden
institutional orders and comprehensive future news calendars are not fabricated.

## Update the existing private Codespace

1. Open the existing **mysterious haunting** Codespace.
2. Choose **Terminal → New Terminal** and paste this whole block:

```bash
cd /workspaces/kalshi-ai-trading-bot &&
git fetch origin &&
git switch codex/kxbtc15m-signals-display &&
git pull --ff-only origin codex/kxbtc15m-signals-display &&
python3 scripts/start_dashboard.py --restart --private-dashboard
```

3. In **Ports**, find **8765**, keep **Port Visibility → Private**, and click
   **Open in Browser**. Refresh the already open dashboard tab after updating.
4. Leave it running before the next opening to accumulate pre-opening inputs.
   Check Details for account authentication/permission status, last successful
   sync, fresh quote/reference timestamps and any exact blocker.

The existing Codespaces secrets are read server-side. Do not paste a key anywhere.
The launcher preserves existing journal/history and restarts only this checkout's
dashboard. If Git reports local changes, preserve them rather than resetting.
Future starts can use **Terminal → Run Task → Start private Kalshi dashboard**.
