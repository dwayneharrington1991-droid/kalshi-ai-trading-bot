# Regression ledger (permanent)

One row per confirmed defect: what failed, why, the regression test that pins it, the fix commit and
the deployment state. Add a row for every new confirmed defect.

Deployment categories:
- **A**: technical or presentation change that does not change prediction behaviour. May be
  auto-deployed after all quality gates.
- **B**: affects features, data selection, timing, coverage or probabilities. Needs your approval.
- **C**: model, calibration, threshold or training change. Needs your approval.

| ID | Date | Defect | Root cause | Regression test | Fix | Cat. | Deployed |
|---|---|---|---|---|---|---|---|
| L1 | 11-19 | One missing Coinbase minute withheld openings for an hour | Trade recovery sent RFC3339 (HTTP 500) and rejected pages over 1,000 trades | `test_trade_recovery_pagination.py` | `e8e8808` | A | 3ce9071 |
| L2 | 11-20 | CI runner killed mid-suite | Retirement script disabled its own systemd unit | `test_own_unit_never_stopped.py` | `3ce9071` | A | 3ce9071 |
| L3 | 11-20 | Activator reports "RESTORE NOT VERIFIED" after a deployment | Pointer swapped before checking that the restart capture still matched | `test_stale_restart_template.py` | `cfa7706` | A | 9a9a6ea (promotion disabled on Oracle) |
| L4 | 11-20 | Retirement capture aborts at random | SQLite removed -wal/-shm between gate and capture | `test_retire_capture_wal_race.py` | `117ca3c` | A | n/a (script) |
| L5 | 11-20 | Live final-minute estimate always missing | Single live buffer cleared on every exchange switch | `test_live_reference_per_source.py` (4 h replay) | `27b68e7` | A | 9a9a6ea; verified live |
| L6 | 11-20 | Coinbase reconnect loop (34,339 per ~16 h) | Connection-wide sequence numbers checked per channel | `test_coinbase_connection_sequence.py` | `046b9c7` | B (more micro inputs present) | 9a9a6ea (approved "deploy everything"); 0 reconnects after |
| L7 | 11-20 | Kraken spot session resets (2,294 per ~16 h) | Dust trades below 1e-6 BTC rejected, which reconnected the socket | `test_kraken_dust_trade.py` | `6cae642` | B | 9a9a6ea; 0 resets after |
| L8 | 11-20 | Flow summary scanned 20,000 records per source each update | Full list filters | `test_advanced_flow_equivalence.py` (identical output) | `0fbff05` | A | 9a9a6ea |
| L9 | 11-20 | Opening froze a 120 s-old candle history (4/442) | Refresh landed before the just-closed minute was published; next refresh after the open | `test_candle_refresh_after_boundary.py` | `9a9a6ea` | A | 9a9a6ea |
| L10 | 11-20 | Accuracy Health panel invisible | `main>:not(#minimal-opening){display:none!important}` hid it | `test_dashboard_accuracy_health.py::test_panel_is_not_hidden_by_the_minimal_layout` | `14ddedd` | A | c3e92a5 |
| L11 | 11-20 | Panel showed Kalshi/Coinbase as n/a | `feed_health` exists only in `monitor.snapshot()` | `test_live_connection_fields_come_from_the_served_snapshot` | `ca2cbe6` | A | 8c087de |
| L12 | 11-20 | Main card did not say which settlement it predicts or how the last locked call scored | Display gap | `test_dashboard_opening_goal.py` | `b1469ea` | A | b1469ea |
| L13 | 11-22 | Live model fed out-of-distribution inputs since 9a9a6ea; ensemble shifted toward UP (logistic component mean P(UP) 0.50 -> 0.61) | Training data recorded the 30 micro/flow inputs under the reconnect-loop computation (37-59% missing, reset windows); L6/L7 fixes changed their distribution | `test_forward_eval_v3.py` (candidate reproduction) | Not fixed in production: candidates pre-registered (v3); needs approval | C | no
| L14 | 11-22 | Input drift (L13) was flagged only inside the Accuracy Health panel, so it went unnoticed for ~30 h | No warning on the opening card | `test_dashboard_connection_status.py::test_live_connection_and_drift_warning` | `5e1c3d1` | A | 36f1b0c (2026-11-22 03:40Z) |
| L15 | 11-22 | Stale dashboard could not be told apart: lost local SSH tunnel vs stale production data | Card only said "Connection lost" | `test_dashboard_connection_status.py` (node-run: unreachable / stale flag / frozen publication) | `5e1c3d1` | A | 36f1b0c (2026-11-22 03:40Z) |
| L16 | 11-22 | Training could not tell reset-window from full-window stream inputs (root cause of L13) | No provenance on opening records; FEATURE_VERSION unchanged by 9a9a6ea | `test_stream_computation_provenance.py` | `9c88e74` (stamp + optional `dataset(stream_computation=)`) | A (metadata) | 36f1b0c (2026-11-22 03:40Z) |
| L17 | 11-22 | R10: 68% of valid Coinbase trades rejected by the micro tracker and advanced flow; no corrected data existed to train on | Shared quote/trade clock + newest-first batches | `test_r10_shadow.py`, `test_micro_event_clock_r10.py` | this commit: isolated corrected pipeline records corrected inputs per opening in `r10_shadow.sqlite3` (kill switch `KXBTC_R10_SHADOW=0`); live inputs unchanged | A (shadow data only) | 92d054a (2026-11-22 06:40Z) |
| L18 | 11-22 | Opening card lacked ticker, official threshold, countdown and WAIT coverage; shadow AI panel hidden by the minimal layout | Display gaps | `test_dashboard_connection_status.py` (market/score/shadow) | this commit | A | 92d054a (2026-11-22 06:40Z) |
| L19 | 11-22 | Order book, trade flow, momentum, short-horizon forecasts and contract EV were computed but not shown (or hidden) | Display gap | `test_market_pulse.py` | this commit: `/api/market-pulse` + visible MARKET PULSE panel; horizons scored prospectively, NOT VALIDATED until 500 forecasts with 95% lower bound > 50%; edge stays UNVERIFIED without a validated probability | A (research display) | 92d054a (2026-11-22 06:40Z) |
| L20 | 11-22 | Only one AI call visible; the challenger (v4) call for the same market was not shown | Display gap | `test_challenger_view.py` | this commit: CHALLENGER AI card + `/api/challenger-opening` (recomputed from the immutable opening record, reproducibility-checked, fixed per market, labelled NOT VALIDATED) | A (research display) | 92d054a (2026-11-22 06:40Z) |
| L21 | 11-22 | CI failed on f7b29f1..6ff74df (never deployed) | R10 kill switch added `import os` to server.py, which two guard tests forbid | existing guards `test_write_access_is_absent_from_public_adapter_and_server`, `test_dashboard_panel_is_read_only_text_and_the_route_is_registered` | `cfc4b89` (switch read in `r10_shadow.from_journal`) | A | 92d054a (2026-11-22 06:40Z) |
| L22 | 11-22 | Market pulse called SELLING PRESSURE from Coinbase's 1-level ticker book (imbalance -0.999 on 0.0003 BTC) | Coinbase stream provides top of book only | `test_market_pulse.py::test_top_of_book_only_venues_are_shown_but_not_used` | `98f66ff` | A | 98f66ff (2026-11-22 07:55Z) |
| L23 | 11-22 | First minute-6 lock (20:06 UTC) had price/momentum components INELIGIBLE | Coinbase public candles endpoint publishes ~150 s late, after the 7:00 lock deadline | `test_minute6.py::test_waits_for_the_late_minute6_candle_then_uses_it`, `test_merge_bars_*`; minute closes from the server's own 15 s trade bars (4e42efd) |
| L24 | 11-22 | Lifecycle NO_CANDIDATE on every cycle since 11-19 (579 markets) | `_load_exposed` recomputed the conservative 'all seen contracts are exposed' set from the CURRENT rows each cycle and never saved it, so every new contract counted as exposed and no 30-contract forward block could form | `test_model_lifecycle.py::test_exposed_baseline_is_persisted_so_later_contracts_count_as_unseen`; baseline persisted once (needs lifecycle cron re-pin, awaiting approval). Diagnostic run with 30 forward markets: candidate 18/30 (60%), Brier 0.248 -> EXPERIMENTAL_FALLBACK_RETAINED, i.e. even when scored no candidate beats the gate yet |
| L25 | 11-22 | Routine lifecycle cycles sent a Telegram 'training cycle completed ... NO_CANDIDATE' every 15 min | notify('training_completed') on every cycle with new data | `test_routine_training_cycles_send_no_phone_message` (needs re-pin) |

