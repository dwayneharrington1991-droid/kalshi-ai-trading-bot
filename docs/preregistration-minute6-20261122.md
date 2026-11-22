# Pre-registration: minute-6 quant consensus (research only), frozen 2026-11-22

**Target.** The official Kalshi settlement (UP/DOWN) of the same KXBTC15M market, predicted once at
opens + 6:00 using only information available then. It is a different prediction from the minute-0 opening
calls, and it is scored on a separate scoreboard. The two are never compared as if they used the same
information.

## Inputs at the cutoff
- Coinbase BTC-USD one-minute candles up to the candle that closes at opens + 6:00. The current partial
  minute is never used.
- Kalshi's own settlement threshold for that ticker, from `/api/state`. The Coinbase price is not the
  settlement index; the difference is shown on the dashboard.
- The production in-market engine's P(UP), only if it was published at or before the lock.

## Components
| | Rule |
|---|---|
| A, price/distance | P(UP) = Φ(ln(price/threshold) / (σ₁ₘ·√9)), with σ₁ₘ from the last 60 one-minute log returns |
| B, momentum/structure | Mean sign of: 1m momentum, 5m momentum, EMA9 − EMA21, and 5m acceleration |
| C, learned | The existing in-market engine's P(UP) (an existing trained model; no new model fitted) |

## Frozen rule
- UP or DOWN only if every eligible component agrees, with at least two components eligible.
- A must be ≥ 0.80 (or ≤ 0.20), |B| must be ≥ 0.5, and C must be ≥ 0.70 (or ≤ 0.30).
- Otherwise WAIT.
- A missing component is INELIGIBLE and is never invented.

## Development (how the rule was chosen)
- The thresholds were selected on the first 70% of markets before 2026-11-21 00:00 UTC (325 markets).
- The check set is the next 30% (140 markets).
- The chronological holdout is the 175 markets from 2026-11-21 onwards. It was used once, after freezing,
  and not for selection.

| Set | Calls / markets | Accuracy | Wilson 95% low | Avg price of the called side | Est. EV per contract after fee + 1¢ |
|---|---|---|---|---|---|
| Fit | 86 / 325 | 93.0% | 0.856 | 0.885 | +1.3¢ |
| Check | 46 / 140 | 91.3% | 0.797 | 0.904 | −0.9¢ |
| Holdout | 33 / 175 | 93.9% | 0.804 | 0.916 | +0.8¢ |

On the same sets, the simple price-vs-threshold sign baseline was right 69–74% of the time, and the Kalshi-mid
direction was right 70–72%.

**Interpretation.** At minute 6 the market is usually already decided by distance and volatility. Kalshi's prices
reflect this, because the rule's calls cost about 90¢. High accuracy here is **not** a demonstrated trading edge.
The prices above are the implied mid, not the ask, so the real EV is lower.

## Prospective test
- The test starts at deployment of the alert service (2026-11-22).
- Ledger: `~/dual-ai-alerts/sent.sqlite3`, table `minute6`. It is immutable, with append-only triggers. Outcomes
  come from official settlements (`/api/market-history`, `VERIFIED_OFFICIAL`).
- Decision at ≥ 150 settled calls. **Supported** only if the Wilson 95% lower bound of accuracy is > 0.85 **and**
  the mean estimated net per contract at the lock-time ask (fee 0.07·p·(1−p) plus 1¢ slippage) has a 95%
  bootstrap CI above 0.
- Nothing from this test is used to change the minute-0 opening model or the v1–v5 frozen tests.
