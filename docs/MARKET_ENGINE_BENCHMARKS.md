# Reproducible offline market-engine benchmarks

Run from the repository root using the dashboard's existing virtual environment:

```bash
.venv/bin/python3 scripts/benchmark_signals.py --output /tmp/kxbtc-offline-benchmark.json
```

For optional real Chromium desktop/mobile checks, when Playwright and Chromium
are already installed:

```bash
.venv/bin/python3 scripts/benchmark_signals.py --browser --output /tmp/kxbtc-offline-benchmark.json
```

This command does not start the live dashboard, use port 8765, read an account,
connect to an exchange, or change an existing journal. It creates temporary
synthetic journals and an ephemeral loopback HTTP fixture. The browser, when
enabled, can request only that fixture. The benchmark output contains timings
and synthetic-fixture status, not account information or credentials.

## Measured example

The following results were measured in the development environment on
2026-10-27. They describe this synthetic workload and this machine; they are not
measurements of your Codespace, laptop, Internet connection, exchange feed, or
prediction accuracy. Rerun the command to measure your environment.

| Operation | Samples | Median | 95th percentile |
| --- | ---: | ---: | ---: |
| Public message parsing plus intelligence-buffer ingestion | 3,001 | 0.0415 ms | 0.1210 ms |
| Multi-horizon intelligence calculation | 601 | 0.8119 ms | 1.6573 ms |
| Advanced executed-trade and cached multi-timeframe analysis | 601 | 5.5011 ms | 12.5945 ms |
| Live prediction plus journal write, including warmed 60-second scalp simulation | 20 | 98.9316 ms | 107.9170 ms |
| Main dashboard HTTP response | 10 | 2.0971 ms | 5.9183 ms |
| Main HTTP response while research is deliberately blocked | 10 | 1.7609 ms | 2.5359 ms |
| Bounded 20-record history HTTP page | 10 | 1.9426 ms | 2.5346 ms |
| Direct indexed 20-record SQLite page | 20 | 0.4643 ms | 0.6359 ms |

One synthetic opening calculation, including its journal write, took **57.418 ms**.
One synthetic rollover, with metadata already supplied, took **62.881 ms**. The
opening record for the previous contract remained identical after rollover.
These timings exclude real discovery, official-threshold publication, provider
delays, and Internet access; they do not promise an opening-second prediction.

Chromium results from the same run:

| Browser fixture | Page load | First contract displayed | Explicit refresh |
| --- | ---: | ---: | ---: |
| Desktop, 1400 × 1100 | 130.902 ms | 175.975 ms | 15.061 ms |
| Mobile, 390 × 844 | 151.344 ms | 188.111 ms | 14.143 ms |

Both fixtures displayed the opening prediction first, had no horizontal overflow,
raised no JavaScript errors, and made no external requests. These figures use a
local ephemeral HTTP server and exclude GitHub's private forwarded-port network.

The accelerated run took **8.516 seconds** of elapsed wall time and **6.940
seconds** of Python process CPU time, equivalent to **81.50% of one CPU core**
during the benchmark. Peak Python resident memory was **46.75 MiB**. Browser child
process memory is excluded. The benchmark processes five simulated minutes as
quickly as possible; these CPU measurements are not steady-state live utilization.

## What is simulated

- The exchange event spacing is configured as **100 ms**. Analysis is requested
  every five events, representing a **500 ms simulated cadence**. No actual
  socket delivery rate is measured.
- The event-to-receipt offset is configured as **25 ms**. This is not measured
  one-way network latency, ping latency, or provider publication delay.
- The model fixture supplies 1,440 synthetic closed one-minute candles, fresh
  synthetic quotes, an explicit target, and verified-rule flags for a synthetic
  contract. Its probabilities are not evidence of calibration or useful skill.
- Quote/reference history is warmed before measuring full live calculations. The
  report states whether both scalp probabilities were available and records the
  scalp holding horizon, rather than timing only an insufficient-data shortcut.
- The advanced benchmark includes 3,001 explicitly synthetic executions, actual
  quantity-weighted calculations and zero capture resets. VWAP/order flow are
  READY; 30-second RSI correctly remains unavailable after only five minutes
  (it needs 15 closed bars). Separate integration tests warm longer genuine
  synthetic 30-second bars. Maximum advanced calculation in this run was
  27.3254 ms; median and p95 are not worst-case guarantees.
- One simulated disconnect/reconnect invalidates the old reference and accepts a
  fresh post-reconnect reference. Both checks passed in the example. This is one
  offline recovery case, not a live reconnection-reliability statistic.
- The five-minute observation test retained 601 half-second bins and six minute
  bins. This confirms the exercised workload only; the separate unit tests cover
  bounded longer retention and reconnect/gap behavior.

## What remains a live-access measurement

Actual feed latency, WebSocket connection stability, event timestamp quality,
provider update cadence, clock offset, REST fallback behavior, and opening
discovery delay require supported live sources in the running Codespace. A
provider publishing less often than every 500 ms cannot be turned into a 500 ms
data source by this benchmark. The dashboard records event, receipt, calculation,
publication, and browser-refresh times separately.

Live HTTP/WebSocket verification was blocked by the development environment's
network access during this upgrade. No synthetic timing above substitutes for a
successful live-source test. Forecast accuracy and profitable edge remain
unverified and require chronological official-outcome evaluation.
