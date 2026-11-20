# Forensic accuracy and staleness audit (2026-11-20)

Production 8768 runs `3ce9071`, model `b50b9dc30f6d664c`, threshold 0.55. It was not restarted or
changed by this audit, and automatic promotion stayed disabled. All Oracle access was read-only
(SQLite mode=ro, nice 19). Data: the live journal exported 19:16 UTC, covering 447 openings (446
settled; 364 settled live-model openings from 2026-11-16 21:15) and 124,347 live predictions. The
pre-registered forward test (frozen 2026-11-20 06:15 UTC, `c08fb8c`) was not scored, and no model
was fitted or selected on openings after the freeze.

## 1. Executive summary

**The "good for a while, then stale" pattern is not caused by a software defect in the opening
prediction path.** It is explained by three things together:

1. **Streaks are ordinary chance.** The sequence of right and wrong calls (122 calls, 64.8%
   correct) is indistinguishable from random ordering:

   | Measure | Observed | Shuffled median | p |
   |---|---|---|---|
   | Number of runs | 54 | 57 | 0.33 |
   | Longest run | 6 | 9 | (shorter than chance) |
   | Range of 20-call accuracy | 0.45 | 0.40 | 0.31 |
   | Lag-1 autocorrelation | +0.04 | | 0.31 |

   A 65% hit rate routinely produces 85% and 40% stretches over 20 calls.
2. **A real but slow decline.** Brier skill against a past-only base rate fell from +0.0135 in the
   first half to -0.0006 in the second (permutation p = 0.047, one-sided). The first half's
   block-bootstrap interval excludes zero; the second half's does not.
3. **The early good run was better than the model's true ability.** In development the model's
   held-out Brier was about 0.247 (53% accuracy), barely better than chance. Live, its first half
   was unusually good (Brier 0.2373; calibration slope 2.63, meaning underconfident). Its second
   half returned to roughly the validated level (0.2481; slope 0.94: calibrated, without skill).
   952 of the 1,124 contracts in its own validation had already been used in earlier tests, so
   even that validation is optimistic.

**Data health does not explain bad openings.** No technical measure is associated with worse skill
after correcting for 14 comparisons. The measures tested were: missing inputs, reference age at the
open, candle-history age, prediction cadence, data age, opening delay, spread, volatility and time
of day.

Every stored probability was reproduced exactly from its frozen pre-open inputs (365/365, maximum
difference 5.6e-17). Core indicators were recomputed independently and matched exactly for all 369
openings with full history. Labels and thresholds matched Kalshi's API on all 60 sampled contracts.

**There is no demonstrated predictive edge.** Across 355 openings with a Kalshi quote, the live
model's Brier was 0.2422 against 0.2462 for the market's own opening price (difference CI -0.011
to +0.002). Over the last 100 settled openings it was 0.2485, against 0.2413 for the past UP rate.

**One confirmed defect, outside the opening path, was fixed** (`27b68e7`): the live settlement
estimate failed in every final minute.

**Deployment recommendation: NO-GO for now.** The fix is low risk and CI-tested, but it is not
urgent for opening calls, and the forward test is running. A deployment can be considered
separately; see section 13.

## 2. System architecture and data flow (production 8768)

```
Exchanges (public, read-only)
  Kalshi REST /markets (KXBTC15M discovery, quotes, floor_strike)  ──┐
  Coinbase Advanced Trade 1-min candles (+ Exchange API fallback,     │  PublicFeed.poll()  (collector thread,
    + paged trade recovery for a missing minute)                      │   1 s cadence, 0.25 s for 6 s after
  Coinbase WS + Kraken WS spot ticks; Kraken futures WS               │   each quarter hour; per-endpoint
                                                                      │   exponential backoff on 429/5xx)
                                                                      ▼
                                       LatestPayload mailbox (latest value only, no queue)
                                                                      ▼
  Monitor.update (prediction thread):
    accept_candle_batch → btc_candles (journal)
    stream observations → Intelligence (micro windows: 5s…24h, continuity rules)
                        → LiveEstimator.observe_reference (per-source receipt audit → preopen_reference_ticks)
    opening snapshot (as-of-open history + reference receipts) → opening_input_snapshots
    opening_forecast(): context24 (1440 contiguous closed 1-min bars, last bar ends 60 s before open)
        + _opening_features (87 model inputs; micro/spot inputs only if their window is READY and
          receipt-proven ≤ open) → frozen_features
        + opening ensemble (logistic, ridge, gradient boosting, mixture) → P(UP)
        + decision: UP ≥ 0.55, DOWN ≤ 0.45, else WAIT
      → btc_engine_opening (INSERT OR IGNORE: immutable, one row per ticker)
    live estimate (LiveEstimator.estimate) → btc_engine_predictions (every ~1.8 s)
    settlement intake → forecast_outcomes (official Kalshi result) → learning_contract_reviews
                                                                      ▼
  HTTP API (127.0.0.1:8768): /api/state, /api/market-history, /api/opening-scoreboard,
    /api/candle-integrity, /api/data-quality-alerts, /api/shadow-opening …  → dashboard.html (SSH tunnel)
  OpeningAlerts → <journal>.alerts.sqlite3 (claim before send) → Telegram (combined message each opening)
```

