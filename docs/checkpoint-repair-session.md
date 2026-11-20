# Repair session checkpoint

Branch: fix/opening-selection-learning

## Commits this session (all pushed)
- 845a142 settlement intake reporting
- 7701d58 retirement-test teardown (stops the 7 leftover-process failures)
- 12787ee release handoff
- 6160dac live export analysis
- db56801 rejected walk-forward threshold experiment
- 33996bd UNCLEAR/WAIT trace and feed timing findings
- 0431cbf abstention recorded as WAIT (safety state kept in confidence)

## Verified
- Opening model, prediction engine, guard evidence and independent review: 62 passed after 0431cbf.
- Retirement tests (tests/test_phase5_retire_8769.py): 21 passed.

## Open
- Full suite run 4 (log /tmp/claude-0/full-suite-run4.txt) showed one failure near 50%. Name not yet confirmed; the summary line prints at the end of the run.
- Intermittent deploy-orchestrator hang: not reproduced in isolation (52/52 pass in 229 s); root cause not established.
- BTC reference: the 5-second gate is enforced; 6 of 300 openings were withheld on it. Records are generated median 10 s after open (p99 121 s); publish-time visibility is not in the export.
- Model improvement: the only threshold rule tested did not beat the recorded direction (rejected, docs/live-export-analysis-20261119.md).
- Persistent worker: not established (no SSH key, no attached computer folder).
- Installer command: not prepared; blocked on a green full suite and a release commit pushed to origin.

## Latest verification (current head)
- Full suite on eb8747d: 1828 passed, 9 skipped, 0 failed (966 s).
- The intermittent deploy-test hang did not recur in this run; root cause still not established.
- 0431cbf (UNCLEAR→WAIT engine change) broke test_previously_valid_openings_are_computed_exactly_as_before and test_per_input_status_is_metadata_only_versus_the_approved_base. It was reverted in eb8747d. The headline dashboard card already maps non-UP/DOWN openings to WAIT, so the immutable record keeps its original label.

## Second session (local Windows workstation, Opus 5.5), started 2026-11-20 00:05 UTC
Two sessions are pushing to this branch. Always fetch and rebase before pushing; never force-push.
- e8e8808 ROOT CAUSE of the 21:30-22:15 withheld openings (read-only journal on Oracle): candle 21:19 UTC missing from both Coinbase candle APIs; trade recovery failed because RFC3339 start/end returns HTTP 500 (Unix seconds work) and the minute had 2,477 trades (> one page). Fixed with paged Unix-second recovery + tests.
- b3c8c3d CI: per-test timeout 300 s, job limit 40 min (full suite takes ~16 min, which exceeded the old 15-minute job limit).
- a081878 Dashboard display-only WAIT mapping (market history, predictions table, settlement outlook). Records unchanged.
- 2bc07b1 Collector polls every 0.25 s for 6 s after each quarter-hour boundary (probe: Kalshi publishes the threshold within ~1 s; live delay was 2-3 s).
- Draft PR #4 exists only to run Linux CI. PR-triggered "Deploy Oracle production" runs are test-only (production job requires workflow_dispatch on main) but share the oracle-production concurrency group, so they were cancelled.
- Model evidence: live candidate over 281 out-of-sample openings Brier 0.2418 vs past-only base rate 0.2505 (CI of difference just excludes 0); second half shows no edge (0.2462 vs 0.2467). Fresh retrain on latest 61-contract block loses to the train prior; gate correctly keeps NO_CANDIDATE. Nothing promoted.

## Release verification (merged head)
- Head: ce5680d (includes djhalyze commits e8e8808, b3c8c3d, a081878, 2bc07b1 rebased under the checkpoint).
- Full suite on ce5680d: 1840 passed, 9 skipped, 0 failed (960 s).
- Production build on 8768 is a269d08 (ancestor of this head).
