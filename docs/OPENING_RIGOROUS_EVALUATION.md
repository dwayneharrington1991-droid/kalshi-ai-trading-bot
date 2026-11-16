# Rigorous opening evaluation (shadow only)

`src/signals_display/rigorous_evaluation.py` is a stricter, separately versioned
evaluation protocol for the KXBTC15M opening model. It runs beside the existing
opening pipeline and **never** promotes, registers or activates anything:

- the journal is opened read-only;
- it writes only its own report and its own ledger, and refuses to overwrite
  any file that is not one of its own (existing artifacts and registries are
  protected);
- its report format cannot be loaded by `opening_models.validate_artifact`.

Run it on a copy of the journal (the command only reads it):

```bash
python -m src.signals_display.rigorous_evaluation --journal signals_monitor.db \
  --output opening_rigorous_shadow.report.json --ledger opening_rigorous_shadow_ledger.json --walk-forward
```

## Protocol

| Concern | Existing opening pipeline | Rigorous shadow protocol |
|---|---|---|
| Approval | Beat baseline and train prior on point estimates (~34 test contracts) | Same, **plus** a one-sided HLN Diebold–Mariano test (Newey–West variance) on per-contract log loss against both references |
| Repeated evaluations | Forward unseen holdouts (Codex) | Forward unseen holdouts **plus** alpha spending: attempt *k* is tested at 0.05·6/(π²k²), so all attempts together stay below 5% |
| Multiple comparisons | 16 linear candidates plus an ensemble ranked on one validation set | Two predeclared candidates plus the baseline; only one reaches the test; requiring both references is an intersection–union test |
| Validation reuse | Calibration, ensemble weights and ranking all on the same rows | L2 chosen by time-series CV inside train; Platt fitted on validation V1; ranking on V2 only |
| Calibration | 150 fixed gradient steps (under-converged) | Existing regularised Newton `calibration.fit_platt` |
| Fitting / L2 | 220 fixed steps; `l2/n` penalty makes the 0.1 vs 1.0 grid nearly inert | Damped Newton to gradient tolerance; un-normalised L2 with grid 0.3…1000 |
| Features | Up to 531 (feature-set grid) | 12 predeclared opening features, Gaussian baseline as an offset |
| Leakage | Receipt-proven frozen features, label purging | Same rows and purging, reused unchanged |

## Evidence: SYNTHETIC studies only

No production journal was available, so **none of this is market evidence**.
Synthetic openings (`rigorous_synthetic.py`) use a calibrated baseline with a
realistic ~4% median tilt. Under the null, labels are drawn from the baseline
itself, so no model can genuinely beat it.

### False approval (no real signal)

| Contracts per trial | Trials | Existing gate approvals | Rigorous gate approvals |
|---|---|---|---|
| 170 | 200 | **29 (14.5%)** | **1 (0.5%)** |
| 400 | 100 | **14 (14%)** | **0** |

Mean untouched-test log loss under the null (baseline ≈ 0.691): the existing
selection scored 0.763 at 170 contracts and 0.706 at 400, i.e. it overfits.
The compact candidates scored 0.702 and 0.696.

### Power (a genuine synthetic signal on one feature)

| Contracts | Signal (log-odds per SD) | Existing approvals | Rigorous approvals |
|---|---|---|---|
| 170 | 0.5 | 18/60 | 1/60 |
| 400 | 0.3 | 9/60 | 0/60 |
| 400 | 0.5 | 29/60 | 0/60 |
| 1000 | 1.0 | — | 3 of 4 seeds |

The trade-off is explicit. The existing gate's approvals at modest signals sit
only slightly above its ~14% false-approval rate, so they cannot be told apart
from luck. The rigorous gate rarely approves until the untouched test reaches
roughly 200 contracts with a strong signal. That reflects how little 34–80
contracts can prove; the threshold was not loosened to compensate.

### High-confidence openings (pooled untouched-test predictions, 25 × 1000 contracts)

| Group | Baseline | Compact, moderate signal (0.5) | Compact, strong signal (1.5) |
|---|---|---|---|
| Highest confidence seen | 64.7% | 90.5% | 99.6% |
| 60–70% | 59 obs, 61.0% acc | 1155, 63.7% | 1080, 64.4% |
| 70–80% | 0 | 272, 68.8% | 1137, 75.8% |
| 80–90% | 0 | 40, 90.0% (CI 77–96%) | 1036, 84.9% (CI 83–87%) |
| 90%+ | 0 | 3 (insufficient) | 663, 94.0% |
| ≥80% coverage | 0% | 0.9% | 34% |
| 80% target demonstrated | No | No | Yes (positive control) |

Conclusions:

1. The statistical opening baseline cannot produce 80–90% calls: its
   confidence never exceeded ~65%.
2. Calibrated 80–90% openings appear only when the features carry a strong
   genuine signal. Whether real BTC opening features do is unknown until this
   protocol runs on the real journal.
3. Where the bands are populated, observed accuracy matches stated confidence
   in every band (calibration consistent).

## Opening WAIT guard

The fallback guard (withhold the direction after 20 openings when the 95%
Wilson upper bound on accuracy is below 60%) is kept unchanged. Two evidence
defects were fixed:

- the guard now judges the fallback on fallback openings only (candidate calls
  were pooled in);
- it scores each fallback opening's probability-implied direction, so withheld
  calls keep counting. Previously the guard froze permanently once it fired.

Is a validation-selected alternative justified for the fallback? Not yet.
Simulation puts the fallback's realistic accuracy near 53–54%, which the guard
correctly treats as too weak to call. Calibrated candidates already use Codex's
validation-selected abstention threshold.