Other components on the host:

| Component | Schedule / port | Role | State |
|---|---|---|---|
| Lifecycle cron (`5a88d1a`) | :07, :22, :37, :52 | Retrains and scores challengers, read-only on the journal | Promotion disabled since 05:47 UTC |
| `kalshi-health.timer` | Every 5 min | Health check | Active |
| `kalshi-learning-page` | Port 8766 | Status page | Active |
| `kalshi-learning.service` | Offline historical learning | Not used by 8768, which runs `--disable-scheduled-learning` | Failed since 2026-11-12 |
| `rclone` | Local port | Backup | Running |
| Ports 8765 / 8767 | | Retired bots | Not listening |

## 3. Confirmed bugs, severity, root cause

| # | Defect | Severity | Root cause | Status |
|---|---|---|---|---|
| 1 | The live settlement probability is unavailable in every contract's final minute (`MISSING_AVERAGING_HISTORY: final averaging window not covered`). 110 of 375 contracts had no final-minute live prediction (median 1 vs 21 in a mid-contract minute). Captured live at 19:29 UTC. | Medium. Affects the dashboard's live probability during settlement averaging, not opening calls or Telegram. | `LiveEstimator` kept one live buffer and cleared it whenever the tick source changed. Coinbase and Kraken ticks arrive interleaved, so the buffer never spanned the 60 s averaging window. | **Fixed** `27b68e7`: one continuous series per exchange; estimates read only the current source's series, so exchanges are never mixed. 4 regression tests (2 failed before the fix), including a 4-hour replay with outages. |
| 2 | Already fixed earlier this session: false "RESTORE NOT VERIFIED" from the activator after a deployment (`cfa7706`); retirement-capture WAL race (`117ca3c`). | Low | See commits | Fixed |

## 4. Suspected or design issues not proven to affect accuracy

- **The opening reference exchange is chosen by whichever exchange ticked last before the open.**
  It switched 158 times in 378 openings (257 Kraken, 121 Coinbase). `distance_scaled`, the
  reference price and the 1-minute micro return come from that exchange. The measured effect is
  negligible: the median Coinbase-Kraken gap at the open was $4.27 (p95 $15) against a 15-minute
  sigma of $176 (0.024 sigma). The two exchanges disagreed on which side of the threshold BTC was
  in 20 of 155 openings. The sign accuracy of price minus threshold was identical (55.9%) using
  Coinbase, Kraken or their average. Training used the same rule, so there is no train/serve
  mismatch.
- **CPU.** The 8768 process averaged 64% of the host's single CPU over its 15 h of life (48–52%
  when sampled; load about 1.8) and used 1.1 GB of memory. No link to prediction quality was
  found: cadence was p95 2.4 s now, about 6 s overall. It is a capacity risk.
- **HTTP 429 rate limits** cluster just before quarter hours on both builds. Backoff is per
  endpoint, exponential (2 to 60 s), honours `Retry-After` and resets on success. A 429 with a
  long `Retry-After` straddling an open could delay one opening. None was observed: all openings
  since 2026-11-19 10:00 were generated within 4.7 s (max), and 0 on 2026-11-20 exceeded 5 s.
- **Historical opening delays.** Before about 2026-11-19 02:30, live-model openings were generated
  5 to 64 s after the open (median about 12 to 15 s on 2026-11-17/18). This did not leak
  information, because the probability is an exact function of inputs received at or before the
  open (section 1). But a user acting on the alert then traded at a later price than the open.
