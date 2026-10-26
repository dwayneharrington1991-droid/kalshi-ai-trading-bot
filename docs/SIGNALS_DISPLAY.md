# KXBTC15M live signals monitor — experimental, read-only

The dashboard reads public market data only. It imports no trading clients,
loads no credentials, accesses no trading account, and has no order or position
management routes. HTTP writes are rejected. A profitable live edge has **not**
been demonstrated: **edge unverified**. 90% accuracy is a goal, never a claim.

## What was wrong before

Public mode previously returned `evidence={}` on every poll. Its only estimator
consumed uploaded scenarios rather than live history, and the uploaded-schema
settlement definition did not match the live contract-rule representation.
Thus unavailable probabilities were principally missing implementation/wiring,
not a disabled configuration or just insufficient history. That path is now
replaced by enabled-by-default live models. `--disable-model` intentionally
selects quotes-only mode and explicitly reports `MODEL_DISABLED` in both panels.

## Live inputs and exact panel reasons

- Public Kalshi contract/series/orderbook GETs provide the actual strike,
  opening/closing times, published settlement rule, fee schedule, complementary
  YES/NO bid/ask prices, and one-contract executable depth. No ticker-based
  settlement-price guessing occurs. Receipt timestamps are **not venue event
  timestamps**, and polled changes can miss intervening prices.
- Coinbase BTC-USD spot is a **proxy**, not CF Benchmarks BRTI. Public one-minute
  OHLCV candles have provider start times. Only closed bars at/before prediction
  time enter calculations; forming/future bars are ignored. One-/five-minute
  momentum, volume, and volatility are context, not proof of edge.
- Quotes/reference must be at most 5 seconds old. A history gap above 5 seconds
  resets the affected rolling buffer. The scalp model needs 61 distinct
  observations spanning at least 120 seconds and nonzero measured variation.
  Its history restarts for each contract; proxy reference history can continue.
- Settlement volatility uses valid contiguous observed proxy ticks when warm,
  otherwise at least 31 contiguous closed one-minute candles (30 return
  intervals). Candle receipt and latest close must be within 150 seconds.
  Cached candles refresh at most once per minute. Unknown/malformed/stale
  candles never fill a missing-history requirement.
- Each panel reports its own `INSUFFICIENT_HISTORY` counts/span, `STALE_DATA`,
  missing reference/depth, unknown fee, unsupported rule, or time-limit reason.
  Overall and scalp calculations are independent. Neither is fabricated merely
  to replace WAIT. Neutral probability, unavailable probability, negative EV,
  and missing evidence have different explanations.

## Overall lean and settlement edge

The zero-drift **dollar Brownian proxy model** estimates the probability that
its final 60-second arithmetic average is at least the actual contract strike.
It requires explicit recognition of the CF Benchmarks BRTI / two sixty-second
averages / "at least" rule. An unknown rule yields unavailable, not a guessed
settlement formula. The screen also shows the complete published rule.

With time remaining `r >= 60`, the model average has mean current proxy price
and variance `sigma² * (r - 40)`. Inside the final minute, the observed portion
is integrated by trapezoidal interpolation and the future contribution has
variance `sigma² * r³ / (3 * 60²)`. Missing observed averaging-window coverage
withholds the estimate. A normal CDF gives UP, and DOWN is its complement.

Assumptions: zero drift, locally constant volatility, zero Coinbase/CF basis,
time-aligned proxy samples, and a proxy average compatible with the official
index average. Those assumptions are **unvalidated**; Coinbase momentum is not
silently substituted for official settlement data. Volatility estimator,
source timestamps, inputs, and assumptions are visible in the panel details.

Direction is UP at probability >=60%, DOWN at <=40%, otherwise UNCLEAR. This
lean is separate from trade qualification. The modeled settlement edge for
one contract is:

`independent model probability - (executable ask + slippage allowance) - entry taker fee`

It assumes holding to $1/$0 redemption with no redemption fee, not a paid exit
order. Spread is included by entering at ask rather than midpoint. The entry
cap is conditional on obtaining the displayed price after the manual delay.
Unknown fees/depth suppress cost calculations. Even a positive modeled edge
remains **WAIT / edge unverified** because proxy basis and calibration have
not been measured. A theoretical edge is not demonstrated profit.

## Separate scalp estimates and expected profit

A **zero-drift logit-midpoint diffusion** fits recent changes in the active
contract's UP quote, independently of the settlement proxy model. It simulates
2,000 coupled UP/DOWN paths at one-second resolution, with snapshot-seeded
repeatability. It assumes current spreads, fee schedule, and future
one-contract depth persist. Missing current depth or crossed quotes reject it.
No trend or favorable candle alone becomes a trade signal.

