# Path to 80-90% opening accuracy: what the evidence says (2026-11-22)

Goal: at each 15-minute KXBTC15M opening, call the official settlement (UP/DOWN) with >= 80% verified
accuracy at meaningful coverage. All numbers below are out-of-sample.

## Where we are
- Live model, lifetime: 110/181 calls right (60.8%, CI 54-68%), calls on ~32% of openings.
- Best non-reserved unseen result (312 openings, docs/research-20261122-coverage.md): 66-71% on the most
  confident 10-33% of openings, lower CI bounds 50-57%. No model or baseline reaches 80% anywhere.
- Kalshi's own opening price is no better (Brier 0.2470 vs base rate 0.2478): the market itself has
  almost no information about the 15-minute outcome at the open.

## Why 80-90% is structurally hard
- The threshold is set at (about) the price at the open, so each market starts near 50/50 by design.
- A 15-minute BTC return is dominated by noise; the measurable predictable part (mean reversion in
  stretched 5-minute indicators, distance to threshold) is small: Brier gains of ~0.005 over the base
  rate, consistent across every model family tried (linear, ridge, gradient boosting, ensembles).
- If a public-data signal gave 80% at the open, Kalshi prices would move to absorb it; they do not.
- Honest consequence: 80% at broad coverage is not achievable with the inputs we have. The realistic
  frontier is higher accuracy on FEWER openings (selective calls) - and even there the evidence so far
  tops out around 70% with wide uncertainty.

## What can still genuinely improve accuracy (ranked by expected value)
1. Consistent inputs (L13/L16) - v5 retrain on full-window inputs (pipeline ready, data from Nov 24).
   Removes the current UP bias; expected effect: back to ~65% on calls, not 80%.
2. Corrected trade flow (R10) - recorded in shadow from 2026-11-22; a model trained on it needs ~4+ days.
   Coinbase flow is currently 2/3 missing trades; corrected flow may carry real signal.
3. Selective calling with honest calibration - prespecified confidence cut (v5: top 33%) and a WAIT
   otherwise; report accuracy AND coverage. This is the only lever that moves accuracy materially.
4. Order-book and short-horizon research (market pulse) - prospective scoring now runs live; only signals
   that pass >= 500 scored forecasts with a lower bound above 50% graduate to model candidates.
5. More data - every week adds ~670 openings; uncertainty on a 33%-coverage accuracy shrinks from
   +-9 points (100 calls) to +-5 points (330 calls).

## What will NOT be done
- Raising the threshold or hiding losses to show a higher number; using reserved forward-test outcomes
  for tuning; deploying a model without a pre-registered pass.

## Regime scan (non-reserved, live calls, 2026-11-16 21:45 .. 11-20 06:00; 12 slices, exploratory)

| Slice | Accuracy on calls [95% CI] | Coverage |
|---|---|---|
| All | 69/106 = 65% [56, 73] | 106/312 |
| Opening 03-13 UTC (Asia/Europe) | 25/32 = 78% [61, 89] | 32/125 |
| Opening 21-03 UTC (US evening) | 21/33 = 64% [47, 78] | 33/91 |
| Opening 13-21 UTC (US day) | 23/41 = 56% [41, 70] | 41/96 |
| Volatility tercile low / mid / high | 58% / 72% / 66% | - |
| Distance to threshold low / mid / high | 69% / 64% / 60% | - |

With 12 slices one large deviation is expected by chance; none is significant after correction.

### Pre-registered hypothesis H-session (frozen 2026-11-22 05:00 UTC)
Live opening calls made at 03:00-12:59 UTC are more accurate than calls at other hours.
Data: live immutable calls with opens_at >= 2026-11-22 06:00 UTC. Decision at >= 120 calls in the
03-13 window: supported only if (a) its accuracy's Wilson 95% lower bound exceeds 0.60 and (b) a
two-proportion test against the other hours gives p < 0.05. If supported, a session-aware WAIT rule
becomes a v5/v6 candidate (never a live change without its own pre-registered pass and approval).

## Recalibration and market stacking of the live model (non-reserved, walk-forward, 192 openings)

| Variant | Brier | vs live 95% CI | Calls | Accuracy |
|---|---|---|---|---|
| live | 0.2453 | - | 68/192 | 61.8% |
| Platt recalibration (refit on past only) | 0.2442 | [-0.0083, +0.0058] | 115/192 | 56.5% |
| logistic stack live + Kalshi mid | 0.2446 | [-0.0085, +0.0072] | 122/192 | 57.4% |
| 50/50 live + Kalshi mid | 0.2444 | [-0.0058, +0.0041] | 62/192 | 62.9% |

No credible gain; recalibration widens coverage and lowers accuracy. Not added to the bot.