- **Missing inputs.** 169 of 320 openings lacked 10 or more of the 87 inputs. The micro (5 s to
  1 m) and spot-book inputs need a continuous tick window with no gap over 5 s, receipt-proven by
  the open. Missingness is highest in US hours (mean 19 to 24 missing at 15 to 21 UTC, against 8
  to 10 at 09 to 12 UTC). It is handled correctly (neutral value plus a missing flag) and was not
  associated with worse skill: the tercile with the fewest missing inputs scored worst.

## 5. Good and bad periods

| Date | Mean missing inputs | Brier skill vs base | UP rate | Mean P(UP) |
|---|---|---|---|---|
| 11-16 | 5.2 | +0.0386 | 0.55 | 0.484 |
| 11-17 | 16.2 | +0.0193 | 0.47 | 0.488 |
| 11-18 | 21.9 | +0.0017 | 0.40 | 0.489 |
| 11-19 | 7.4 | +0.0003 | 0.44 | 0.486 |
| 11-20 | 10.1 | -0.0015 | 0.42 | 0.509 |

- Skill fell steadily with model age: +0.018, then +0.008, then -0.007 across age terciles
  (rho -0.12, p = 0.027; not significant after correcting for 14 tests).
- Missing inputs went up and then down while skill only fell, so missing inputs do not track skill.
- Higher confidence went with slightly better skill (rho +0.13, p = 0.022; also not significant
  after correction).
- Nothing technical changed immediately before deteriorations. The decline is gradual and
  consistent with an aging model whose early run was lucky.
- UP calls were right 56.5% of the time (46 calls) and DOWN calls 69.7% (76 calls), during a
  period when BTC settled UP only 43% of the time.

## 6. Data freshness and missing features

- Live predictions: median cadence 1.8 s (p95 6.5 s, p99 11 s); data age at record median 0.53 s
  (p95 5.6 s).
- Worst hours were 2026-11-17 17:00 to 23:00 (p95 gap 12 to 16 s), during the model's good period.
- Outages: 2026-11-19 08:00 to 10:00 (2 h), and 21:20 to 22:16 (the candle-gap incident fixed in
  `e8e8808`). The about 62 s gaps at each quarter hour are contract changeovers, not outages.
- Candles: no internal gaps in any of 369 frozen 1,440-bar histories. The last bar ends 60 s
  before the open (120 s in 4 openings).
- Reference age at the open: median about 0.4 s.
- Clock: chrony offset 5 microseconds.
- Kalshi threshold publication: 0.9 to 1.8 s after the open (measured earlier).
- No process degradation with uptime was found: there are no stage timings that worsen with
  process age. Restarts were frequent because of deployments.

## 7. Model drift and calibration

- Training labels end 2026-11-12 06:30, so the model is 8.5 days old. The lifecycle retrains every
  15 min but every new candidate loses to the training prior on its untouched test block, so no
  newer model exists.
- Calibration slope: 2.63 in the first half (underconfident), 0.94 in the second, 0.91 over the
  last 100 openings.
- Input spread grew: the logistic logit sd rose from 0.42 to 0.56, led by `distance_scaled`, which
  stayed predictive, and the 5 s to 30 s micro returns, which lost their relationship with the
  outcome.
- Market information: refit before each opening, the market price alone (Brier 0.2435) was not
  improved by adding the live model (0.2497).

## 8. Repairs and commits (this audit)

- `27b68e7`: per-exchange live price series; fixes the final-minute live estimate.
- `2eadb96`: read-only accuracy health monitor (`src/signals_display/accuracy_health.py`, 10
  tests). Run on production read-only, it reported technical DEGRADED (latest opening missing 16
  inputs; this threshold was later raised to 31, above the 28 stream-dependent inputs) and
  predictive NO VERIFIED EDGE.
- Long-run replay test (4 h, interleaved exchanges, 20 s outages): continuity returns within
  121 s after each outage, and buffers stay bounded.
- Earlier this session: `cfa7706`, `117ca3c`, `c08fb8c` (pre-registration), `b04f3bb`.

## 9. Test results