Default manual delay is 3 seconds and adverse slippage is $0.01 per leg. At
the delay, a path assumes an entry only if its simulated ask plus slippage is
no more than the displayed entry cap. Otherwise that path is **no entry**.
For an entered path, liquidation is at the executable-side modeled bid minus
slippage, with separately rounded entry and exit taker fees. It resolves by
net target, net stop, or timeout. Hitting a gross price target is insufficient.
Both directions can win or lose; their target-first probabilities are not
complements. Probability is conditional on hypothetical entry; entry
probability and no-entry counts are shown separately.

`net EV per opportunity = mean(net P&L across target + stop + timeout + no-entry paths)`

No-entry paths contribute zero; timeout losses are not dropped. Outcome counts,
mean net P&L by outcome, EV, entry cap, target/stop exit bids, maximum holding
period, depth, costs, and manual-delay assumptions are visible. Monte Carlo
sampling intervals cover simulation error **only**, not parameter/model error.

A model lean is displayed separately from SCALP UP / SCALP DOWN / WAIT.
Experimental scalp qualification requires a unique side with target probability
>=60%, positive modeled net EV, positive lower numerical EV bound, >=200 entered
paths, fresh compatible data and sufficient remaining lifetime. These are fixed
quality gates, not a profitability guarantee. All candidates remain explicitly
experimental and **edge unverified**. Fee/spread costs can make WAIT the correct
result despite a high directional probability.

## Journal preservation and evaluation

Use the existing `signals_monitor.db`. Schema updates add columns/tables;
existing predictions and outcomes are retained. New delayed probes are labeled
by model version; legacy immediate-entry simulations remain separate. Closed
provider candles are archived once per source/start time. Prediction records
contain model version, available inputs, assumptions and timestamps **before**
settlement labels are applied. No prediction is counted as a real fill/trade.

Counterfactual probes wait for the configured manual delay, then observe the
actual polled ask/depth and entry cap. A moved price produces no entry. Entered
probes use observed bid minus slippage and both fees. Missing observations,
changed fees, insufficient depth and missed deadlines remain UNOBSERVED with
no invented P&L. The simulation cannot recover intrapoll barriers or guarantee
a manual execution. Keep data gaps/censoring visible when judging results.

Settlement accuracy/Brier scores are separate from scalp target-before-stop
rates and simulated net P&L. Summaries separate model versions and public versus
synthetic/replay data. Approximate sampling intervals, sample sizes, pending,
no-entry and unobserved counts are reported. Call/WAIT coverage uses the first
fresh prediction per contract/minute, rather than inflating opportunity count
with repeated polls. Overlap, dependence and informative missing data can
invalidate naive confidence intervals. No sample statistics imply 90% accuracy.

Offline tests use synthetic observations. The walk-forward evaluator uses fixed
parameters, chronological warmup and holdout, and fits volatility only from
observations available by each forecast. It rejects reversed time, future-dated
quotes/references and outcomes before contract close. Closed candles after a
prediction time never influence that prediction. Synthetic passing tests are
software validation, not evidence of a profitable live edge.

```bash
.venv/bin/python -m pytest -q tests/test_signals_display.py tests/test_signals_monitor.py tests/test_signals_live_model.py tests/test_signals_edges_and_evaluation.py tests/test_signals_browser.py
.venv/bin/python -m src.signals_display.evaluate_replay --input /path/to/timestamped-frames.json --output /tmp/kxbtc15m-holdout.json --training-frames 61
```

Replay inputs use the snapshot/reference schema below plus `now`, optional
`btc_candles` with `source`, `received_at` and provider OHLCV `candles`, and optional
`settlements` mapping ticker to `yes`/`no` after close. Actual chronological
out-of-sample profitable-edge evidence remains insufficient.

## Update and restart an existing GitHub Codespace

Stop only this dashboard with **Ctrl-C** in its terminal. From repository root:

```bash
git switch codex/kxbtc15m-signals-display
git pull --ff-only origin codex/kxbtc15m-signals-display
.venv/bin/python -m src.signals_display.server --mode public --host 0.0.0.0 --port 8765 --journal signals_monitor.db --target 0.05 --stop 0.10 --horizon 60 --manual-delay 3 --slippage 0.01
```

No new runtime dependencies or trading credentials are required. Keep the same
journal path; do not delete it or reset the branch. A Codespace with local
changes must preserve those changes before updating; the commands do not force
an overwrite. Recently contiguous journal inputs can restore model warmup;
a long restart gap requires fresh observations. After rollover, scalp history
warms up again even if overall direction remains supported.

