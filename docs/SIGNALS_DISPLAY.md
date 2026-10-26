# KXBTC15M browser monitor — signals only

The browser dashboard has no account connection, credentials, order controls,
trading SDK, or execution path. It is started manually. Public mode makes only
fixed HTTPS GET requests for market data. It never reads `.env` or trading keys.
The legacy upload-only Streamlit display remains available below.

## Launch

From `/workspace/kalshi-ai-trading-bot`, first use the bundled synthetic replay:

```bash
.venv/bin/python -m src.signals_display.server --mode replay --port 8765 --journal /tmp/kxbtc15m-replay.db
```

After public data access is allowed, use this command for **public market-data
monitoring only**, without trading credentials:

```bash
.venv/bin/python -m src.signals_display.server --mode public --port 8765 --journal signals_monitor.db --target 0.05 --stop 0.10 --horizon 60
```

The local dashboard address is `http://127.0.0.1:8765/` on the machine running
that command. In a cloud task this address is inside the cloud machine; use a
supported port forward for port 8765. This onboarding UI supplies no shareable
browser preview URL. No public deployment or externally accessible URL has
been created. Binding defaults to loopback. The command does not start a bot.

The public adapter requires egress to `api.elections.kalshi.com` and
`api.coinbase.com`. Initial checks failed with proxy **403 Forbidden**. After the domain additions,
read-only checks succeeded: both public endpoints returned HTTP 200, and the
adapter retrieved a current KXBTC15M contract, fixed-point orderbook depth,
series fees and CF Benchmarks settlement-source metadata, and Coinbase BTC-USD
spot. This verifies the current instance only, not permanent upstream access.
No authentication fallback will be attempted; 401/403 or unsupported data leaves
signals unavailable. Rate limits, regional restrictions, missing fields, and
upstream outages also result in unavailable data rather than fabricated values.

## What the panels mean

**OVERALL DIRECTION** shows UP at 60% or more, DOWN at 40% or less, and UNCLEAR
otherwise. Probabilities require matched conditional scenario evidence. Current
quotes alone are not settlement forecasts. All model estimates are UNVALIDATED.

**SCALP OPPORTUNITY** selects a unique UP or DOWN target-first probability of at
least 60%, otherwise WAIT. Both side probabilities remain visible separately.
The hypothetical plans show entry ask, fee-adjusted target/stop exit bids, net
target/stop in dollars per contract, and maximum holding seconds. A displayed
plan during WAIT is only a counterfactual probe specification, not a suggestion
to enter. Unknown fees, missing market mapping, or stale data suppress plans.

The screen includes the active contract, Bitcoin strike, YES/NO bid and ask,
remaining time, reference source and timestamp, and quote receipt timestamp.
Discovery repeats every two seconds and follows the sole current 15-minute
contract using its explicit closing/opening times. Ambiguous discovery fails
closed. Snapshot age above five seconds clears signals. Browser disconnection
also clears them. A new quote timestamp means received/polled, not necessarily
that the venue changed its quote at that instant.

Coinbase BTC-USD spot is a **proxy**, not the official Kalshi settlement index.
The official settlement source is shown separately from series metadata.
The raw contract must verify YES maps to UP; no ticker/title guessing occurs.
An official index feed and a calibrated conditional price-path model are not
included. Consequently public monitoring currently shows **UNCLEAR / WAIT** and
probabilities **unavailable** while still displaying accessible market quotes
and the proxy reference. The replay demonstrates supported scenario estimates;
replay results are synthetic and cannot validate live prediction performance.

## Journal and evaluation

The SQLite journal persists across restarts. It stores timestamped predictions,
probabilities (including unavailable), input context, fees, signal explanations,
and optional official settled outcomes. Live and replay records are separated
by mode. Replay settlement labels are explicitly supplied test data. Public
settled results are fetched through the public market endpoint for up to three
recent pending tickers per minute. Older pending contracts can remain unscored;
a missing settlement is never inferred from the proxy price.

