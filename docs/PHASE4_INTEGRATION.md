# Phase 4 integration: Claude + Codex research upgrades

Branch `claude/phase4-integrated-accuracy` merges:

- Claude: `claude/phase4-opening-research` @ `c788be7`
- Codex: `codex/phase4-accuracy-verification` @ `e3f778b`
  (report: `docs/CODEX_PHASE4_ACCURACY_VERIFICATION.md`)

Research only. Nothing here changes the live bot, model, signals, journals
or artifacts. No result in this branch was measured on real Oracle data.

## Incorporated from Claude

- Per-model accuracy table; exact replica of the dashboard's verified
  scoreboard with most-recent 26/50/100 windows, tested against the live counter.
- Report A per running port's journal; fresh `phase4-run` folder.
- Running-model comparison on the same sealed-holdout contracts (paired exact
  McNemar) and at matched coverage.
- Volatility-regime interaction features.
- Resumable checkpoints, vectorised solver, pinned as-of (Phase 3).

## Incorporated from Codex

- Official-settlement conflict quarantine; earliest receipt kept for identical repeats.
- Duplicate / conflicting official-row counts in integrity checks.
- Six pre-registered fitted candidates (regime and Kalshi price as references).
- Failed-call list uses the live display rules.
- Invalid recorded probabilities counted and excluded.
- 0.55 threshold challenge: alternatives on development only; the sealed
  holdout is opened only for the predeclared 0.55 policy.
- `requested_87_inputs_matches_artifact` and reproduction status.

## Integration decisions

- **Candidate limit.** The automatic merge produced seven candidates. Claude's
  interaction candidate and the trend candidate are combined into ONE
  pre-registered `momentum_trend_volatility` slot, keeping six. An import-time
  assertion enforces the limit.
- **Reproduction cohort.** Union of both rules: the label must be
  `<model_id>` or `.../<model_id>` and the opening must be at or after the
  artifact's `available_at`. Mismatches are listed per ticker. Matching labels
  before `available_at` are reported as anomalies. Other-model openings after
  release are counted. Pre-release openings are counted separately.
- **Quarantine everywhere.** One reader (`official_outcomes`) feeds scoring
  AND the market/base-rate/persistence baselines. Report B and retrospective
  loaders drop any contract joined to more than one official row.
- **No silent version pooling.** The 0.55 threshold challenge also runs per
  recorded model; a pooled result is labelled `MIXED_MODEL_VERSIONS_REFERENCE_ONLY`.

## Codex safety review incorporated (`8dbd321`, fast-forward)

See `docs/CODEX_PHASE4_INTEGRATION_REVIEW.md`. In summary:
- Report B and artifact diagnostics are pinned to port 8768's journal.
- The running-model comparison uses the canonical display rule.
- Holm correction is applied across the paired McNemar tests.
- No holdout threshold tables or regime slices: development only.
- Candidate availability is decided on development rows only.
- Any invalid official row quarantines its ticker.
- SSH and SCP use strict host-key verification.

Added on top: per-candidate incremental-feature coverage on development rows
(`candidate_incremental_coverage_development`). A family kept active by one
always-present input, such as a regime indicator, is labelled `PARTIAL_FAMILY`
instead of being silently relabelled.
