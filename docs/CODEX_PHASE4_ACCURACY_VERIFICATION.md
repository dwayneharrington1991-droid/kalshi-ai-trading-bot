# Phase 4 independent accuracy verification

Status: repository audit complete; live Oracle evidence unavailable in this
workspace. No deployment, activation, threshold change, journal write, or
trading operation was performed.

## The reported 18 correct / 8 incorrect

The arithmetic is correct: 18 / 26 = **69.23%**. Its Wilson 95% interval is
**50.01%–83.50%**. That interval includes chance and does not establish that
69.23% will persist. The 26 immutable rows, official settlement receipts,
deployed artifact, and explicit cohort boundary are absent from this
workspace, so the claim is **NOT INDEPENDENTLY VERIFIED** here.

The read-only settled audit is the required verifier. It scores only immutable
UP/DOWN records with an explicitly displayable confidence state, reports every
exclusion, checks receipt/cutoff chronology, and joins official outcomes. It
now quarantines conflicting or invalid official outcomes rather than silently
using the last row returned by SQLite.

## Findings

1. The deployed-artifact diagnostic already checks component loading, finite
   outputs, opening-time input coverage, feature and component drop
   sensitivity, and post-label-cutoff performance. It now requires the saved
   opening's model identity to match the artifact before calling a probability
   reproducible. Calls from other versions are reported separately.
2. “Four components” and “87 inputs” are hypotheses to verify against the
   artifact, not repository facts. The report emits actual component/input
   counts and whether the artifact has exactly 87 inputs. A defined feature is
   not necessarily present, retained, weighted, or influential.
3. Phase 3 configured eight fitted candidates despite the preregistered limit
   of six. Regime and pre-open market price are now references/conditions,
   leaving six fitted candidates and reducing multiple-testing risk.
4. Historical failure diagnostics could include calls that were never
   displayable or whose direction contradicted their probability. They now
   use the same eligibility rules as the live-call score.
5. A 0.55 threshold cannot be justified from a headline accuracy. The new
   challenge reports development-only alternatives and opens the chronological
   sealed holdout only for the predeclared 0.55 policy. Regime slices remain
   exploratory and cannot select production policy.
6. Twenty-six calls cannot support an 80–90% claim. Even 18 correct leaves a
   50.01% lower confidence bound. Nonstationarity and overlapping BTC regimes
   can widen effective uncertainty further.

## What limits accuracy

- Small live directional sample and potentially shifting market regimes.
- Most historical probabilities cluster near 0.50–0.55, indicating weak
  separability rather than a threshold-only problem.
- Missing pre-open executable Kalshi quotes prevents comparison with the
  strongest market baseline and prevents credible after-cost results.
- Feature availability is not equivalent to contribution. Missing inputs can
  be mean-imputed and effectively silent.
- Artifact reproduction is impossible when model IDs or exact frozen inputs
  do not match the deployed artifact.
- Selective thresholds trade coverage for apparent accuracy and must be fixed
  before untouched evaluation.

## Decision rules for Claude candidates

Accept for further shadow evaluation only when the candidate uses the same
chronological contracts and sealed holdout, improves proper scores versus the
current artifact and simple baselines, remains calibrated, and survives
multiple-testing correction. Reject candidates based on retrospective feature
reconstruction, final-holdout tuning, tiny high-confidence subsets, or an
accuracy gain accompanied by materially worse Brier/log loss.

Additional evidence needed before a safe upgrade:

- The 26 raw immutable opening records and official outcomes.
- The exact deployed artifact and model ID attached to every call.
- At least 100 high-confidence calls for any 80% claim, with Wilson lower bound
  at least 80%; current evidence is far below this bar.
- Sufficient pre-open quote coverage for paired market and after-cost tests.
- A frozen candidate evaluated once on the same untouched period as the
  current model.

## Comparison points for Claude Phase 4

- Report exact contract identities and cohort boundary behind 18/8.
- Report actual component/input counts, input coverage, drop sensitivity, and
  matching-artifact reproduction rate.
- Compare fixed 0.55 on the sealed holdout; keep alternative threshold and
  regime research on development data.
- Report accuracy with Wilson interval, coverage, Brier, log loss, ECE, and
  executable-quote after-cost results on identical contracts.
- Treat no pre-open market quote as missing evidence, never as a free fill or
  successful market comparison.
