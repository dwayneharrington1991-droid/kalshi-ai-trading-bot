# 8768 shadow upgrade (experimental, not deployed)

This work is shadow-only. The running 8768 bot, its model, its 87 inputs, its 55% threshold, its journal and its settings are not changed. The live bot never imports the new module (a test enforces this). There is no activation path, and the bot stays signals-only.

## What was built

| Piece | File | Purpose |
|---|---|---|
| Phase 1 audit | `deploy/oracle/audit_8768_readonly.sh` | Read-only audit of 8768, sections A–G (below). |
| Experimental models | `src/signals_display/shadow_upgrade.py` | Adds models and a sealed-holdout evaluation (below). |
| Oracle runner | `deploy/oracle/shadow_upgrade_8768.sh` | Runs the audit, then the evaluation against the 8768 journal opened read-only. |
| Tests | `tests/test_shadow_upgrade.py` | Leakage, no-false-claims, nonlinear learning, end-to-end on the real journal schema, read-only guards. |

### Phase 1 audit sections

- **A.** Deployed build compared with the branch.
- **B.** Model SHA-256, threshold and input count.
- **C.** Workers, readiness, data freshness and latency.
- **D.** Verified accuracy, plus the split of openings into UP, DOWN and WAIT.
- **E.** The previous guarded deployment jobs and the exact gate each one failed.
- **F.** Whether 8768 depends on 8765.
- **G.** 8765 storage inventory.

### What the experimental module adds

It adds three things to the existing `opening_research` pipeline, which it reuses for loading, label purging and scoring:

- **Gradient-boosted trees:** written in numpy, depth 3, shrinkage 0.05, L2 leaves, minimum leaf size 20. Early stopping uses a purged, chronological tail of the training block. It runs over every frozen as-of-opening feature family: momentum, EMA/RSI/MACD/VWAP/Bollinger, volume, candles, order book/CVD flow, derivatives basis, trend/regime interactions and Kalshi pre-open price. LightGBM and XGBoost are not installed. This in-repo version is the same model family, with no new dependency.
- **A wide regularised logistic model and a Platt-calibrated ensemble.**
- **An accuracy-target frontier for 70, 80, 85 and 90%:**
  - The threshold is chosen on development (walk-forward out-of-sample) predictions only.
  - The last 20% of contracts form a sealed holdout, scored once at that threshold.
  - Each target reports holdout accuracy, Wilson 95% interval, coverage (the share of markets given an UP/DOWN call), Brier score and log loss.
  - The running AI's displayed calls on the same holdout contracts are reported beside it, with a matched-coverage comparison.
  - The report also includes per-regime accuracy and per-prediction latency.

### Pre-registered rules

- Three candidates.
- Threshold grid from 0.50 to 0.95.
- At least 30 development calls before a threshold can be chosen.
- One selected model: the lowest development log loss. The others are labelled exploratory.

## Honesty rules built into the report

- **Labels:** "VERIFIED" appears only when the Wilson lower bound on the holdout reaches the target. A point estimate at the target is labelled as such.
- **Tests that guard against false claims:**
  - Flipping every holdout label leaves all thresholds and predictions unchanged, so there is no leakage.
  - On no-signal data, 85% and 90% are never verified.
- **No real numbers in the repository yet.** All numbers produced here use synthetic data and say nothing about live accuracy. Real numbers come only from running the Oracle command on the 8768 journal.

## What 90% would require

The last verified live record was 39/57 = 68.42%. Verifying 90% with a 95% lower bound means, for example, about 96 correct out of 100 calls. That has to be on contracts the model never saw, and at a coverage high enough not to be trivial. The frontier reports exactly how much coverage each accuracy level costs.

## 8765 dependency (from code review)

The deployment gates require that 8765 is *running and unchanged* (same PID and start time); they do not require it to be *healthy*. 8765 being unhealthy therefore does not block the 8768 deployment. Retiring 8765 later will require one separate, approved gate change: treat 8765 as optional-observed, as was done for 8769. Audit section F checks the runtime dependencies:

- shared files;
- sockets;
- the interpreter;
- the artifact path;
- environment variable names (values are never printed).
