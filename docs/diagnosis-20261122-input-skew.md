# Opening accuracy at 61%: diagnosis (2026-11-22 00:15-00:45 UTC)

Read-only. Nothing in production, the journal, the scoreboard or tests v1/v2 was changed.

## What the 61% is

`/api/opening-scoreboard` at 00:18 UTC: **110 correct / 179 UP-DOWN calls = 61.5%** (95% CI 54.2-68.3%),
lifetime since the first live call (2026-11-16 21:45 UTC). 384 openings were WAIT (|P-0.5| < 0.05) and
are excluded; 0 unsettled, 0 invalid, 0 duplicates. Every call is an immutable opening record matched to
the official Kalshi result (32/32 of the last 8 hours re-checked against the Kalshi API).

| Period (all live, never trained on) | Openings | UP rate | Live Brier | Base-rate Brier | Calls | Accuracy | UP calls | DOWN calls |
|---|---|---|---|---|---|---|---|---|
| 11-16 21:45 - 11-20 06:00 | 309 | 0.437 | 0.2423 | 0.2460 | 106 | 65.1% | 22/37 | 47/69 |
| 11-20 06:00 - 11-21 00:00 | 72 | 0.403 | 0.2492 | 0.2405 | 26 | 57.7% | 6/14 | 9/12 |
| 11-21 00:00 - 11-22 00:15 | 97 | 0.474 | 0.2531 | 0.2493 | 47 | 55.3% | 16/32 | 10/15 |
| Lifetime | 478 | 0.439 | 0.2456 | 0.2463 | 179 | 61.5% | 44/83 (53%) | 66/96 (69%) |

- The fall is from new losing calls, not scoring or display: the counts above reproduce the scoreboard.
- 65.1% (106) vs 56.2% (73) since 11-20 06:00: z = 1.2, p = 0.23. **Not statistically significant on its own**,
  but the model's Brier has been worse than a constant base rate since 11-20.
- UP calls are the weak side over the whole life (53%); DOWN calls 69% while only 44% of markets settled UP.
- Confidence is not informative above 0.55: P in [0.55, 0.60) 27/52 right, P >= 0.60 17/31 right.
  Below 0.45 it is (52/73 right for P in [0.40, 0.45)).

## Root cause found: input skew after deployment 9a9a6ea

The Coinbase/Kraken stream fixes (regression ledger L6/L7, deployed 2026-11-20 20:21 UTC as approved)
made 30 micro-structure and trade-flow inputs (all `micro_*`, `advanced_trade_intensity`,
`advanced_effective_spread`) always present and computed over complete windows. Before, the reconnect
loop left them missing 37-59% of the time and reset their windows constantly - and that is how they were
recorded in the live model's training data.

From the first opening after the deployment (20:30) the inputs step change, e.g. standardized
`advanced_trade_intensity` 0.3-2 before, 5-8 after; missing rates 0.37-0.59 -> 0.00
(local analysis scripts, outside the repo). The live model's components (reproduced exactly, max error 1e-16):

| Component (weight ~0.25 each) | mean P(UP) before | after |
|---|---|---|
| logistic, all 87 inputs | 0.504 | **0.613** |
| ridge, 16 price inputs | 0.475 | 0.465 |
| stumps (GBM) | 0.487 | 0.487 |
| volatility baseline | 0.501 | 0.496 |

The logistic component alone moved +0.52 logit; the ensemble average P(UP) rose 0.492 -> 0.516 while the
UP rate barely moved (0.433 -> 0.459), so more openings crossed 0.55 as UP calls. The Accuracy Health
panel flagged this as DRIFTED (mean shift 0.84 SD) but nothing acted on it.

Separately, real market conditions changed: 24-hour volatility rose smoothly to +2.6 SD and the 24-hour
return fell to -2 SD (candle inputs unaffected by the stream fix).

## What masking would do (outcome-free where outcomes are reserved)

Since 20:22 (112 openings; outcomes not inspected because they belong to forward tests v1/v2):
- treating the 30 skewed inputs as missing changes 27 calls; UP/DOWN/WAIT 35/18/59 -> 20/28/64;
- dropping the logistic component changes 35 calls.

Before 11-20 06:00, when these inputs were computed the old way, both variants were slightly worse than
live (masked Brier +0.0023, CI [+0.0001, +0.0043]; no-logistic +0.0018, CI [-0.0007, +0.0044]). So there is
no evidence yet that either is better; they are pre-registered as forward test v3
(`docs/preregistration-v3-20261122.md`) and scored only on openings from 2026-11-22 01:00 UTC.

