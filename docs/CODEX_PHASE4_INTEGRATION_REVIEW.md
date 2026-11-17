# Codex Phase 4 integration safety review

Reviewed integration: `5016b07f471c2b587b3b50969e87588b0708a368`.

Verdict for that commit: **BLOCKED pending the review-branch corrections**.
The integration retained the six-candidate limit, settlement quarantine,
model-scoped scoreboard, fixed-0.55 challenge, and matching-artifact
reproduction checks, but the independent review found remaining defects.

## Corrections on the review branch

1. Report B and artifact diagnostics are pinned to the journal used by the
   listener on port 8768. Larger production/legacy journals receive their own
   Report A but cannot be paired with the preview artifact.
2. Running-model comparisons use the canonical display rule: finite in-range
   probability, displayable calibrated status, UP/DOWN direction, and
   direction/probability agreement.
3. McNemar comparisons across candidates receive Holm correction.
4. Alternative threshold tables and regime slices stay on development data.
   The sealed holdout exposes only proper scores and the single threshold
   chosen on development.
5. Candidate-family availability is decided from development rows only.
   Candidates whose incremental features lack adequate coverage are withheld
   rather than silently collapsing to the compact model.
6. A ticker with any invalid official outcome row is quarantined and the
   invalid row count is reported.
7. The supplied Windows research command now requires strict host-key
   verification instead of accepting a new identity.

## Read-only boundary

The research Python entry points open journals with SQLite `mode=ro`. They
write only reports and resumable checkpoints under the research output path.
They refuse to overwrite an artifact path, expose no promotion route, and set
`activation_allowed=false` / `promotion_allowed=false`. The Oracle runner
contains no service restart, process kill, deployment, model-copy, registry
write, or order operation. No trading client is invoked.

## Evidence still required

Repository tests establish computation and safety behavior, not predictive
skill. Before model deployment, Oracle must supply the immutable 8768 calls,
trusted official outcomes, exact deployed artifact, pre-open executable quote
coverage, and the untouched chronological report. No higher-accuracy or
profitability claim is supported merely by merging this code.