Use **Ports → 8765 → Open in Browser**, with visibility Private. Keep the same
GitHub HTTPS forwarded address. This cloud onboarding environment itself has
no supported external preview service; binding to all interfaces does not make
a new public URL. The included devcontainer declares port 8765 and installs
only the monitor's HTTP dependency; it starts no server or trading process.

Public GETs use `api.elections.kalshi.com` and `api.coinbase.com`. Authentication
failures, regional limits, rate limits or network blocks yield explicit
unavailable/WAIT diagnostics; no trading-account authentication is attempted.

---

# Legacy upload-only signals display

This feature is offline and signals-only. It imports no clients, execution jobs,
strategy adapters, settings, or credentials. It never discovers markets over the
network, generates orders, or reads account data. It is not launched at setup.

From the repository root, manually launch only when desired:

```bash
.venv/bin/python -m streamlit run scripts/signals_display.py --server.headless true --browser.gatherUsageStats false
```

Upload a local JSON snapshot. Until sufficient fresh evidence is supplied, both
settlement and scalp estimates show `unavailable` and the status is `WAIT`.
The display rechecks freshness every second. All probabilities remain explicitly
**UNVALIDATED**. These are conditional scenario frequencies, not a validated
prediction service. No live model or market feed is included.

## Evidence contract

Top-level JSON has `markets` (list) and `evidence` (object). The active market
must be the sole open KXBTC15M contract whose opening/closing interval includes
current UTC time. Ambiguous candidates fail closed. The interval cannot exceed
15 minutes. Required market fields:

| Field | Meaning |
| --- | --- |
| `series`, `ticker`, `status` | `KXBTC15M`, explicit `KXBTC15M-…` identity, `open` |
| `opens_at`, `closes_at`, `observed_at` | ISO timestamps with timezones; snapshot at most 5 seconds old |
| `settlement_reference` | Explicit official settlement source, not an unrelated exchange price |
| `strike` | Positive reference target |
| `up_definition` | Exactly `reference_at_settlement > strike`; equality belongs to DOWN |
| `quotes` | `up` and `down`, each with dollar `bid`, `ask`, `bid_size`, `ask_size` (at least one contract) |
| `fee_type`, `fee_multiplier` | Authoritative series fee metadata; supported quadratic taker schedule |

`evidence` must repeat `ticker`, `snapshot_at` (exact market `observed_at`),
`settlement_reference`, and `strike`. It needs a nonempty `method` describing
provenance and at least 30 equally weighted conditional `paths`. The supplier
must establish conditioning on the current snapshot, independence, official
settlement semantics, fee applicability, and suitability of future bid paths.
This tool cannot establish those claims from an upload. Thirty paths is a
minimum input guard, not proof of calibration or statistical adequacy.

Each path has a boolean `settlement_up` derived from the official reference at
settlement, and `quotes`, a list like:

```json
[
  {"seconds": 0, "up_bid": 0.48, "down_bid": 0.48},
  {"seconds": 30, "up_bid": 0.70, "down_bid": 0.25},
  {"seconds": 60, "up_bid": 0.65, "down_bid": 0.30}
]
```

Times must strictly increase from zero, include the exact selected time limit,
and cover that limit. Initial bids must match the snapshot. Unknown fees or
missing quote paths make scalp estimates unavailable while independently
supported settlement frequencies can still display. Bad or stale core evidence
makes all estimates unavailable. Changing the time limit can require new paths.

## Two distinct questions

Settlement UP is the fraction of scenario outcomes marked UP; DOWN is its
complement. This is not inferred from a scalp signal or the current ask.

Each scalp considers buying one contract at its current ask and hypothetical
liquidation at that side's future bid. Net P&L subtracts separately rounded
entry and exit taker fees: `ceil(100 * .07 * multiplier * p * (1-p)) / 100`
per leg. Thus spread is paid through ask-to-bid pricing, not double-counted.
A success reaches the net profit target strictly before the time limit and
before the first net stop-loss observation. The initial spread and fees can
immediately breach a tight stop. A timeout is a non-success, not a discarded
sample. UP and DOWN scalp estimates are calculated separately and need not sum
to one. The horizon cannot exceed the remaining contract lifetime.

Discrete paths can miss barriers between observations. Future executable
liquidity, queueing, fills, changing fees, latency, source validity, and model
calibration are unvalidated. No estimate implies an execution recommendation.

## Mock-only validation

```bash
.venv/bin/python -m pytest -q tests/test_signals_display.py
```

Tests use synthetic in-memory data and Streamlit's simulated AppTest runtime;
they do not start a web server, call an API, access credentials, or execute a
trading script. Do not run the bot, trading scripts, live API tests, or order
operations for this environment. Do not access or change trading credentials.