## Other checks

- R10 (single event clock in the micro tracker): still ~1.3 M rejected Coinbase updates since restart;
  affects only the micro inputs above.
- Feed interruptions: none material in the last 8 h; Kalshi REST read timeouts 3-13 per hour are absorbed
  by the cached-payload path. Opening calls generated 2-4 s after every open, no gaps, no overwrites.
- Tests v1 and v2 are affected by the same skew for all models; their rules are unchanged.

## Follow-up 2026-11-23: does fixing the skew restore accuracy? (`scripts/research_align_window_20261123.py`)
Window: 124 settled openings from 2026-11-20 20:30 to 2026-11-22 03:15. This is after the input change and before
the v4 freeze, so it is not reserved for v4. The alignment used inputs only from this window, never outcomes.
43.5% of these markets settled UP; the base-rate Brier is 0.2458.

| | Calls | Right | UP share of calls | Avg P(UP) | Brier |
|---|---|---|---|---|---|
| AI #1 live | 58 | 51.7% (UP 18/40, DOWN 12/18) | 69% | 0.518 | 0.2546 |
| AI #1 with quantile-aligned inputs (AI #2 mechanism) | 48 | 50.0% (UP 10/23, DOWN 14/25) | 48% | 0.495 | 0.2532 |
| AI #1 before the change (369 openings) | 126 | 64.3% | 38% | 0.492 | 0.2433 |

- Aligned minus live Brier is −0.0013, with a 95% bootstrap CI of [−0.0065, +0.0040].
- **Confirmed:** the train/serve skew creates an UP bias. The UP share went from 38% to 69% of calls (z ≈ 4).
  Alignment removes the bias.
- **Not restored:** accuracy and Brier stay near coin-flip level with aligned inputs. The accuracy drop itself
  (64.3% of 126 calls to 51.7% of 58) is not statistically distinguishable from market variation (p ≈ 0.10).
  Part of the strong start was early-sample luck (Nov 17: 29 calls, 72%).
- **Action:** no production change. The v4 forward test still decides about 05:30 UTC Nov 24. The cure for
  consistency is training on the same full-window inputs the live bot computes, which is the v5 protocol.

## Root-cause test of the decline (2026-11-23, `scripts/research_fix_candidates_20261123.py`)
These are the frozen v3/v4 corrections run on non-reserved windows only.

| Window | Variant | Calls | Right | Accuracy | UP share | Brier | ΔBrier vs live (95%) |
|---|---|---|---|---|---|---|---|
| Pre-change, 369 openings (inputs match training) | live | 126 | 81 | 64.3% | 38% | 0.2433 | |
| | masked (30 skewed inputs removed) | 126 | 78 | 61.9% | 35% | 0.2441 | +0.0008 [−0.0012, +0.0029] |
| | no_logistic | 109 | 67 | 61.5% | 32% | 0.2435 | +0.0001 [−0.0022, +0.0025] |
| Skew window, 114 openings (after change, before v3 freeze) | live | 53 | 29 | 54.7% | 66% | 0.2527 | |
| | masked | 48 | 26 | 54.2% | 42% | 0.2532 | +0.0005 [−0.0072, +0.0079] |
| | no_logistic | 40 | 20 | 50.0% | 35% | 0.2516 | −0.0011 [−0.0105, +0.0083] |
| | aligned (AI #2) | 46 | 23 | 50.0% | 46% | 0.2520 | −0.0007 [−0.0060, +0.0047] |

**Conclusion.**
1. The train/serve skew is a real bug, and it explains the UP bias. All three corrections remove it.
2. The skew does **not** explain the accuracy decline. Even before the change, the 30 inputs added almost
   nothing (masked 61.9% vs 64.3%). After the change, every corrected variant is still at 50–54%, with Brier
   near the base rate. The early 64% came from the other inputs in a mostly-DOWN market (Nov 16–20, 44% UP;
   DOWN calls 54/78 right), plus small-sample luck. That edge did not persist.
3. **Smallest consistent repair:** "masked", which uses only inputs computed identically in training and
   serving. It restores train/serve consistency and an honest UP/DOWN balance, with no measurable change in
   accuracy. Promoting it would change live predictions, so it requires the pre-registered v3 result (about
   03:00 UTC Nov 24) and explicit approval. Consistently better accuracy needs a model retrained on full-window
   inputs (v5), judged on unseen markets.
