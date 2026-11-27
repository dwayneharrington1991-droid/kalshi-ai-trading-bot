# Order-flow research competition

Run one target per immutable JSONL export and fixed JSON spec. Use
`python scripts/research_order_flow_value.py --records development.jsonl --spec comparison.json --out report.json`.
The output path must be new. The runner uses development decisions strictly before
2026-11-20 06:00 UTC. Keep V3/V4/V5 exports and any new protected forward window separate.
The scope is offline research. The deployed model, five-line dashboard, seven-day scoreboard
and Telegram alert cadence are outside the comparison.

## Fixed comparison

- `price_only` is AI A, the price-feature control fitted with regularized logistic regression.
- `price_walls` is AI B and adds only eligible wall and resting-depth fields.
- `price_walls_trades` is AI C and adds only eligible executed-trade fields.
- `recorded_baseline` is the actual immutable baseline probability and call. The score uses the
  stored UP, DOWN or WAIT instead of applying the candidate threshold retrospectively.
- `combined_research` is a versioned equal-weight mean of A, B and C on the same decisions.
  Its weights are fixed and the three estimates are treated as correlated.

Set `competition: true`, `threshold: 0.55`, `flow_max_age_s`, `eval_start`, `eval_end`,
`min_train`, `l2` and `candidates` in the spec. Names in the candidate lists must use
`price.`, `walls.` and `trades.` prefixes in the nested order above. Freeze the spec before
evaluating. All three fitted models use the same training and evaluation intersections.
Only labels received strictly before the decision enter training.

| Target | Decision cutoff | Earliest valid label receipt | Required label origin |
| --- | --- | --- | --- |
| `opening_final` | Contract opening | Opening + 900 s | `official_kalshi` |
| `first6` | Contract opening | Opening + 360 s | `verified_btc_reference` |
| `minute6_final` | Opening + 360 through 367 s | Opening + 900 s | `official_kalshi` |

Every row requires a stable sample and market ID, `opens_at`, `decision_at`, `label_observed_at`,
`label_origin`, `label`, `baseline_probability`, `baseline_generated_at`,
`baseline_model_version`, `baseline_call` and the `families` object. Include
`volatility_regime` when it was available at decision time. Opening records published later
than the strict decision cutoff are ineligible for this competition. Exclude flat BTC moves under
the existing FIRST 6 MIN scoring rule and separately report the count. Preserve the export's
eligible market inventory so an absent record can be distinguished from WAIT or a missing family.

## Read-only handoff

Claude should export one row per decision from the immutable journals and include a manifest
with source hashes, market identity, feature versions, original exchange and collector
timestamps, data quality flags, and official outcome provenance. The wall and trade families
must report `event_at`, `received_at`, `quality: "VERIFIED"` and their numeric values.
Retain per-venue evidence and continuous trade coverage separately in the manifest. The
runner checks timestamps and declared provenance. It cannot authenticate the export or recover
information that the collector never recorded. The compact per-second research table alone
does not establish point-in-time trade coverage or independently verify official settlement.

The `order-flow-competition-v1` payload provides sample size, latest scored decision,
correct, wrong, wrong UP/DOWN, WAIT, missing from the matched sample, directional accuracy
and coverage, Brier, log loss, reliability bins, Wilson intervals, trailing call windows,
and daily, regime and baseline-version splits. It reports both matched and all-exported-market
coverage. It includes paired Brier differences for adding walls and then trades.
Bootstrap intervals are descriptive because adjacent contracts can be dependent. A favorable
development comparison should lead to a freshly preregistered forward study and separate approval.

## Experiment status

The synthetic fixtures exercise identical samples, exact cutoff boundaries, stale-family
withholding, chronological label purging, and actual recorded WAIT scoring. There is no
genuine matched export in the checkout. Real candidate rankings and deployment readiness
remain unmeasured.

## Independent OF1 exporter review

Keep the published OF1 specs and dates frozen. At upstream commit `12bae51`, the exporter
uses the FIRST 6 MIN lock time for features. That lock can occur 10–90 seconds after
opening. The opening-final export similarly uses the generated time, which can be after
opening. Neither export establishes strict opening-time eligibility. The exporter fills the
exchange event field from the per-second aggregate timestamp and replaces absent observations
with numeric zero in several fields. A fresh latest book cannot establish continuous executed
trade coverage over earlier windows. Its baseline export carries a probability without the
stored direction or model version, and its final-settlement lookup does not check the conflict
table.

Claude should retain the existing observational capture, keep OF1 decisions frozen, and
review these gaps before using OF1 to make a promotion claim. For a strict follow-up, use
separately preregistered markets and export genuine event and receipt timestamps, full
window-coverage flags, the stored call and model version, and quarantined official outcomes.
If the underlying collector or historical logs lack that evidence, mark the family unavailable.