- Linux CI on `2eadb96`: see section 14 (it was running when this was written).
- Focused local tests: 37 passed (live model, per-source series, health monitor, activator).
- Previous full runs: `c08fb8c` 1853 passed, 14 skipped, 0 failed.
- `test_phase5_opening_repair::test_previously_valid_openings_are_computed_exactly_as_before`
  fails on this Windows PC both with and without my change (platform-specific). It passes on
  Linux CI.

## 10. Accuracy and profitability, with uncertainty

| Set | n | Live Brier | Base rate | Market price | Live minus market (95% CI) | Live after-cost P&L per contract |
|---|---|---|---|---|---|---|
| All with quote | 355 | 0.2422 | 0.2483 | 0.2462 | -0.011 to +0.002 | +0.087 (n=119; earlier CI -0.010 to +0.183) |
| Before freeze | 303 | 0.2420 | 0.2490 | 0.2470 | -0.013 to +0.002 | +0.095 (n=103) |
| After freeze (descriptive only) | 52 | 0.2436 | 0.2438 | 0.2419 | -0.012 to +0.016 | +0.038 (n=16) |
| Last 100 settled (monitor) | 100 | 0.2485 | 0.2413 | | vs base -0.024 to +0.010 | +0.009 (CI -0.14 to +0.16, n=43) |

- Call accuracy: 64.8% (122 calls, CI 56–73%) on the dashboard's scoreboard. Counting the 9
  pre-alert fallback calls from 2026-11-16 (3 correct), which the scoreboard excludes under its
  pre-declared confidence rule, it is 62.6% (131 calls).
- By regime (volatility terciles, trending vs sideways, US vs other hours): no row shows a
  credible advantage over the market. With 12 rows, one nominal interval excluding zero would be
  expected by chance.
- **Conclusion: no demonstrated predictive edge beyond Kalshi's own opening price after costs.**
  The frozen forward test decides on the two pre-registered challengers after 200 new openings
  (about 2026-11-22 08:16 UTC).

## 11. Remaining risks

- Single-CPU host at about 50–64% use by 8768. A burst (backfill, heavy page loads) could slow
  openings.
- A long `Retry-After` 429 could delay an opening by up to about 60 s. This was not observed
  recently.
- Model aging. No retrained candidate passes the gate, so the live model keeps aging.
- Small samples. All accuracy statements carry wide intervals. About 100 calls cannot separate a
  5-point edge from luck.

## 12. Prioritised next actions

1. Score the frozen forward test after 2026-11-22 08:16 UTC (`scripts/Run-ForwardEval.ps1`).
2. Optionally deploy `27b68e7` (live final-minute estimate) in a normal guarded 8768 deployment,
   only after separate approval.
3. Run the health monitor periodically, read-only (`python -m src.signals_display.accuracy_health
   --journal … --artifact …`). Treat its NO VERIFIED EDGE as the honest default label.
4. Research only, with a new pre-registration: a consensus reference (average of exchanges) and
   inputs normalised for volatility; any candidate must be compared against the market price, not
   only the base rate.
5. Capacity: profile the 8768 CPU use before adding any feature.

## 13. Deployment recommendation

**NO-GO for deploying anything new right now.** Production is healthy and the opening path has no
confirmed defect. The only behavioural fix (`27b68e7`) touches the live intra-contract estimate,
not opening calls, and deploying during the forward test changes the live process, though not the
model or its inputs. It is safe to deploy later with the standard guarded procedure if you want
the final-minute live probability back.

## 14. CI result

(Filled in below after the run completed.)

---

# Repair results (2026-11-20, implementation pass)

## Resolution table (every finding)