## Open items (not defects, or awaiting data/approval)

- R10 shadow vs live inputs (inputs only, no outcomes; 14 paired openings 06:45-09:30 UTC Nov 22):
  live trade_flow_imbalance is buy-skewed at short windows (median 15s 0.66 / 30s 0.46 live vs -0.04 / 0.09
  with R10), and trade intensity reads about half (4.3 vs 8.0 trades/s). The rejected trades are not
  random, so live flow inputs are biased, plausibly adding to the UP skew of L13. Separately, every
  micro_*_depth_imbalance is identical (-0.907) across all windows in both pipelines: it comes from
  Coinbase's 1-level ticker book (same source as L22) and carries no depth information. No live change;
  feeds v5/R10 model design (exclude depth_imbalance from Coinbase, train on R10 flow). Re-check at >=100.
- R10: measured on the live Coinbase feed 2026-11-22 (2 min): 0 duplicates, 0 stale, 0 future-dated, yet
  935/1,384 valid trades (68%) rejected. Two causes: quotes are stamped with message time and trades with
  (earlier) execution time on one shared clock, and market_trades batches arrive newest-first. Candidate
  fix = `MarketIntelligence(per_kind_event_clock=True)` + `StreamHub(sort_trade_batches=True)`: 661/661
  valid trades kept (per-kind clock alone 673/1,384; ordering alone 424/661). OFF in production: it
  changes micro inputs (category B) and the live model was trained without it.
- Exchange choice for the opening reference (latest tick wins): measured effect 0.024 sigma, no
  change.
- Model edge: frozen forward test v1 (`c08fb8c`) DECIDED 2026-11-22 08:33 UTC on 201 openings: NO CANDIDATE
  PASSED. Live Brier 0.2513 (83 calls, 55.4%, -1.5c/contract, CI -12.6c..+8.8c) vs base rate 0.2467,
  distance-only 0.2438 (137 calls, 59.1%), core16 0.2651; no CI excludes zero. Report:
  forward/20261122T083252Z/report.json (local). 168 of the 201 openings were after the 9a9a6ea input skew.
- L13 input skew: forward test v3 (`docs/preregistration-v3-20261122.md`) decides after 200 openings from
  2026-11-22 01:00 UTC (about 2026-11-24 03:00 UTC).
