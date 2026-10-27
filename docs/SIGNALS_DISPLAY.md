# KXBTC15M signals dashboard

Experimental estimates; **edge unverified**. No profitable edge or 90% accuracy
has been demonstrated. This application never submits, amends, cancels, transfers,
or manages broker orders. You execute every trade yourself.

The default is public-data monitoring with authentication disconnected. Inspection
of this cloud environment found no configured monitoring credential binding; the
previous running dashboard read public data only, not positions, orders or fills.
Your separate Codespace cannot be inspected from this environment.

## Update your existing Codespace, preserving the journal

In the terminal running the old dashboard, press Ctrl+C. Then run from the repository
root (do not delete or replace `signals_monitor.db`):

```bash
git fetch origin
git switch codex/kxbtc15m-signals-display
git pull --ff-only origin codex/kxbtc15m-signals-display
python3 -m venv .venv
.venv/bin/python3 -m pip install httpx==0.27.0
.venv/bin/python3 -m src.signals_display.server --mode public --host 0.0.0.0 --port 8765 --journal signals_monitor.db --target 0.05 --stop 0.10 --horizon 60 --manual-delay 3 --slippage 0.01
```

If Git reports local modifications, preserve them and resolve that before pulling;
do not reset or clean the checkout. SQLite schema additions preserve existing
predictions/simulations. Run from the same directory and use the same journal path.
The account journal defaults to the separate `signals_account_monitor.db`; preserve
that file too once account monitoring is configured. Nothing autostarts.

In Codespaces **Ports**, forward **8765**, set **Port Visibility: Private**, and click
**Open in Browser**. Keep your existing private forwarded URL. To print its URL:

```bash
python3 -c 'import os; print("https://" + os.environ["CODESPACE_NAME"] + "-8765." + os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev"))'
```

This onboarding environment has no supported external browser preview; its loopback
address is not a laptop-accessible URL. Codespaces supplies the private forwarding
and access control. The application itself has no login service; do not expose it
through a public port or proxy. Private forwarding is particularly necessary when
account data is enabled. `--private-dashboard` acknowledges this requirement; it
cannot verify your Codespaces port setting.

## Simple display and local manual tracking

The two main cards keep settlement direction separate from trade qualification.
UP does not mean BUY. Unsupported probabilities read `unavailable`; WAIT has a
plain reason. LIVE / STALE / WARMING UP reflects data/history readiness, not model
validation. Qualifying scalp plans show entry cap, target, stop, maximum hold,
experimental success probability, timestamp and expiry. Signals expire after five
seconds without a refreshed quote and cannot outlive their contract/holding window.

Details contains source coverage, quote depth, candles, independent model inputs,
Kalshi benchmark, assumptions, uncertainty/sensitivity, fees, net edge, diagnostics,
and timestamped prediction/counterfactual evaluation. No candle or market price
alone qualifies a trade.

“I entered” records a trade you report actually making; entry/exit controls write
only the local journal. They cannot communicate an order to Kalshi. Recorded open
trades continue receiving their own contract's public quotes through rollover.
Target/stop/time/invalidation guidance never closes the record automatically. Record
your actual exit, including partial exits and fees. On restart, restored records
require fresh quotes before price-based guidance. These records are user reports,
separate from broker-confirmed account holdings and counterfactual simulations.
Optional browser/sound alerts require your opt-in; browsers may suppress audio or
background notifications. Persistent event IDs suppress repeated alerts.

## Optional read-only account monitoring (left disconnected)

Account mode has only a server-side GET allowlist for `/trade-api/v2/portfolio/`
`balance`, `positions`, `orders`, `fills`, plus `/trade-api/v2/api_keys` to inspect
key scopes. Fixed HTTPS origin, redirects disabled, bounded pagination, redacted
errors; it imports no execution client. POST/PATCH/PUT/DELETE, transfers, individual
order mutation paths, and arbitrary endpoints are blocked before signing or network
access. Dashboard POST routes only save local journal/preferences, with same-origin
JSON checks; they are never broker write proxies.

