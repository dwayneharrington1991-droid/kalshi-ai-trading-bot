# Independent order-flow verification handoff

The verifier in `src/signals_display/order_flow_verification.py` is an offline reference for one spot venue and symbol at a time. The fixtures in `tests/test_order_flow_verification.py` are synthetic and use known quantities and outcomes. The public source tree did not include the new collector branch, its shared schema, Oracle journal exports, or a continuous live capture.

## Collector integration

| Input | Normalization and checks |
| --- | --- |
| Coinbase Exchange BTC-USD | Capture raw Level 2 snapshots and changes in wire order. Treat published Level 2 quantities as replacements and zero as deletion. Convert Exchange matches from maker side to aggressor side before aggregating. |
| Kraken spot BTC/USD | Record the subscription depth, apply each update batch in order, verify the published checksum with full decimal precision, and normalize the trade side as the taker side. Size is BTC. |
| Kalshi BTC 15-minute ticker | Keep a distinct capture and contract quantities. Preserve the subscription identity, sequence, market ticker, outcome side and wire units. Never pass Kalshi prices or contract sizes into the spot verifier. |

Use [Coinbase's Exchange channels](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels), [Kraken's spot book](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book), [Kraken's spot trades](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade), and [Kalshi's order-book channel](https://docs.kalshi.com/websockets/orderbook-updates) to confirm adapter semantics for the actual subscription.

1. Export a bounded, credential-free capture with the raw public messages, venue, instrument, subscription generation, channel, native sequence/checksum where present, native event time, collector receipt time, and integrity/reconnect transitions. Include the normalized snapshot and dashboard payload for each whole second being checked. Export an interval with a disconnect and resync plus a continuous interval with overlapping Coinbase and Kraken data. Include collector drop counters and the dashboard/API serving timestamps.
2. Use integer UTC cutoffs. Replay in receipt order through each cutoff. Preserve the event and receipt time independently. A receipt after the cutoff cannot influence that cutoff. For streams without a native sequence or checksum, document the integrity mechanism and report unknown gaps separately. Invalidate a book after any known gap, checksum failure, or disconnect until a verified snapshot restores it.
3. Compare `spot_depth` outputs against each normalized valid collector snapshot. Use the same displayed price range and precision when comparing depth and wall transitions. Wall threshold in the fixture is three times the same-side median, with at least five levels on that side. A level leaving the subscribed depth is a boundary eviction. A trade at the same price can corroborate consumption but doesn't determine what share of a displayed reduction was executed.
4. Call `book_second` with the last applied book change receipt and the last channel-specific receipt supporting book integrity. A heartbeat on a different channel alone is insufficient. A valid quiet second keeps the same actual levels. For `STALE` or `UNAVAILABLE`, omit the ladder from point-in-time features. Preserve subsecond updates in the capture so additions and withdrawals within a second are visible in replay.
5. Call `trade_windows` with normalized aggressor-side trades from one venue. Give it only a bounded window and use its outputs only after the collector verifies continuous trade coverage over that window. Identical trade IDs within the window count once. A zero window delta is meaningful only with coverage. Compare 1, 5, 15, 30, 60 and 300-second totals with raw trades, independent of book updates.
6. Compare ladder/API/dashboard values and measure feed event-to-receipt, receipt-to-aggregate and aggregate-to-render latency. Report p50, p95 and p99, sample counts, gaps, duplicates, resyncs, and the elapsed time coverage per venue. Use bounded rotation for raw capture storage and report disk growth at observed rates.

## Forecasting follow-up

Join one immutable feature snapshot per decision to the matching independently confirmed outcome. Keep opening settlement, first-six-minute BTC direction, minute-six settlement and longer BTC horizons in separate evaluations. Use price-only and price-plus-order-flow inputs on identical markets and compare with the unchanged model. Freeze the feature variants, coverage rule, fit schedule and pass criteria before gathering a new forward sample. Preserve V3/V4/V5 reserved samples and prior decisions.

The V5 changes close an inner-fold preprocessing leak and enforce pre-opening quote eligibility. The report includes the feature-source artifact and evaluator hashes. Treat output from the repaired evaluator as a distinct recipe and use a fresh forward window if a protected result has already been examined. The repository contains prior results for V3, FIRST 6 MIN, and MINUTE-6 FINAL. This work generated no new empirical model results.

For the alert-service repair, review `scripts/minute6_runner.py`, `scripts/scoreboard7.py` and `src/signals_display/minute6.py` together. The settlement conflict table is additive. Conflicted markets are excluded from final-settlement scoring while the historical records are preserved. The watchdog patch should be reviewed with the Oracle deployment operator. Verify its two lock paths match the actual guarded deploy and job launcher paths.

## Direct public-feed replay evidence

A single 315-second capture on Nov. 26, 2026 Mountain Time compared each post-update top-10 spot ladder to the replayed wire levels. It checked cumulative size at every level using exact rational arithmetic, matched spread and midprice, and independently summed observed aggressor-side trades and CVD for each receipt-eligible window.

| Venue | Ladders checked | Trades | Observed buy / sell / CVD (BTC) | Event timestamp to workstation receipt p50 / p95 / p99 |
| --- | ---: | ---: | --- | --- |
| Coinbase Exchange BTC-USD | 4,941 | 2,840 | 17.81084385 / 8.25264462 / +9.55819923 | 59.6 / 105.3 / 191.4 ms |
| Kraken spot BTC/USD | 16,147 | 462 | 6.28786736 / 2.47722017 / +3.81064719 | 96.8 / 166.4 / 199.4 ms |

Each venue had 314 matching one-second trade windows. At 5, 15, 30, 60 and 300 seconds the matching window counts were 310, 300, 285, 255 and 15 per venue. Coinbase had 314 seconds with book changes. Kraken had 313 with changes and one quiet second where the last book was still fresh. The peak observed one-second trade counts were 216 and 56, respectively. Ladders showed 5,509 Coinbase and 11,938 Kraken in-range wall transitions, including changes in size or the relative-depth flag. Resting depth reductions alone do not identify executions or cancellations.

The final observed trade-ID ranges had zero duplicates and zero missing IDs on either venue. Kraken had 97 transient high-water jumps that filled within the capture. All 16,147 Kraken book checksums matched. In a separate replay, intentionally skipping a live Kraken update triggered a checksum mismatch. Reconnecting restored a checksum-verified snapshot and delta. Coinbase sent a snapshot before deltas on both connections. Its batched L2 deltas do not have a per-delta sequence field in the observed messages. The Coinbase heartbeat cross-check concerns trade IDs and does not certify L2 continuity. A separate public Kalshi REST snapshot confirmed YES/NO ladders in contract quantities. These measurements cover the direct replay path and the workstation clock, with no Oracle collector or dashboard render timings.

## Production compatibility checks before installation

- PR #5 is an isolated change based on public research head `4d95fc4`, descended from the five-line/shadow commit `aca5b20` and the last documented release `7b985a7`. The dashboard, shared five-line formatter, FIRST 6 MIN model and opening Telegram script match the documented release. The new collector branch, live working copies and active Oracle service inputs were unavailable for comparison.
- On Claude's exact release, merge the alert/scoring changes together. Rehearse with a disposable copy of the existing alert ledger. Confirm a conflicting official settlement is excluded from both final-settlement scoreboards, the first stored outcome remains intact, and FIRST 6 MIN continues scoring against its separate BTC reference. A local test with an existing-schema fixture verified this behavior.
- Rehearse one complete alert lifecycle with the actual service argv and poll cadence. A local mock delivered exactly two messages for the same market, both in the existing five-line order. The opening message showed MINUTE-6 FINAL pending, the second reused the locked opening lines, and FIRST 6 MIN stayed TESTING. Measure how often engine snapshots are first received by open +367 seconds before installing the stricter input check. Later first receipts become ineligible.
- Verify the production job launcher and 8768 watchdog resolve to the same deployment and jobs lock files. Rehearse deployment contention and verify the watchdog rechecks the listener under those locks. The watchdog requests a relaunch under lock. Production startup and socket readiness still need a guarded rehearsal.
- Keep the patched V5 evaluator outside the unattended schedule until the revised recipe has a fresh authorized forward window. The historical opening parity assertion now checks the already-existing provenance field explicitly and compares the rest of the opening payload exactly.
