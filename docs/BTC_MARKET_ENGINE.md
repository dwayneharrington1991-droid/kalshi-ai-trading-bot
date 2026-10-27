# BTC 15-minute market engine

This upgrade extends the existing dashboard. It monitors data and provides manual
decision support. It cannot submit, modify or cancel orders, transfer money, or
manage a broker position. Port 8765 must remain Private.

## Open the updated dashboard

In your existing Codespace, choose **Terminal → New Terminal**, paste this block,
and press Enter:

```bash
cd /workspaces/kalshi-ai-trading-bot &&
git fetch origin &&
git switch codex/kxbtc15m-signals-display &&
git pull --ff-only origin codex/kxbtc15m-signals-display &&
python3 scripts/start_dashboard.py --restart --private-dashboard
```

Then choose **Ports → 8765 → Open in Browser** and refresh the page. Keep **Port
Visibility → Private**. Existing server-side secrets are reused; never put a key
in the page or chat. The launcher preserves both journals and stops only matching
dashboard processes from this checkout. Stop if Git reports local changes; do not
reset, clean or remove databases to force an update.

## Screen layout

1. **Opening prediction:** immutable UP/DOWN/UNCLEAR, actual contract ticker and
   threshold, both probabilities, exact generation/cutoff times, delay, countdown,
   data quality and experimental confidence. The optional price distribution is
   a conditional statistical estimate of the final settlement-reference average.
2. **Live prediction:** fresh estimates, distance to threshold, change from the
   opening estimate, model/market comparison and a bounded probability chart.
3. **Model vs. Market:** executable YES/NO bids, asks, depth, fees, assumed slippage
   and theoretical settlement EV. **EDGE UNVERIFIED** remains visible even when
   calculated EV is positive. A favorable forecast is not a BUY instruction.
4. **My Live Trades:** confirmed holdings, actual reconciled entry costs, broker
   cash and estimated exit P&L; pending and partially filled orders stay distinct.
   A same-contract experimental forecast is shown only when fresh and supported.
   A current-market forecast is never reused for an older held contract.

History is paginated and loads on request. Technical calculations and connection
diagnostics stay under **Details**. Optional alerts have browser-local thresholds
for probability changes, outages, volatility, settlement time, position value and
existing explicit risk limits. They cannot change a position or guarantee a fill.

## Documented source selection

| Source | Information used | Access / cadence / limitations |
| --- | --- | --- |
| Coinbase BTC/USD | Public ticker, trades, available best bid/ask, event and receipt times | `wss://advanced-trade-ws.coinbase.com`, public Advanced Trade `ticker`, `market_trades`, `heartbeats`. Trade side is maker side; aggressor direction is inverted. No fabricated depth. |
| Coinbase historical BTC/USD | Closed one-minute OHLCV and UTC daily levels | Public Advanced Trade candle GETs. Initial 24h preload is at most five requests; later refreshes fetch missing closed minutes, normally one request/minute. Gaps remain explicit. |
| Kraken BTC/USD | Trades and checksum-verified spot depth, cross-venue context | Existing public `wss://ws.kraken.com/v2` trade/book subscriptions reused. Adapter sample time is explicitly different from original socket receipt time. REST ticker/OHLC/depth remain context/fallback. |
| Kraken futures | PF_XBTUSD public trade flow; PI_XBTUSD inverse mark/index, funding and OI context | Existing public `wss://futures.kraken.com/ws/v1` trade subscription plus five-second REST ticker context. Raw contract quantities are not pooled with spot BTC; PF and PI products are not treated as interchangeable. Funding units/intervals are not guessed. |
| Kalshi KXBTC15M | Official ticker, opening/close times, target/rules, executable book and official result | Credential-free REST discovery/order books, normally once/second. Optional `wss://external-api-ws.kalshi.com/trade-api/ws/v2` requires already verified read-only credentials; only fixed order-book subscriptions for up to 24 KXBTC15M contracts are permitted. |
| Federal Reserve | Timestamped public press-release context | Public RSS only. Untrusted text is data, never instructions. This is not a comprehensive news feed or advance CPI/FOMC calendar. |
| Official CF Benchmarks BRTI | Kalshi's published target and rule metadata | **Live official index is unavailable.** No paid index feed was added. Exchange proxies, opening-basis assumptions and proxy averaging are clearly labeled. |

No universal Internet coverage is claimed. Binance/USDT is excluded without a
verified contemporaneous USD conversion. No extra exchange is promoted simply to
increase source count; incremental predictive value must be tested.

REST endpoints are fixed market-data GETs. Provider 429/5xx and transport failures
use bounded retry intervals; Retry-After is respected when numeric. Streams use
bounded buffers, sequence checks, reconnection/backoff, stale checks and explicit
overflow/gap counters. Unsupported book schemas invalidate the book and retain
REST fallback; they are not interpreted by guessing price units.

Kalshi signing accepts only the fixed WebSocket GET upgrade path after broker
metadata verifies read-only scopes. The stream has no generic send function,
order channels, mutation endpoints or arbitrary destination. It follows the
current and still-held contracts across rollover and checks permissions while
connected. Missing credentials, unknown scopes or mutation permissions block it.
Public BTC monitoring remains available independently.

## Timing and data integrity

Public sockets ingest events independently. A coalescing mailbox schedules model
updates at an approximately 500 ms minimum interval when new input exists. REST
collection, account reconciliation, official result lookup and research have
separate workers. Exchange update cadence, network delays and calculation time
can make actual intervals longer. An idle provider is not a new observation.