At most one **counterfactual one-contract probe** per side, contract, minute,
and target/stop/holding configuration is started when fresh two-sided quotes,
size, and fees support it. This includes WAIT periods, enabling future model
assessment without assuming a signal was filled. Entry is a hypothetical ask
with entry taker fee; exit is an observed bid with exit taker fee. Both directions
are tracked independently. `real_fill` is constrained to zero in the database.
These records never enter the repository's trading tables or performance stats.

Targets and stops are checked from the first observation including entry.
Immediate spread/fee stop-outs are valid simulated losses. A quote strictly
before the deadline can hit the target; at the deadline a surviving probe
becomes a timeout. Missing quotes, insufficient exit size, gaps over five
seconds, changed fee schedules, clock reset, or missed deadline are UNOBSERVED,
with no fabricated P&L. Intrapoll hits and actual fills remain unknowable;
even target/stop results are explicitly simulations rather than execution facts.

The dashboard lists the most recent 200 records and exports that view as JSON;
complete history stays in SQLite. Brier scores use only available probabilities
with supplied/official outcomes and are labeled as correlated research samples.
No hit-rate, calibration, or profitability claim follows from a replay.

## Validation

```bash
.venv/bin/python -m pytest -q tests/test_signals_display.py tests/test_signals_monitor.py
# Optional real Chromium tests, still using only local mocked data:
.venv/bin/python -m pip install playwright
.venv/bin/python -m pytest -q tests/test_signals_browser.py
```

Chromium tests use `/usr/bin/chromium`. They verify both panels, live browser
refresh, contract rollover, stale clearing, mobile layout, journal outcomes,
no external browser requests, and no order controls. Unit tests verify public
GET request boundaries with mock responses, fee/spread math, immediate stops,
first passage, missing data, timeout/gap exclusion, persistence, and HTTP write
rejection. Tests do not contact live market APIs or trading accounts.

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

## Accuracy goal

90% accuracy is a goal to measure, never a claim. Contract-level accuracy and
Brier score use the first supported UP/DOWN prediction per settled contract,
separately for public data and synthetic replay. UNCLEAR is excluded, repeated
polls cannot inflate the scored contract count, and no outcome means no score.
This is a research metric, not validation of a forecasting model. The screen
also shows the exact published contract rule because KXBTC15M settlement may
compare 60-second CF Benchmarks averages rather than instantaneous spot values.

## GitHub Codespaces: laptop access

This cloud onboarding environment has no supported external browser-preview or
port-forwarding capability. Binding to `0.0.0.0` does not itself create a public
URL. Use a GitHub Codespace for a supported authenticated browser connection:

1. Open this repository on branch `codex/kxbtc15m-signals-display` in GitHub.
2. Select **Code → Codespaces → Create codespace on this branch**. The included
   `.devcontainer/devcontainer.json` provides Python 3.12, installs only the
   read-only monitor's HTTP dependency, and declares port 8765 for forwarding.
   It never starts the monitor or any trading process automatically.
3. In the Codespace repository-root terminal, run:

   ```bash
   .venv/bin/python -m src.signals_display.server --mode public --host 0.0.0.0 --port 8765 --journal signals_monitor.db --target 0.05 --stop 0.10 --horizon 60
   ```

   If using an existing Codespace that has not loaded the devcontainer config,
   first run `python3 -m venv .venv` and
   `.venv/bin/python -m pip install httpx==0.27.0` from the repository root.
4. Open the **Ports** tab. If port 8765 is absent, choose **Forward a Port** and
   enter `8765`. Leave visibility **Private**, then select **Open in Browser**.
   Use the HTTPS **Forwarded Address** displayed there from your laptop; sign
   in with the GitHub account owning the Codespace. It normally has the form
   `https://<codespace-name>-8765.app.github.dev/`. A real clickable forwarded URL
   exists only after GitHub creates that Codespace and forwards the port.
5. Confirm **PUBLIC MARKET DATA**, a current contract, advancing last-update
   timestamps, and changing quotes/countdown. Missing data remains WAIT and
   unavailable; unsupported model probabilities never become invented values.

No trading credentials are needed. Keep the Codespace terminal and Codespace
running. Stop the monitor with Ctrl-C. This application serves only read-only
market-data/research pages; it contains no order or position-management route.