Use Kalshi's supported API key settings to request read-only permissions **if the
platform exposes them**. This implementation checks the authenticated key metadata:
only explicit `read` scope without write scopes is reported read-only. Unknown
metadata or permission errors mean **PERMISSIONS UNVERIFIED**; write permission
means **TRADING CAPABLE KEY**. It never assumes API keys are read-only. A key may
permit trading outside this client; the strict client allowlist remains enforced.
Live key permissions/API compatibility have not been verified here because no
account authentication was enabled. Unsupported response schemas fail closed.

Never paste keys into chat, dashboard forms, terminal commands, or Git. In your
Codespace's secure Secrets settings configure:

- `KALSHI_MONITOR_KEY_ID`
- `KALSHI_MONITOR_PRIVATE_KEY_PEM` (complete PEM as a server-side secret)

Restart the Codespace if necessary for secret injection. Alternatively configure
`KALSHI_MONITOR_PRIVATE_KEY_PATH` pointing to a private RSA PEM outside Git; only
owner permissions (`chmod 600`) are accepted. `.private/`, PEM files and `.env*`
are ignored. Use one account store per account; a hash binding refuses accidental
mixing of accounts. Never put keys in browser code. Keep the forwarded port Private.

Install the signing dependency without starting any trading application:

```bash
.venv/bin/python3 -m pip install cryptography==42.0.0
```

After securely configuring secrets, stop public mode with Ctrl+C and start:

```bash
.venv/bin/python3 -m src.signals_display.server --mode public --host 0.0.0.0 --port 8765 --journal signals_monitor.db --account-readonly --private-dashboard --account-store signals_account_monitor.db --target 0.05 --stop 0.10 --horizon 60 --manual-delay 3 --slippage 0.01
```

No account requests occur without `--account-readonly`. Removing that flag restores
public-only mode and does not expose stored account records to the browser.

MY LIVE TRADES reconciles confirmed fills with authoritative net positions, one
card per current contract/side, including adds, partial fills and reductions.
Unfilled pending orders are separate and never counted as invested. Startup and
reconnect read full histories; routine sync overlaps recent fills and deduplicates
fill IDs. Incomplete pagination/schema failures withhold reconciliation. Missing
historical fills or fees withhold net cost/P&L and cost-dependent guidance. A known
entry price can remain visible when fees are unknown. Confirmed-fill FIFO is an
explicit accounting convention, not tax reporting. Opposite-side netting uses
complementary payout prices. Settlement-only closures without closing fills do not
produce invented realized P&L; complete settlement cashflows are not collected.

Select which holdings receive guidance. All held KXBTC15M contracts continue quote
monitoring through main-contract rollover. Cards show executable full-size bid,
estimated liquidation P&L after costs, reachable scalp-target net profit, conditional
winning-settlement profit (not expected/guaranteed), cost at risk, countdown, and
HOLD / CONSIDER EXIT / WAIT. Insufficient depth or unknown prices/fees mean
unavailable, never zero. Stale account snapshots (>20s) or quotes (>5s) withhold
fresh guidance and flag last-known values. Guidance thresholds use the launch
`--target`, `--stop`, and `--horizon`; existing holdings are not inferred to be
purchases of dashboard setups. Current guidance supports only these simple
thresholds and a strong, fresh opposing same-contract forecast.

CONSIDER ADDING is withheld unless all local exposure/cash limits are configured,
fresh executable size is available, an independent model has adequate held-out
samples and positive conservative incremental expected value after costs. Pending
orders reserve potential exposure but are not invested capital. Losing money is
never a reason to add. No present evidence qualifies a proven profitable edge.

## Data coverage, model assumptions and limitations

Public polling is bounded to documented APIs: Kalshi KXBTC15M markets/series/books,
Coinbase BTC/USD spot and closed one-minute candles, Kraken BTC/USD ticker/OHLC/
10-level book, Kraken inverse BTC/USD perpetual mark/index/funding/open interest,
and Federal Reserve public press-release RSS. Extra collectors run independently,
with stale/error status and reconnects. No subscriptions or paid services were added.
Feed availability depends on geography and host egress. Receipt times are displayed;
where exchange event times are missing, true exchange-to-receipt latency is unknown.
Spot depth is not trade flow. Funding/OI are raw provider conventions, context only.
RSS headlines are bounded untrusted data, never instructions or universal BTC news.
Binance USDT quotes are excluded without a contemporaneous USDT/USD conversion.

