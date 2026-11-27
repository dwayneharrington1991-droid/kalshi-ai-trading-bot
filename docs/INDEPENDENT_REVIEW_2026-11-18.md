# Independent review and deployment diagnostics (2026-11-18)

Reviewer: Claude, acting as the independent review agent. Branch
`claude/independent-review-2026-11-18`, based on `2839785`
(`claude/phase5-deploy-failure-repair`). Codex's branches were not touched.
Oracle was not accessed from the sandbox (no SSH); Oracle facts come from the
read-only evidence collected on 2026-11-18 (job log, latency report, process
profile, service inventory).

## Root cause of the Phase 5 preview failure (job 01997b30, ae05d37)

1. Immediate cause: the switch's readiness gate broke out on the FIRST passing
   probe and re-probed once; a single marginal re-check failure was a hard
   failure (the job lasted ~55 s, not the 600 s timeout it reported). Fixed
   in d61b0e0 (3 consecutive full passes, true elapsed time reported).
2. Underlying cause: one CPU core at load ~8 shared by four Python dashboards
   (8765 42%, 8767 22%, 8769 22%, 8768 21%). The long-running old 8768 build was
   already publishing quotes 6-8 s old before the switch, so freshness sat on
   the 5-second boundary. The prediction cycle re-validated the same 1,440
   candles several times per cycle and recomputed the paper summary on every
   request. Fixed in b6af8ad and 8c40a10 (identical outputs; cycle CPU -54%,
   prediction -61% offline), deployment gates in 1e3ad9e and 2839785.

## Findings of this review

| Area | Result |
|---|---|
| SHA pinning | Exact 40-char commit enforced by client, job (commit-bound), installer (HEAD == target, clean checkout), switch and soak (exact `build_commit`). OK. |
| Races | Exclusive deployment lock for the whole sequence; atomic job creation; pidfd-based stop in the retirement script; watchdog for orchestrator death. OK. |
| Installer / startup / environment | Old process argv, cwd and full environment captured privately and restored with `env -i`; artifact preserved by sha256; new code adds no third-party runtime imports (numpy only inside a research-only function). OK. |
| Rollback | Guarded restore stops only this run's new build; previous process restored exactly; failure paths end in ROLLED_BACK or MANUAL_RECOVERY_REQUIRED with steps. OK. |
| Opening leakage | New independent tests: post-open bars, post-open reference receipts and histories received after the opening never change the decision; a late history is rejected; delayed generation changes only timing metadata; pre-open data does change the result (tests not vacuous). PASS. |
| Immutable openings | First write wins; UPDATE/DELETE blocked by triggers. PASS. |
| Settlement scoring / WAIT | WAIT/UNCLEAR and uncalibrated calls are excluded from verified accuracy and counted separately; never scored as wrong. PASS. Cosmetic: `quick_learning_summary` reports `unclear_openings` equal to `wait_openings`. |
| 1438/1440 preload stall | Root cause: a minute that both Coinbase candle endpoints omit and the trade endpoint confirms had NO trades cannot be filled (no-interpolation policy), so the 24 h window stays INCOMPLETE and openings are withheld until the minute ages out (up to 24 h). Meanwhile the refresh retried every 5 s for the whole period (~17,000 refreshes/day, each re-requesting candle ranges and the trade lookups). FIXED here: confirmed-empty minutes are re-checked at most every 10 minutes and the refresh uses the 60 s cadence when only confirmed-empty minutes are missing. No bar is ever invented; the window still reports INCOMPLETE. The policy question (whether to represent a verified zero-trade minute as a zero-volume bar) is left to the owner/Codex because it changes model inputs. |
| Stale quotes / readiness | Freshness logic unchanged (5 s). Staleness on Oracle was CPU contention (above). Readiness now cheap (no full snapshot) and the gate requires sustained passes. |
| Promotion gates | First shadow evaluation needs 170 settled frozen openings; gradient boosting 500. Current eligible count requires a read-only Oracle query (not possible from the sandbox). |
| Out-of-sample validation | Methodology (walk-forward, sealed holdout, Wilson intervals, multiple testing) covered by the test suite. Re-running on real settled contracts needs the Oracle journals: BLOCKED here. No accuracy improvement is claimed. |
| Dashboard | Countdown uses server-computed `remaining_seconds` minus local elapsed since the response (error of at most one request latency, ~0.1-0.4 s). Scalp signals expire 5 s after the quote, so on the contended host (6-8 s quote ages) they could never be valid; the performance repair addresses that. |
| Signals-only | No order/POST code added relative to 6c19623; execution capability must be False in every deployment check; soak requires `signals_only` on every sample. PASS. |
| Historical data | No DELETE/UPDATE/DROP of historical tables; only additive `CREATE TABLE IF NOT EXISTS`; deployment requires the same journal path. PASS. |

## Commits on this branch

- see `git log 2839785..claude/independent-review-2026-11-18`
  (independent tests + confirmed-empty candle backoff + this report).

## Tests

- New independent tests: `tests/test_independent_review_opening.py` (13).
- Candle pipeline tests: 19/19.
- Full suite: 1421 passed, 9 skipped (8 real-money live tests + browser
  tests without Playwright), 0 failed.

## Remaining blockers, by severity

1. HIGH (operational): single-core contention. The new build must pass the
   on-host 15-minute soak; that cannot be proven offline. Retiring 8769
   (script PASSED by Codex at 839a79c) first adds ~22% CPU margin.
2. HIGH (process): commits after 1e3ad9e (2839785 and this branch) have only
   Claude's review; Codex should review them when available.
3. HIGH (evidence): out-of-sample accuracy on real settled contracts was not
   re-run in this pass (no journal access). No improvement is claimed.
4. MEDIUM (policy): verified zero-trade minutes withhold openings for up to
   24 h by design. Decide whether a verified zero-volume bar is acceptable
   model input (requires retraining/validation if adopted).
5. MEDIUM: eligible-contract count vs the 170/500 gates unknown until a
   read-only Oracle query runs.
6. LOW: `unclear_openings` duplicates `wait_openings`; six legacy deploy
   scripts outside the deployment path have ShellCheck warnings.

## Instructions for Codex

1. `git fetch origin claude/independent-review-2026-11-18`.
2. Review the diff against `2839785` (one source file:
   `src/signals_display/monitor.py`, candle refresh backoff; one new test
   file; this document). It is a fast-forward of
   `claude/phase5-deploy-failure-repair`; integrate by fast-forward or
   cherry-pick after review. The deploy target then becomes this branch's
   head; `2839785` remains a valid target if the backoff is deferred.
3. Run `python -m pytest -q tests/test_independent_review_opening.py
   tests/test_signals_mission_integration.py` and the full suite.

## Go / no-go

- Guarded PREVIEW 8768 deployment: GO, conditional on the owner's approval
  (and preferably after retiring 8769). Every failure mode found or tested
  rolls back automatically to the current build; nothing touches 8765/8767,
  journals, models or trading.
- PRODUCTION (8765): NO-GO. Out of scope until the upgraded preview has
  passed its soak and run stably, the model-promotion gates are met on real
  settled contracts, and Codex has independently reviewed the final changes.
