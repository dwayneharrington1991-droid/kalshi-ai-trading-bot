# Pre-registered forward test of opening challengers (frozen 2026-11-20 06:15 UTC)

Research only. Nothing here changes the live model `b50b9dc30f6d664c`, the 0.55 threshold, the
journal, alerts or production 8768. Automatic promotion on Oracle is disabled (2026-11-20 05:47 UTC).

## Why
In-sample and walk-forward research on 2026-11-20 (docs/research-20261120-edge-decay.md) tried more
than ten variants. Over the 113 openings from 2026-11-18 22:30, the best (core16) had Brier 0.2390
vs the live model's 0.2489, but its 95% interval for the difference (-0.032 to +0.013) included zero,
and choosing the best of many inflates it. The only honest test left is on openings that had not
happened when the candidates were fixed.

## Frozen definitions (`scripts/forward_eval_openings.py`; this commit is the freeze)
- **dist**: L2 logistic (ridge 1.0) on `distance_scaled`.
- **core16**: L2 logistic (ridge 20.0) on the live artifact's 16 price_core inputs, using the live
  model's own preprocessing (missing input = neutral).
- Refit before every opening on openings that opened at least 30 minutes earlier. Both were settled
  by then.
- Compared with the live model's immutable P(UP) and a past-only base rate.

## Decision rule
- Only openings with `opens_at >= 2026-11-20T06:15Z` count. At least **200** are required, about
  2 days of openings.
- A candidate passes only if BOTH of these hold against the live model on the same openings:
  - the 95% bootstrap interval of (candidate Brier minus live Brier) is entirely below zero;
  - the 95% interval of after-cost P&L per $1 contract is entirely above zero. Trades use the 0.55
    call rule, buying at the quoted ask nearest the open plus Kalshi's quadratic fee.
- Passing makes a candidate eligible for a separate promotion review, which needs your approval. It
  never deploys anything. A changed candidate is a new experiment with a new freeze time.

## How to run (read-only)
1. Export the journal tables with the read-only research export.
2. Run
   `python scripts/forward_eval_openings.py --export journal_dump.json --artifact <live artifact json>`.

The script prints the report and writes nothing else.