UTC timestamps identify event time, receipt time, cutoff, generation and
publication separately. Monotonic clocks measure durations. Event-to-receipt
differences include clock offset and provider publication delay; they are not a
measured one-way network latency. Clock discontinuities invalidate stream
continuity. NTP synchronization is not asserted or changed by the application.
Details show actual processing intervals, transport RTT where measured, data age,
queue drops, reconnects, CPU samples and Linux peak process RSS.

Analysis covers 500 ms, 1/5/15/30 seconds, 1/3/5/15/30 minutes, 1/4 hours and 24
hours where observed coverage permits. Bounded half-second and minute aggregates
avoid retaining every tick for a day. Quotes cannot invent traded volume or
aggressor direction. Closed-candle indicators include momentum, RSI/MACD/slopes,
ATR, volatility clustering/expansion, Bollinger measures, volume and structural
descriptors. These are correlated experimental candidates, not independent votes.

At each detected opening, the engine freezes inputs received by the opening
cutoff. Later BTC observations are excluded even if the official target arrives
late. The actual delay is recorded. Existing opening records survive restart and
cannot be overwritten. Cold starts or incomplete frozen data show **OPENING
PREDICTION UNAVAILABLE**. Leave the application running before the next opening;
preloading does not guarantee timely provider publication. New live probabilities
are recorded before outcomes, while official settlement results are collected
separately. No historical opening predictions are backfilled.

The 24-hour window is historical context for the particular 15-minute event, not
a 24-hour prediction. The default statistical model assumes zero drift, measured
volatility and a constant proxy/index opening basis. Its final-60s averaging
calculation respects the verified contract comparison. Basis sensitivity and a
Gaussian conditional range are not calibrated uncertainty bounds.

## Learning, validation and rollback

Implemented candidates are logistic regression, ridge and a validation-selected
convex ensemble with the independent event-aware volatility baseline. Kalshi odds
are a separate benchmark. Train-only preprocessing, purged chronological groups,
validation-only calibration/weight selection and untouched test gates prevent
future leakage. Input ablations include the new microstructure horizons.

The actual journal evaluation during this upgrade found **80 official contracts**,
below the **170 minimum** before further feature/split/class-balance checks. It
returned **INSUFFICIENT_OFFICIAL_LABELS** without reading the historical payload
blobs or fitting a model. No learned candidate was promoted. Tree families and
sequence models are deferred until independent samples and a predeclared protocol
justify the extra model search. No useful accuracy or profitable edge is claimed.

While the public dashboard runs, an hourly background check schedules evaluation
at most once per 24 hours. This can archive an experimental candidate but **cannot
activate it**, modify an active artifact or change risk limits. The registry
records exposed holdout contracts and refuses silently reusing them as untouched
test data. A minimum count is not a guarantee of skill. Use
`--disable-scheduled-learning` with the server if archive-only evaluation is not
wanted. Model and registry files remain excluded from Git.

Manual offline comparison, optional when enough official evidence exists:

```bash
.venv/bin/python3 -m src.signals_display.learning --journal signals_monitor.db --output signals_model.json --registry signals_model_registry --scheduled
.venv/bin/python3 -m src.signals_display.model_registry --registry signals_model_registry --status
```

Explicit activation and rollback, followed by a normal dashboard restart:

```bash
.venv/bin/python3 -m src.signals_display.model_registry --registry signals_model_registry --activate VERSION --output signals_model.json
.venv/bin/python3 -m src.signals_display.model_registry --registry signals_model_registry --rollback --output signals_model.json
```

Replace `VERSION` only with a reviewed registered version. Activation remains an
experimental forecast change, not permission for a BUY signal. The opening model
stays its frozen statistical forecast; post-opening learned features are never
borrowed to manufacture an ensemble opening. A live learned binary classifier
does not produce a BTC price distribution; the separately labeled price range
belongs to the independent statistical baseline.

Performance reports keep opening/latest, horizons, volatility regimes, probability
buckets, source quality, missing openings and coverage distinct. They show Brier
score, log loss, calibration error, baselines, sample counts and descriptive
intervals. Serial dependence/model error can invalidate nominal uncertainty.
Actual-account results, manual records and simulated scalp outcomes remain
separate; no signal is a fill.

## Verification and remaining limitations

Run only the offline signals tests and benchmark:

```bash
NO_PROXY=127.0.0.1,localhost .venv/bin/python3 -m pytest -q tests/test_signals*.py
.venv/bin/python3 scripts/benchmark_signals.py --browser --output /tmp/kxbtc-benchmark.json
```

The NO_PROXY setting above applies only to the local synthetic HTTP fixtures,
preventing a corporate proxy from intercepting loopback tests. It does not bypass
exchange access controls. The benchmark never connects to exchanges or accounts,
uses temporary journals, and does not touch port 8765. See
[measured workloads and limitations](MARKET_ENGINE_BENCHMARKS.md).

Live HTTP/WebSocket access from this development environment was blocked by its
network proxy. No personal Codespace session or authentication was accessed, and
deployment there is not claimed. The new socket adapters are mock/fixture tested;
their production handshakes, schema compatibility and true provider cadence must
be confirmed in Codespace Details after restart. If unavailable, source status
and reasons remain visible and REST/stale/WAIT behavior remains active. No live
reconnection reliability, 500 ms delivery guarantee, model accuracy or profitable
edge has been demonstrated by the offline benchmark.