| # | Finding | Severity | Root cause | Status | Tests | Commit |
|---|---|---|---|---|---|---|
| R1 | Live final-minute settlement estimate unavailable (`MISSING_AVERAGING_HISTORY`) | Medium | One live buffer, cleared on every Coinbase/Kraken source change | **FIXED AND VERIFIED.** Real-tick replay: averaging history available in 2/178 contracts before, 162/178 (91.0%) after | `test_live_reference_per_source.py` (4, incl. a 4 h replay with outages; 2 failed before) | `27b68e7`, `1601d54` |
| R2 | **Coinbase websocket reconnect loop** (new in this pass): 34,339 reconnects in 15.7 h, 78,203 false gaps, 803,395 discarded snapshot trades | **High**: wiped micro/flow windows about every 1.6 s, the main cause of missing micro inputs; also wasted CPU | `sequence_num` is per connection (verified on the live feed: strictly +1 across all channels), but was checked per channel | **FIXED AND VERIFIED.** Live feed, 60 s each: old 38 reconnects, 229 messages; new 0 reconnects, 802 messages. Stream CPU 2.77 s/min down to 0.75 s/min | `test_coinbase_connection_sequence.py` (4; 3 failed before); 3 streaming fixtures moved to real numbering | `046b9c7` |
| R3 | **Kraken spot session resets** (new): 2,294 in 15.7 h vs 6 for futures | **High**: reset spot CVD/flow/micro windows about every 25 s | Real dust trades below 1e-6 BTC were rejected; the exception reconnected the socket | **FIXED AND VERIFIED.** Reproduced against the live Kraken feed (2 resets in 150 s, both dust trades) | `test_kraken_dust_trade.py` (5; 1 failed before) | `6cae642` |
| R4 | Missing micro inputs (28–30 on some openings) | Medium | R2 and R3 (window continuity reset by reconnects) | FIXED at the root (R2, R3). Post-deploy measurement in "After deployment" | via R2, R3 | — |
| R5 | 8768 CPU about 50–70% of one core | Medium (capacity) | Per-update flow summary scans of 20,000 records per source (about 280–306 ms per cycle); stream reconnect overhead | **FIXED (partly), VERIFIED.** Exact-output binary-search slicing (13% faster per summary); reconnect overhead removed (R2, R3). Post-deploy measurement below | `test_advanced_flow_equivalence.py` (7, byte-identical outputs vs the original filtering) | `0fbff05` |
| R6 | Last closed candle 120 s old at 4 openings | Low | Refresh landed on the minute boundary before Coinbase published the just-closed bar; the next refresh came after the open | **FIXED AND VERIFIED.** 5 s retry when the newest closed bar is behind the current boundary. Never uses an open bar | `test_candle_refresh_after_boundary.py` (2; failed before) | `9a9a6ea` |
| R7 | 60 s candle lag at the open | — | Intended: the minute ending at the open is not complete until after the open; the final minute is covered by reference/micro inputs | INVESTIGATED: NO DEFECT | — | — |
| R8 | Accuracy reporting / health monitor | — | Needed a separate data-health vs edge view and both scoreboard populations | **FIXED AND VERIFIED.** `/api/accuracy-health` (cached 60 s, read-only) and a dashboard panel: dashboard scoreboard and all directional calls, with every excluded call listed (9 fallback calls, 3 correct) | `test_accuracy_health.py` (14), `test_dashboard_accuracy_health.py` (2); rendered from production data | `2eadb96`, `2893aa0` |
| R9 | Opening reference exchange chosen by latest tick | Low | Design; measured effect 0.024 sigma | INVESTIGATED: NO ACTIONABLE DEFECT. Changing it would alter a trained input; left for a pre-registered research candidate | — | — |
| R10 | Micro tracker rejects Coinbase trades whose event time precedes the latest ticker (single last-event clock across kinds; 2.6 M rejections) | Low–medium | Single ordering clock for quotes and trades | REQUIRES MORE DATA / separate approval. A fix changes how trained micro inputs are computed (training-serving consistency); measure after R2 first | — | — |
| R11 | HTTP 429 near quarter hours | Low | Provider throttling; backoff honours Retry-After and resets on success | INVESTIGATED: NO DEFECT (correct behaviour); no delayed opening observed | — | — |
| R12 | Model skill decline, early run above validation | — | Regression to near-zero validated ability plus aging; no pipeline bug (probabilities reproduce exactly, labels and thresholds verified) | REQUIRES MORE DATA. Frozen forward test decides after 200 openings; no model promoted | `forward_eval_openings` tests | `c08fb8c` |
| R13 | Streaks | — | Chance (runs p=0.33) | INVESTIGATED: NO DEFECT | — | — |
| R14 | Activator false alarm after deployment; retirement WAL race | Low | (earlier) | FIXED AND VERIFIED | (earlier) | `cfa7706`, `117ca3c` |

## Important consequence for the live model (read before deploying)

R2 and R3 make the micro/flow inputs present far more often. The live model was trained when they
were often missing, and it handles both cases (neutral value plus a missing flag), so this is not a
model or threshold change. But live probabilities may shift slightly, because inputs that were
absent will now carry real values. Watch the opening P(UP) distribution and call coverage after
deploying.