The live CF Benchmarks BRTI stream is **unavailable**. Kalshi supplies official
contract strike, comparator/mapping, rules and averaging metadata. Coinbase remains
a proxy. The independent statistical fallback anchors proxy/index basis to observed
opening-window prices or the matching opening-minute OHLC4 approximation; it never
silently assumes zero basis. It assumes that basis persists, an unvalidated
assumption. No anchor, invalid rules, missing final-average history, stale inputs,
or inadequate volatility history makes the forecast unavailable with exact reasons.
Its settlement distribution integrates the contract's final 60-second averaging
window; observed portions require contiguous history. Gaussian conditional intervals
and opening-range sensitivity are **not calibrated confidence bounds**. The terminal
spot forecast and settlement average are distinct. Kalshi odds are excluded from
settlement feature inputs and shown separately as a benchmark.

Scalp probabilities use contract-price diffusion fitted to recent timestamped
quotes, separate UP/DOWN target-before-stop/timeout simulation, modeled manual entry
delay, entry cap, spread, both fees and per-leg slippage. It is an experimental
conditional model, not an observed win rate. Frozen spreads, assumed future depth,
1-second simulation steps and public polling can miss barriers or liquidity changes.
Simulated fills are never real fills. Net settlement edge is independent model
probability minus executable ask and costs; scalp edge is net expected profit over
target/stop/timeout/no-entry paths. Positive model edge alone is not demonstrated
profitability. WAIT remains available for ambiguous, stale or negative-cost cases.

Scanner diagnostics show past closed 1m/3m/5m returns without assigning confluence
confidence. Qualifying scalps also require aligned return signs and an experimental
5m sweep/reclaim of the preceding 20m high/low, using closed bars only. This fixed
structural filter has no demonstrated predictive value; KDJ/exhaustion confidence
is unavailable. Licensed
liquidation clusters, aggressive futures trade flow, official running BRTI TWAP and
“mathematically protected” average arbitrage are unavailable; none is fabricated.
A >10 percentage-point model/market difference is experimental divergence, not a
high-probability mispricing claim. Spreads above three cents mute scalp calls.
Fed releases mute calls for 15 minutes after publication. **No complete verified
forward CPI/FOMC/SEC calendar is connected**, so pre-event 15-minute muting cannot be
promised. Macro status says incomplete rather than CLEAN. No universal internet scan.

## Offline validation and learning

Run only the scoped mocked/replayed signals tests (never the trading/live suites):

```bash
.venv/bin/python3 -m pytest -q tests/test_signals_display.py tests/test_signals_monitor.py tests/test_signals_live_model.py tests/test_signals_edges_and_evaluation.py tests/test_signals_browser.py tests/test_signals_account_readonly.py tests/test_signals_upgrade.py
```

Development/browser tests need the repository's dev/dashboard dependencies,
`playwright==1.64.1`, and Chromium at `/usr/bin/chromium`; production browser serving
needs only httpx, plus cryptography for optional authentication.

Predictions precede outcomes in SQLite. Official labels and simulated/replayed
results are kept distinct. Evaluation reports settlement accuracy, uncertainty,
Brier/log loss/calibration by horizon against zero-drift and market baselines,
called/WAIT coverage, scalp target/stop/timeout counts, simulated net P&L, approximate
intervals and drawdown. Repeated forecasts are correlated, not independent samples.
Account/manual results are never pooled into simulation performance.

```bash
.venv/bin/python3 -m src.signals_display.learning --journal signals_monitor.db --output signals_model.json
```

The offline learner requires at least 170 distinct official labeled contracts and
100/30/30 contracts after chronological train/validation/test purging. Label-availability
and overlapping settlement windows are purged; normalization/imputation uses train
only, calibration/model selection uses validation only. Logistic/ridge candidates,
input-group validation ablations, simple prior, zero-drift and market-implied
baselines are reported. Final holdout is a deployment gate, not a retuning set.
A failed/insufficient run writes a report without invented trained weights. Do not
repeatedly tune against the same final holdout. Optional JSON artifacts are validated
and future-trained artifacts rejected at prediction time. This process has not yet
established a trained, calibrated live advantage; the statistical fallback remains
experimental. Added feed features have **not** demonstrated incremental value without
sufficient labeled observations. No present claim of 90% accuracy or profitable edge.
