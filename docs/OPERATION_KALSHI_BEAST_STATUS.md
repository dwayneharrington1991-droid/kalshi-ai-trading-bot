# Operation Kalshi Beast — capability and handoff status

Updated: 2026-11-04 UTC

Repository verification before the final browser-throttling pass: 1,014 tests
passed, 8 skipped. The final result is recorded with the release commit.
The selective forward-signal protocol is integrated into Opening and
Settlement evaluation but has not yet been run on a new untouched Oracle
holdout. Production deployment and model promotion therefore remain separate,
explicitly verified operations.

This document records repository capability, integration status, external
blockers, and production boundaries. It is not a performance or profitability
claim.

## Capability matrix

| Capability | Status | Live integration / limitation |
|---|---|---|
| Market Scout, Quant Analyst, Risk Guardian, Decision Coordinator | IMPLEMENTED | `decision_coordinator.py` decorates every published state; all outputs are manual-review or WAIT. |
| Immutable Opening forecast | IMPLEMENTED | Frozen opening-cutoff inputs and receipt chronology; missing openings are not reconstructed. |
| Live Settlement forecast | IMPLEMENTED / EXPERIMENTAL | Updates independently; calibrated advantage remains unverified. |
| Scalp analysis | IMPLEMENTED / EXPERIMENTAL | Fees, spread, slippage, timeout, and liquidity gates; no execution. |
| KXBTC15M rule snapshots and fingerprints | IMPLEMENTED | Structured metadata or strict recognized rule text only; ticker inference is rejected. |
| Official settlement reference | BLOCKED | No authorized real-time CF Benchmarks BRTI feed is configured. External spot remains `ESTIMATED_REFERENCE`. |
| Decimal strike, equality, pricing and fair value | IMPLEMENTED | Decimal arithmetic, tick size, executable prices, depth, fees, and slippage. |
| Time-to-settlement model | IMPLEMENTED / SHADOW | Distance, time, realized/jump volatility, averaging exposure, and uncertainty; uncalibrated and non-actionable. |
| Coinbase live spot, trades, candles, heartbeat | IMPLEMENTED | Public WebSocket plus REST recovery, timestamp and freshness diagnostics. |
| Kalshi public market/order book | IMPLEMENTED | Public REST/stream, reconnection, freshness, active-contract rollover. |
| Binance and OKX adapters | BLOCKED | Not activated without verified access, jurisdictional eligibility, and venue-specific live sequence validation. |
| Polymarket comparison | BLOCKED | No verified compatible-contract adapter/source is configured; no contract equivalence is assumed. |
| Order book reconstruction and sequence gaps | IMPLEMENTED | Snapshot/delta normalization, bounded depth, gap invalidation and resync requirement. |
| CVD, imbalance, replenishment, cancellation, VPIN-style diagnostics | IMPLEMENTED / SHADOW | Executed volume is never inferred from quotes; predictive value remains unverified. |
| Kalshi wall persistence | IMPLEMENTED / SHADOW | Descriptive only; no institutional-intent claim. |
| Cross-venue divergence and lead/lag context | PARTIAL | Venue-neutral analytical primitives exist; live venue coverage is limited to verified accessible feeds. |
| RSI, MACD, VWAP, Bollinger, EMA, volume, volatility, momentum | IMPLEMENTED / SHADOW | Connected to bounded advanced intelligence; unavailable inputs remain explicit. |
| Bayesian regime weighting | IMPLEMENTED / SHADOW | Opening and Settlement buckets remain separate; past verified outcomes only; conservative fallback. |
| News intelligence | PARTIAL | Timestamped Federal Reserve public releases are supported. CPI/regulatory/exchange-event expansion needs verified permitted sources. |
| Read-only account monitoring | IMPLEMENTED / CURRENTLY BLOCKED | GET-only allowlist and permission verification. Last reported Oracle credential returned HTTP 401; no account values inferred. |
| Daily drawdown warning | IMPLEMENTED | Default 3% advisory threshold from confirmed same-day P&L; unavailable when evidence is incomplete. |
| Emergency signal kill switch | IMPLEMENTED | Operator/stale/worker/drawdown suppression affects displayed signals only. |
| Bounded background analytics | IMPLEMENTED | Latest-value workers, bounded queues, superseded-work dropping, latency counters. |
| Health/readiness/recovery | IMPLEMENTED | `/healthz`, `/readyz`, worker heartbeat, service restart, bounded reconnect and atomic rollback. |
| Simple dashboard | IMPLEMENTED | Opening, live Settlement and Scalp cards keep probabilities visible; diagnostics are collapsible. |
| Automated order execution | DISABLED | Signals service import boundary excludes clients/jobs/orders/strategies; no dashboard mutation routes. |
| Selective 80% accuracy objective | IMPLEMENTED / UNVERIFIED | Validation-only abstention selection; untouched test reports accuracy, coverage, sample uncertainty, calibration and regimes. It never promotes a model and 80%/90% are objectives, not claims. |
| Live AI signal chart | IMPLEMENTED | Bounded 1m/3m/5m/15m closed candles, volume, EMA/VWAP/Bollinger, optional RSI/MACD, strike/reference overlays and immutable BUY/EXIT markers. Unsupported 15s history is explicit; markers require verified risk gates or a fresh read-only held position. |
| Settlement evaluation extraction | IMPLEMENTED / ORACLE RECHECK REQUIRED | Uses indexed per-contract/horizon reads instead of a global window sort over the multi-gigabyte revision table, with targeted legacy fallback so valid historical rows are not dropped. Local 8.5 GB journal benchmark is ~4.6s with identical eligible-row coverage. |
| Browser responsiveness | IMPLEMENTED | Main state rendering is capped at 2 Hz, chart history is bounded to 180 bars, hidden tabs suspend live/chart work, details load only while open, and large research reports remain on independent cached endpoints. |

## Validation state last observed from Oracle output

- Opening: 426 eligible frozen openings; 259 previously exposed; 26 genuinely
  untouched forward contracts in the next block. Four additional qualifying
  contracts are required before its 30-contract evaluation can be unsealed.
- Settlement: the last isolated evaluation reached its 900-second bound under
  the old global-window query and did not produce a report. The indexed repair
  must be deployed and rerun before any activation conclusion is available.
- No model promotion is performed by this change. The production evaluator and
  registry remain authoritative.

## Performance evidence

- Repository deterministic settlement/pricing/coordinator microbenchmark from
  the prior integrated release: 20,000 iterations, approximately 103.86 µs per
  iteration and 4,322 bytes peak traced allocation. This is not end-to-end feed
  latency.
- Oracle CPU, RAM, queue depth, feed latency and browser latency for this new
  commit are unverified until staged deployment. Runtime meters expose process
  CPU/RSS and phase timing without claiming one-way network latency.

## Production boundary

- Last user-verified Oracle production commit before this change:
  `1821de9d94f29b8cc548b5af8cdd39d6a00e94d9`.
- Production was not modified by this repository work.
- Persistent journals, account journals, model artifacts, registries,
  credentials and Oracle-local modifications must remain outside release
  directories and are protected by existing deployment checks.
- Deployment requires explicit user approval after tests and review.

## Next actions

1. Inspect the saved completed Opening holdout report without exposing or
   relabeling its contracts.
2. Optimize or checkpoint the Settlement dataset extraction before another
   production evaluation; do not extend an unbounded full-journal scan.
3. Stage the integrated build, measure Oracle resource/latency behavior, then
   request production-cutover approval.
4. Replace or revoke the invalid Kalshi monitoring key through the existing
   secure root-owned credential installer if read-only account cards are needed.
