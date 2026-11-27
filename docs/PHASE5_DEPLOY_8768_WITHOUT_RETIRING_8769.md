# Deploying the Phase 5 performance upgrade to preview 8768 (revision 3)

Author: Claude. Revision 2 resolves the six blockers from Codex's DEPLOY HOLD
on 839a79c. Not executed on Oracle. Requires Codex PASS and owner approval.

## Why 8769 need not be retired first (unchanged)

8768 and 8769 share no port, journal, directory, code path or lock; 8769's
commit `7ba1763` is already inside the 8768 build. They share only the single
CPU core and memory, which the upgrade reduces (offline: cycle CPU -54%,
prediction calculation -61%, identical outputs). The gates below measure the
real effect on Oracle and roll back automatically if it is not met.

## The deployment is ONE Oracle-owned job; all gates run inside it

`deploy_job_8768.sh start` launches `deploy_preview_8768.sh` detached (own
session, nohup, SIGHUP ignored) under the exclusive preview-8768 lock, plus a
detached watchdog. The Windows client only starts and watches; closing it or
losing the network changes nothing.

1. **Preflight** (`preview_gates_8768.py preflight`, before ANY change; failure = `BLOCKED`, nothing changed):
   load1 <= 12.0 (observed 7.1-8.4 on 1 core); MemAvailable >= 768 MiB;
   SwapFree >= 1024 MiB; swap activity <= 200 pages/s over 10 s; disk free
   >= 3 GiB; not inside an opening window (90 s before to 150 s after a
   quarter hour); exactly one readable listener on 8765 and 8767 (PID and
   start time recorded); the current 8768 CPU share measured over 20 s as the
   baseline.
2. **Install + switch + verification** (existing): exact 40-char build,
   HEALTHY, signals-only, same artifact/model/threshold/inputs/journal,
   execution capability disabled, 8765/8767 unchanged; readiness must hold
   for 3 consecutive full probes within 600 s.
3. **Soak, 15 minutes, rollback armed** (`soak`; every 10 s on a fixed cadence):
   every sample must have `/healthz` 200 HEALTHY, workers healthy,
   signals-only and `build_commit` EXACTLY the 40-character commit;
   `/readyz` 200 READY with fresh quotes and reference; `/api/state` 200 with
   measurable quote/reference ages and latency meters (any unsuccessful or
   unmeasurable response fails immediately); age never above 10 s and <= 5 s
   in >= 95% of samples; at EVERY sample whose meters hold >= 100
   measurements: publish-after-collection p95 <= 3000 ms and p99 <= 4500 ms,
   prediction p50 <= 1020 ms (a breach at any point fails, not only at the
   end); >= 80% of samples must have that much latency data and >= 90% of
   the expected samples must be taken; the 8768 listener PID constant; 8765
   and 8767 identities unchanged.
4. **Postflight** (`postflight`): 8765/8767 identities unchanged;
   MemAvailable >= 512 MiB; SwapFree >= 768 MiB; swap activity <= 200
   pages/s; load1 <= 14 and <= preflight + 2.0; new 8768 CPU over 60 s <=
   preflight baseline + 5 points and <= 60%; final `/healthz` HEALTHY on the
   exact build.
5. **SUCCESS** is written only here (the orchestrator has a single SUCCESS
   exit, after soak and postflight). Any failure in 3 or 4 runs the existing
   guarded restore: it stops ONLY this run's new build (identified by
   `--build-commit <sha>`), restarts the exact previous process (argv, cwd,
   full environment from the private snapshot) and verifies it: `ROLLED_BACK`.

Rollback stays armed until SUCCESS even if the orchestrator dies: the
watchdog detects a dead orchestrator without a result and requests the
guarded `recover`, which never marks SUCCESS without a fresh full soak and
postflight, and otherwise restores the previous build. A client disconnect
has no effect at all (the job is server-owned).

## Codex blockers on 839a79c and their resolution

| # | Blocker | Resolution |
|---|---|---|
| 1 | Soak not inside an Oracle-owned job with automatic rollback | Soak and postflight run inside the detached orchestrator under the lock; failure -> commit-bound guarded restore; watchdog covers orchestrator death; recover re-soaks. |
| 2 | Latency only checked at the end | Checked at every sample with enough data; a mid-soak breach fails (tested with a breach that recovers before the end). |
| 3 | No minimum sample counts | >= 100 measurements per meter for a statistic to count; >= 80% of samples must qualify; >= 90% of expected samples. |
| 4 | Failed /api/state tolerated | Any non-200, non-JSON, missing field or non-numeric value fails immediately. |
| 5 | Prefix build match | Exact 40-character equality (soak rejects shorter input; `/healthz` must equal the full commit). |
| 6 | Manual resource/window/listener checks | Automated preflight and postflight with the thresholds above. |

## Revision 3: adversarial review by Claude (Codex unavailable)

This is Claude's own adversarial review of the whole workflow, NOT an
independent Codex review. Four defects were found and fixed, each of which
would have caused a false rollback on Oracle that the simulation could not:

1. Latency history: the server keeps its last 300 cycles per stage, so at
   soak start the percentiles still contained slow startup cycles. Latency
   limits now apply at every sample once >= 300 cycles were published after
   the soak began (server `state_version`), i.e. on soak-only data; >= 50% of
   samples must reach that point (a build too slow to refresh fails).
2. Contract rollover: every 15-minute soak crosses a quarter hour, where the
   active contract changes and `/readyz` is briefly NOT_READY. From 20 s
   before to 60 s after a quarter hour, only contract-quote reasons are
   tolerated (no active contract / not open / expired / stale or missing
   quote); health, workers, signals-only, exact build, `/api/state` 200,
   latency meters, a fresh reference (<= 10 s) and protected identities stay
   strict on every sample, and freshness is scored on the other samples.
3. The gates' HTTP helper discarded the JSON body of 503 responses, so
   `/readyz` reasons were unreadable (verified against a real local server).
4. Protected identity included the process run state (R/S/D), which flaps;
   it is now PID + kernel start time only.

CPU postflight was also made realistic for four CPU-bound processes sharing
one core: new 8768 CPU <= preflight baseline + 10 points and <= 40%.

Verified against a real signals server: every field the soak reads exists
(`/healthz` workers/build/signals_only, `/readyz` 503 JSON with
stale_reasons, `/api/state?view=main` runtime.state_version, market_health
ages, latency meters with p50/p95/p99/samples).

## Verification (local; never Oracle)

- `bash -n` and `shellcheck -S warning` clean for every deploy script (three
  pre-existing SC2155/SC2034 warnings fixed).
- Orchestrator and job rehearsals with real processes on 8768/8765/8767:
  success only after soak and postflight; rollback to the exact previous
  process (same environment) for: unready, slow latency, mid-soak latency
  breach, `/api/state` 500 inside the soak, too few latency samples, short
  build inside the soak, postflight CPU and memory failures; preflight
  blocks with no install for a missing protected listener and for memory,
  load, disk and swap limits; overrides refused outside test mode; opening
  window boundaries; watchdog recovers a SIGKILLed orchestrator with no
  client; client-requested recovery still works.
- Full suite: 1408 passed, 9 skipped.

## After Codex PASS

A paste-ready Windows command for the exact approved commit is generated
from `deploy/windows/Deploy-Preview8768.ps1` (`-Sha <commit>`, `-Branch
claude/phase5-deploy-failure-repair`). Expected duration about 25-30 minutes
(install, up to 10 minutes readiness, 15-minute soak, postflight); the client
waits up to 45 minutes and can be resumed with `-ResumeJobId`.
