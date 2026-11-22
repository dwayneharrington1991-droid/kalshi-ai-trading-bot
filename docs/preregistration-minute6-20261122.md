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

## Exploratory follow-ups (2026-11-22, after freezing; not used to change the frozen rule)
Component comparison (same splits; calls / accuracy / coverage):

| Rule | Fit (325) | Check (140) | Holdout (175, already viewed) |
|---|---|---|---|
| A price/distance alone ≥ 0.80 | 104, 91.3%, 32% | 51, 90.2%, 36% | 51, 90.2%, 29% |
| A + B | 90, 92.2%, 28% | 47, 91.5%, 34% | 44, 90.9%, 25% |
| A + B + C (frozen) | 86, 93.0%, 26% | 46, 91.3%, 33% | 33, 93.9%, 19% |
| B momentum alone | 250, 71.6%, 77% | 122, 74.6%, 87% | 146, 65.1%, 83% |
| C engine alone | 109, 86.2%, 34% | 71, 81.7%, 51% | 50, 94.0%, 29% |
| Sign of price − threshold (always call) | 73.8% | 74.3% | 69.1% |

- No duplicate markets. All 649 outcomes are `official`.
- Component A is calibrated: readings with stated confidence 0.95–0.97 were right 93–100% of the time.
- Dropping C (whose dev timing allowed a reading up to 40 s after 6:00) leaves about 91%. So the accuracy is not driven by that lookahead.

**Hour-of-day volatility adjustment (rejected).** The volatility blend σ = w·σ₆₀ₘ + (1 − w)·σ_hour used an hour table from fit markets only. It was selected on fit for maximum coverage with Wilson low ≥ 0.85, which gave w = 0, t = 0.75. It did not hold on the clean check set: 69/86 = 80.2% (Wilson low 0.71), against the baseline 46/51 = 90.2%. The extra coverage came from genuinely uncertain markets. The rule is kept unchanged.

**Strict-timing audit with real asks (2026-11-22 23:45 UTC).** The C reading is used only if it was stamped ≤ opens + 367 s; 3 readings were dropped. Asks are the latest Kalshi quotes observed ≤ opens + 367 s. The fee is 0.07·p·(1−p), rounded up to 1¢, plus 1¢ slippage.

| Set | Accuracy | Avg ask paid | Net per contract (95%) |
|---|---|---|---|
| Fit | 80/86 = 93.0% | 0.897 | +1.2¢ (±5.3¢) |
| Check | 42/46 = 91.3% | 0.908 | −1.6¢ (±8.0¢) |
| Holdout (viewed) | 31/33 = 93.9% | 0.918 | +0.2¢ (±8.0¢) |

Accuracy is unchanged under strict timing. There is no measurable edge at real entry prices. `four_lines.ENTRY_RULE` stays None, so the dashboard and Telegram show `| WAIT`.
