# Opening selection and learning: release handoff

Branch: `fix/opening-selection-learning` (base `ae40d8e`). Production 8768 has NOT been changed by this work.

## Verified changes

1. **Settlement intake is reported** (`src/signals_display/model_lifecycle.py`, commit `845a142`).
   Each learning cycle now reports official settlements that arrived after the training cursor,
   both eligible and ineligible, with counts by eligibility reason. Before this, a cycle with zero
   new eligible rows was silent. The training decision is unchanged.
   Tests: `tests/test_lifecycle_settlement_intake.py`.
2. **Regression tests for the opening candidate path** (`tests/test_opening_candidate_defects.py`).
   - A missing fresh BTC reference withholds the probability and never shows a candidate call.
   - A candidate artifact that fails validation is rejected on the record and never partially applied.
   Both pass with the current code. They pin existing safe behaviour; they do not change it.
3. **Retirement test teardown** (`tests/test_phase5_retire_8769.py`, commit `7701d58`).
   The seven failures were leftover test server processes from earlier runs, not retirement logic.
   Teardown now kills everything started from the test's own temporary directory and waits on it.

## Test results

- `tests/test_phase5_retire_8769.py`: 21 passed (after clearing leftovers and after the teardown fix).
- Focused opening/lifecycle suites (`test_opening_candidate_defects`, `test_lifecycle_settlement_intake`,
  `test_signals_opening_models`): 27 passed.
- Full suite on the working tree after the fix: see the final message for the count
  (run log: `/tmp/claude-0/full-suite-final.txt`). The previous clean run was 7 failed, 1821 passed,
  9 skipped; all 7 failures were the retirement-test leftovers above and are now resolved.

## Measured model performance

**Not measured in this session.** The live journal and a faithful feature export are not available
in this sandbox. No walk-forward model comparison, calibration, regime breakdown, or fee/spread-aware
profitability result exists for this branch. No model was promoted. The 80–90% accuracy target has no
evidence behind it and is not claimed. The user-quoted baseline (63.83% accuracy, 32.75% coverage) was
not found in any tracked file and has not been reproduced.

## Unresolved

- **Calibration and directional bias** of the opening probabilities: needs the live journal export
  (`scratchpad/export/export_remote.sh`, read-only) and a chronological walk-forward study.
- **Why the last three openings fell back**: the candidate-to-fallback transition is consistent with the
  fallback withholding its probability (stale BTC reference gate, 5 s). Whether the feed was stale at the
  time needs the collector trace from the live host. The gate itself must stay.
- **Fallback-model quality** (zero-drift Gaussian): unmeasured against candidates.
- **Combined AI installer** (`Install-Combined8768.ps1`): ON HOLD, never run, not part of this release.
- **Credit balance**: the assistant cannot read the user's remaining usage balance.

## Release commit and install

The install uses the existing guarded client `deploy/windows/Deploy-Preview8768.ps1`, which runs the
server-owned job `deploy_job_8768.sh` → `deploy_preview_8768.sh` (preflight, switch, 15-minute soak,
postflight, automatic rollback on failure). It targets only port 8768. Ports 8765 and 8767 are only
observed. The client has no `Read-Host`; it starts the job and polls it.

The release commit must be an ancestor of `origin/fix/opening-selection-learning`. Push first.

## Rollback

Automatic: `deploy_preview_8768.sh` rolls back to the exact previous process on any failed
verification.

Manual, if needed (on Oracle, as `opc`):

```bash
/home/opc/kalshi-preview-repair-<previous-sha7>/deploy/oracle/deploy_preview_8768.sh status
/home/opc/kalshi-preview-repair-<previous-sha7>/deploy/oracle/deploy_preview_8768.sh rollback <previous-40-char-sha> <run-id>
```

Recover a job that stopped midway by resuming it with the job id printed at start:

```powershell
.\Deploy-Preview8768.ps1 -Sha <sha> -Branch fix/opening-selection-learning -ResumeJobId <job-id>
```
