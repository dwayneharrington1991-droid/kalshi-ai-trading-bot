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

## Open items (not defects, or awaiting data/approval)

- R10: the micro tracker uses one event clock for quotes and trades, rejecting delayed Coinbase
  trades. A fix changes how micro inputs are computed: category B, measure first.
- Exchange choice for the opening reference (latest tick wins): measured effect 0.024 sigma, no
  change.
- Model edge: the frozen forward test (`c08fb8c`) decides after 200 openings (about 2026-11-22
  08:16 UTC).
- L13 input skew: forward test v3 (`docs/preregistration-v3-20261122.md`) decides after 200 openings from
  2026-11-22 01:00 UTC (about 2026-11-24 03:00 UTC).
