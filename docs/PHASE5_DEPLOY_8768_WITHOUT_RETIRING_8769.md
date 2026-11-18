# Deploying the Phase 5 performance upgrade to preview 8768 without retiring 8769

Author: Claude (implementation). Independent assessment requested by the owner;
this is NOT Codex's review. Nothing here has been executed on Oracle.

## Verdict: YES, with the gates below

There is no functional dependency between 8768 and 8769:

| Possible coupling | 8768 | 8769 | Shared? |
|---|---|---|---|
| Port | 8768 | 8769 | no |
| Journal | `upgraded-live-8768.db` | `repair-test-8769.db` | no |
| Directory / release | `kalshi-preview-upgrade` + new release root | `kalshi-gap-repair-stage` | no |
| Code | target build contains `7ba1763` already | runs `7ba1763` | 8769 tests nothing 8768 lacks |
| Deployment lock / tooling | `preview-8768` job + lock | not referenced | no |
| CPU (1 core) and memory | yes | yes | **only shared resource** |

The deployment switch stops the old 8768 process before starting the new one
(one 8768 process at a time), so memory demand does not double. The only
risk 8769 adds is CPU contention, which is exactly what the upgrade reduces:

- Measured on Oracle (old build 6c19623, 21% of the core):
  publish-after-collection p50 2.59 s, p95 4.25 s, p99 5.15 s; prediction
  calculation p50 1.70 s. The old build already passes readiness, but only
  intermittently.
- Measured offline with realistic inputs (identical outputs): cycle CPU
  169 -> 78 ms (-54%), prediction calculation 148.5 -> 58.4 ms (-61%);
  per-request paper summary 79 -> 1.1 ms; /readyz no longer builds the full
  dashboard; background analytics run at nice 10.
- Expected at the same CPU share: roughly half the publish latency (p95
  around 2-2.5 s). This is an extrapolation; the soak gate below measures it
  on Oracle and rolls back if it is not met.
- The readiness gate (d61b0e0) now requires 3 consecutive full passes within
  600 s, so a warm-up flap no longer aborts after ~55 s, and nothing is relaxed.

Retiring 8769 later remains worthwhile (about 22% CPU, 570 MiB) but is not a
precondition.

## Preconditions

1. Codex PASS for the exact target commit, covering the performance code
   (b6af8ad, 8c40a10; already PASSED) AND the readiness-gate change in
   `deploy/oracle/switch_preview_8768.sh` (d61b0e0) and this soak gate. If
   the readiness-gate change was not part of the performance review, it must
   be reviewed first.
2. A paste-ready Windows command generated for that exact commit (the
   existing `deploy/windows/Deploy-Preview8768.ps1` with `-Sha` and
   `-Branch claude/phase5-deploy-failure-repair`), with the Windows PowerShell
   5.1 tests re-run on the owner's PC.
3. Owner authorization. Production 8765 is never touched; 8767 and 8769 are
   left running.

## Gates

### G1 Pre-deploy baseline (read-only, about 70 s)
Run the existing process profile (`collect_process_profile.sh`, commit
b6af8ad command). Proceed only if ALL hold:
- 8768 (old build) answers HEALTHY;
- host available memory >= 1.0 GB and swap is not growing between vmstat samples;
- 1-minute load <= 10 (observed 7.1-8.4);
- no opening window in progress (the switch also enforces 90 s before to 150 s after each quarter hour).
Record the 8765 listener PID, the 8768 baseline percentiles and 8768 CPU.

### G2 Deployment job (automatic, already implemented)
The server-owned job (disconnect-safe, exclusive lock, commit-bound) passes
only if: `/healthz` HEALTHY, workers healthy, signals-only and build == target;
`/readyz` READY with fresh quotes AND reference (5-second rule unchanged) for
3 consecutive probes within 600 s; candle integrity 1440 bars (or a verified
frozen opening); execution capability disabled; 8765 and 8767 listener PIDs
unchanged. Any failure restores the exact previous 8768 process automatically
(`FAILED_PREVIOUS_STILL_RUNNING`).

### G3 Post-deploy soak (read-only, 15 minutes, strict, automated)
On Oracle, immediately after `SUCCESS`:
```bash
bash /home/opc/kalshi-preview-repair-<sha7>/deploy/oracle/verify_preview_8768_soak.sh <sha> 15 <8765 pid from G1>
```
PASS (exit 0) requires, over every 10-second sample for 15 minutes:
- HEALTHY, workers healthy, signals-only, build == target; READY with fresh quotes and reference;
- quote and reference age <= 5 s in >= 95% of samples and never > 10 s;
- publish-after-collection p95 <= 3000 ms and p99 <= 4500 ms (old: 4254 / 5154);
- prediction calculation p50 <= 1020 ms (60% of the old 1698 ms);
- the 8765 listener PID never changes.
Then re-run the G1 profile once: 8768 CPU must not exceed the G1 value by
more than 5 percentage points and load must not rise by more than 1.0.

### G4 Rollback (on any G3 failure, or owner request)
```bash
bash /home/opc/kalshi-preview-repair-<sha7>/deploy/oracle/deploy_job_8768.sh rollback <job id> <sha>
bash /home/opc/kalshi-preview-repair-<sha7>/deploy/oracle/deploy_job_8768.sh status <job id> <sha>
```
This is the existing commit-bound, locked rollback of a successful job: it
restores the exact previous process (argv, working directory, full
environment from the job's private snapshot) and verifies it. Afterwards
confirm 8768 answers with build 6c19623, HEALTHY and READY, and that 8765's
PID is unchanged. No journal is modified by deployment or rollback.

## What this plan does not do
- It does not stop, restart or modify 8765, 8767 or 8769.
- It does not change the model, the 87 inputs, the 55% threshold, journals
  or trading (signals-only throughout).
