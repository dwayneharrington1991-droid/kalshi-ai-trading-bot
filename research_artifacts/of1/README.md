# OF1 — order-flow forward test (pre-registered 2026-11-27 03:00 UTC, before any OF1 outcome existed)

Question: do Coinbase/Kraken book walls (AI B) and executed trades + CVD (AI C) add predictive value beyond the
current production probability (AI A, model b50b9dc30f6d664c, unchanged) and a price-only control?

* Targets: opening→final Kalshi settlement, FIRST 6 MIN (BTC open→+6:00), MINUTE-6 FINAL (final settlement;
  features only up to min(lock, open+367 s)).
* Data: only decisions at or after 2026-11-27T03:15Z (collector start 2026-11-27 02:29Z); nothing from the
  V3/V4/V5 holdouts or the development period (< 2026-11-20T06:00Z) is used.
* Features: collector rows at or before the decision, both venues LIVE, newest row ≤ 2 s old; otherwise the market
  is written to `<target>.exclusions.json` (never silently dropped).
* Method: `scripts/research_order_flow_value.py` (Codex) — expanding chronological fit, fold-local preprocessing,
  labels used only after their receipt, all candidates scored on the identical sample intersection, paired bootstrap CI.
* Fixed now: threshold 0.55, l2 1.0, min_train 100, eval 2026-11-29T03:15Z → 2026-12-04T03:15Z.
* Decision rule: a candidate "helps" only if its Brier improvement vs recorded_baseline has a 95% paired CI
  entirely below 0 AND its directional coverage is ≥ 20%. Until then every result is labelled COLLECTING DATA.
* Nothing here changes production. Promotion requires the user's explicit approval.
