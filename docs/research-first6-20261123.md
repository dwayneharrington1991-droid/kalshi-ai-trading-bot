# FIRST 6 MIN shadow AI — research status (2026-11-23)

**Target.** At the market open, will the BTC reference price at opens + 6:00 be above the reference price at the
open? (Coinbase one-minute closes; FLAT moves are not scored.) This is separate from the Kalshi final settlement.

**Data.** 640 KXBTC15M markets (Nov 15–22). The split is chronological: fit 384, check 128, and a test block of
128 scored once. The up-rate on fit was 45.3%.

## Inputs available at the open, single-input AUC (0.5 = no skill)

| Input | Fit | Check | Verdict |
|---|---|---|---|
| AI #1 opening P(UP) for the final settlement | 0.563 | 0.530 | weak and consistent; scored once on test below |
| Kalshi UP mid at the open (`opening_quote_snapshots`) | 0.504 | 0.573 | inconsistent |
| Kalshi ask-size imbalance at the open | 0.437 | 0.582 | flips sign (noise) |
| Coinbase price vs Kalshi strike (σ-scaled) | 0.496 | 0.575 | inconsistent |
| 1m / 5m / 15m volatility-scaled momentum | 0.49–0.52 | 0.47–0.60 | noise |

## Candidates scored on the test block
- **v0 (installed), momentum logistic:** 69/128 = 53.9%, Brier 0.2485, equal to the base rate.
- **AI #1-based logistic** (fit on fit + check, scored once): Brier 0.2477 against a base rate of 0.2489. At
  55% it was right on 34/63 calls (54%). Test AUC 0.512. Not a meaningful improvement.

## Conclusion
None of the data the bot collects predicts the six-minute BTC direction better than the base rate. That covers
Coinbase/Kraken price action, Kalshi quotes, book imbalance, strike distance, and AI #1's opening probability.
The six-minute move looks close to a random walk at this horizon.

FIRST 6 MIN therefore stays in shadow mode, displayed as `TESTING 🟡`. v0 keeps recording every opening
prospectively in `first6` / `first6_outcomes` on the alert service, so a future model can be judged on truly
unseen markets. It is **not** ready for activation, and activation will not be proposed without a pre-registered
pass.
