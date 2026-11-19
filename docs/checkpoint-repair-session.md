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
