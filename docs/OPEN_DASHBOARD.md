# Open your dashboard on your laptop

The dashboard helps you decide what to watch. It cannot place a trade. The safe
launcher uses your existing server-side monitoring secrets, if configured, and
requires verified read-only permissions before connecting. Never paste a key into
chat or the webpage.

## 1. Open your existing Codespace

1. On your laptop, open your web browser and go to https://github.com/codespaces.
2. Sign in to your GitHub account if asked.
3. Find the **existing** Codespace for `kalshi-ai-trading-bot` and click its name.
   Wait for the editor to open. You do not need to create another Codespace.

## 2. Open the terminal

1. At the top of the editor, click **Terminal**, then **New Terminal**.
   If the menus are hidden, click the three-line menu first. Alternatively use
   **View → Terminal**.
2. A terminal is the text box along the bottom where commands run.
3. If another terminal is running the old dashboard, click that terminal and press
   **Ctrl+C** once. This stops the dashboard, not your Codespace or saved journal.
4. In the new terminal, paste the following block and press **Enter**. It updates
   the code and starts the private dashboard, preserving your journals. Existing
   monitoring secrets enable authenticated GET requests only after permission checks.

```bash
cd /workspaces/kalshi-ai-trading-bot &&
git fetch origin &&
git switch codex/kxbtc15m-signals-display &&
git pull --ff-only origin codex/kxbtc15m-signals-display &&
python3 scripts/start_dashboard.py --restart --private-dashboard
```

The standard GitHub Codespace checkout is `/workspaces/kalshi-ai-trading-bot`.
If the first line says “No such file or directory,” use the folder shown in the
editor's Explorer and run the block from that repository root instead. Keep the
existing `signals_monitor.db` there. Do not create another journal path, delete
files, reset Git, or clean local changes to make an error disappear. If Git reports
local changes or a conflict, stop and preserve those changes before updating.

When it works, the terminal says **Signals-only public dashboard on port 8765**.
Leave that terminal open. “Address already in use” means the old dashboard is still
running: find its terminal and press Ctrl+C, then repeat only the final command.

## 3. Open the webpage

1. Click **Ports** beside the Terminal tab along the bottom.
2. Find **8765**. If it is missing, click **Forward a Port** and type **8765**.
3. Right-click its row and choose **Port Visibility → Private**. Keep it Private.
4. Hover over the row and click the globe icon, **Open in Browser**.
5. A new browser tab opens. If asked, sign in to GitHub. Bookmark that tab's address.
   Your existing Codespace and private port keep the same dashboard address.

Do not type `localhost` or `127.0.0.1` into your laptop browser. Those addresses
refer to your laptop, not the remote Codespace. GitHub supplies the correct private
forwarded address. This chat environment cannot provide its own external preview.

## 4. Read the screen

- **WAIT**: no profitable opportunity has been validated. Do not read the
  experimental forecast as a BUY instruction. The current version intentionally
  keeps the recommendation at WAIT until a reviewed profitability promotion
  process exists; model accuracy or a favorable score is insufficient.
- **SCALP NO**: there is no validated scalp recommendation.
- **Not available** for entry, maximum risk or target means the system cannot
  support a trade plan. It does not mean a zero-dollar price or zero risk.
- **Why** explains the recommendation in one sentence.
- **LIVE / WARMING UP / STALE** explains data readiness. LIVE does not mean profitable.
- **OPENING PREDICTION**, at the top, is frozen for this particular contract. Its
  direction, probabilities, target, generation time and data cutoff remain fixed.
  A cold start without pre-opening inputs says **OPENING PREDICTION UNAVAILABLE**;
  the bot never backfills it using later prices. Leave the dashboard running to
  prepare inputs before the next contract opens.
- **LIVE PREDICTION**, immediately below, changes with fresh data. It never
  replaces the opening prediction. Both are experimental forecasts, separate
  from a buy signal. The predicted price is a model-dependent estimate of the
  settlement reference average, not a guaranteed future BTC exchange price.
- **MY LIVE TRADES** will show actual positions and estimated exit P&L after costs
  once the optional read-only account connection is securely configured. Before
  then, it says the account is not connected. Prices and fees may be unavailable.
- **Details** expands the advanced analysis and experimental model estimates.
  History loads only when you click **Load recent history**, with 20 records per
  page. Full research metrics are an optional background calculation. They do not
  hold up the main forecast cards.

Any future entry would be a price cap, not a guaranteed fill. Stops and exit alerts
cannot guarantee a maximum loss. You place every trade and exit manually.

## 5. When you return

A Codespace can stop when idle. Open the same Codespace again and repeat the steps
above. If the dashboard is already running, simply open **Ports → 8765 → Open in
Browser**. If a page looks old after an update, refresh it. Keep the same journal
and account-store files; restarting does not erase them.

Advanced setup and read-only account security: [SIGNALS_DISPLAY.md](SIGNALS_DISPLAY.md).

## One-click launcher after this update

Use **Terminal → Run Task → Start private Kalshi dashboard**. The saved task stops
only matching dashboard processes from this checkout on port 8765, installs missing
monitoring dependencies, uses existing Codespaces secrets, and preserves both databases.
Keep **Ports → 8765 → Port Visibility → Private**. The launcher attempts to enforce
Private visibility with GitHub CLI; if that fails, the task relies on this explicit
private-port confirmation and says so. It does not provide its own login service.

The same action from a terminal is simply:

```bash
python3 scripts/start_dashboard.py --restart --private-dashboard
```

With secrets present, authenticated reads are attempted only after broker metadata
verifies the key has exclusively `read` scope. Missing/unknown/trading-capable scope
blocks account monitoring while the dashboard continues in public-only mode; it cannot honestly confirm read-only broker permissions.
No write endpoint is ever tested to discover permissions. If blocked, public-only
mode still works with `--public-only` added. The account data uses your existing
`signals_account_monitor.db`; confirmed fills/positions remain separate from the
signal journal. No database or WAL/SHM sidecar should be deleted to fix a lock.

The launcher stays running in its terminal. When a Codespace sleeps, reopen it and
run the saved task again, then click the port's globe icon. This avoids repeated
long commands without silently starting account access in other environments.

### What was fixed

SQLite contexts now always close after commit/rollback, use WAL and a 30-second
busy timeout, and serialize same-process access by database path. Server startup
reserves its port before database initialization and holds journal-file locks to
prevent another dashboard on another port from owning the same journal. These
changes prevent common dashboard contention; they do not forcibly unlock another
program's active transaction. The launcher reports/stops only matching dashboard
servers, with a bounded wait and no forced kill. An active unrelated database writer
can still exhaust the timeout; keep its transaction short or stop that writer safely.

BrokenPipe/connection-reset errors usually occur when a tab closes, reloads or a
proxy cancels an HTTP request. The server now treats these as client disconnects,
not failed account syncs. The original Codespace traceback/process state was not
accessible from this implementation environment, so its exact cause is unconfirmed.


## Connection troubleshooting without exposing secrets

After pulling the latest code, use the saved startup task or:

```bash
python3 scripts/start_dashboard.py --restart --private-dashboard
```

Open **Details → Connection Diagnostics**. Credential fields are only true/false:
no API key ID or PEM is displayed. The report separates dashboard-server reachability,
public market requests, production authentication, verified permissions, and account
sync. A failed history request no longer marks live market/account state disconnected.
History loads separately; live refreshes cannot overlap. Candle downloads run outside
the quote polling path, retain original timestamps, and never refresh a stale candle's
age just to make a signal appear.

To run a fresh production check in a second Codespace terminal, using your existing
server-side secrets and authenticated GETs only:

```bash
.venv/bin/python3 scripts/check_dashboard.py --account-check
```

This prints safe status, HTTP/error type, endpoint, booleans and record counts. It
never prints secrets, response bodies, private keys or account/order identifiers.
Account reads are withheld until key metadata explicitly proves exclusively `read`
scope. The application's GET-only allowlist still blocks all order/transfer mutations.

- Secret present = false: the running process did not receive that variable. Reopen
  the existing Codespace after confirming its repository secret assignment; then
  restart the dashboard. Do not create another key or paste one into a command.
- HTTP 401/403: authenticated production metadata request rejected/forbidden. The
  exact HTTP code is preserved; successful secret injection is not successful auth.
- HTTP 429/5xx or transport errors: request failed; the report identifies the endpoint
  and error type. Permission verification retries in the background every 60 seconds.
- `KEY_METADATA_SCHEMA_UNSUPPORTED`, `CURRENT_KEY_NOT_IN_METADATA`, or
  `READ_ONLY_SCOPE_NOT_PROVEN`: a successful response did not prove this key's
  permissions. This may be an API metadata limitation; it does not prove your key
  is bad. The monitor remains disabled rather than assuming read-only permission.
- `MUTATION_SCOPE_PRESENT`: metadata reports trading/transfer capability. Account
  monitoring stays disabled. No write request is sent to test that capability.
- Public request failure: inspect the source, HTTP status and receipt time separately
  from account auth. Coinbase is a proxy; the official BRTI stream remains unavailable.
- Account checking: page is usable while authentication runs in the background.
  A verified successful full account sync is required before balances/positions are
  labeled connected. Failed schemas/history keep cost basis unavailable, not zero.

Never delete the journal, WAL/SHM files or account store to fix a connection error.
Actual authenticated compatibility must be checked in the Codespace containing the
secrets; this development environment does not have access to that process or keys.

### Current Kalshi API compatibility repair

The current official [OpenAPI schema](https://docs.kalshi.com/openapi.yaml)
(version 3.35.0 when checked) documents RSA and Ed25519 signing, hierarchical key
scopes, canonical `outcome_side`/`book_side` fill direction, and fixed-point dollar
`fee_cost` strings. The monitor now handles these explicitly:

- Broad `read` plus documented read-only children is accepted; **every `write` or
  `write::*` scope is refused**. Unknown scopes still block verification. A balance-only
  child scope cannot authorize this full positions/orders/fills monitor.
- Both documented RSA-PSS/SHA256 and Ed25519 keys are loaded from existing server-side
  secrets. No new key is created. The signature still covers timestamp + GET + path,
  without the query string; credentials are never sent to the browser.
- Canonical fills no longer require deprecated `side`/`action`. Direction is
  normalized as equivalent economic exposure for FIFO accounting, not labeled as an
  actual submitted purchase. Conflicting fields fail closed. Current string fee costs
  are dollars; legacy integer-cent fees remain a separate explicitly handled format.
- Positions, fills, orders and cash use the same primary or key-bound subaccount.
  This prevents other subaccounts' fills from being mixed into a position's entry.
  The monitor does not aggregate other subaccounts or manufacture missing history.
- A genuinely empty account store left by a failed startup can adopt the verified
  new key binding. A store containing any account history/settings remains protected
  against mixing keys/accounts. Nothing is deleted to bypass that protection.

Connection labels distinguish **ACCOUNT NOT ENABLED** (secrets present but launch
mode disabled), **MONITORING SECRETS NOT LOADED**, **CHECKING KALSHI CONNECTION**,
**ACCOUNT BLOCKED — SEE REASON**, and a verified connected/stale account. These
states replace the misleading generic OFF label for every failure. No successful
account connection is claimed merely because secrets exist.

Official references: [API keys](https://docs.kalshi.com/api-reference/api-keys/get-api-keys),
[order direction](https://docs.kalshi.com/getting_started/order_direction), and the
OpenAPI `ApiKeyScope`, `ApiKeyType`, `Fill`, and subaccount query schemas. Live-key
compatibility is still unverified until the Codespace holding those secrets runs
the diagnostic check; the compatibility tests use generated keys and mocked data.

Pending-order polling now requests documented `status=resting`; completed trade accounting still uses confirmed fills. This avoids repeatedly paging through canceled/executed order history during each live refresh.

## Reconciliation audit and the dedicated 15-minute engine

Kalshi's [direction migration guide](https://docs.kalshi.com/getting_started/order_direction)
says new integrations should read **canonical `outcome_side` / `book_side`**, not
require deprecated `action` / `side` to agree. `yes` is `bid`; `no` is `ask`.
Buy YES / sell NO imply YES exposure; buy NO / sell YES imply NO exposure. The old
parser wrongly made a deprecated-field disagreement fatal to the entire account sync.

Valid canonical fields now determine exposure; legacy discrepancies are flagged
and their complete raw records retained in the private account database. Conflicting
canonical fields, unsupported legacy-only direction, future timestamps and inconsistent
duplicate IDs are quarantined, not guessed or discarded. Affected entry costs,
fees and P&L remain unavailable. Confirmed cash/position quantities remain available.
Every source version and superseded cached interpretation is retained in additive
audit tables. Corrected authoritative records can resolve quarantine on later sync.
Existing journals are never reset. The account warning/Details show reconciliation
counts. A mocked 2576-fill/10-position bootstrap is part of regression coverage.

**BTC 15-MINUTE PREDICTION ENGINE** forecasts each individual KXBTC15M settlement,
not a 24-hour future price. Background, bounded Coinbase requests preload a rolling
24h candle cache before market openings. Quote polling runs independently, at a
one-second requested cadence (actual interval includes request/processing time).
Kalshi contract metadata and executable books use REST; Kraken public WebSockets
provide available order-flow/depth context with reconnects. No Kalshi authenticated
WebSocket or licensed BRTI stream is claimed. Failed streams remain unavailable.

- Full 1440-bar contiguous historical windows are checked for alignment, freshness,
  missing/duplicate intervals and OHLC validity. Only candles closed when received
  are admitted. The latest complete window can end up to 120 seconds before cutoff,
  reflecting provider publication latency, which is disclosed. Incomplete boundary
  5m/15m/hourly bars are excluded.
- RSI14 uses simple mean gains/losses, MACD uses EMA12/26 and EMA9 signal, EMA50,
  volume-weighted typical-price VWAP and 20-close/two-population-SD Bollinger bands.
  Trend/volume/momentum/acceleration/support/resistance/regime features are correlated
  candidates, never independent confidence votes.
- The transparent live fallback forecasts the final official 60-second average at
  this contract's close, using actual threshold/rules, a constant opening-window
  proxy/index basis assumption, zero drift and max(recent,24h RMS) volatility.
  The opening forecast uses frozen pre-opening history/reference observations and
  its own explicitly labeled 24h volatility baseline. Model estimates remain
  **experimental, uncalibrated, and edge unverified**. Official BRTI is unavailable;
  Coinbase remains a proxy. No indicators are converted into arbitrary percentages.
- Opening estimates are stored separately and never overwritten. BTC inputs must
  have been received by the opening cutoff. Official threshold metadata can arrive
  late: its receipt time and actual generation delay are disclosed. No post-opening
  BTC price/candle is substituted. If frozen inputs were unavailable (including a
  cold start mid-contract), the initial estimate stays unavailable; it is not backfilled.
- Latest revisions have their own generation timestamps/data cutoffs. UP/DOWN are
  complementary only under verified exhaustive binary rules. Expired/stale quotes
  suppress current estimates and all recommendations; recorded initial estimates
  remain visible as historical forecasts. No profitable opportunity is validated,
  so YOUR NEXT MOVE remains WAIT.
- Prediction rows are uniquely keyed, recorded before close/outcome availability,
  and compared with official results only. Initial/latest accuracy, Brier, log loss,
  calibration, probability bands, volatility regimes and 1/7/30-day windows are
  separate from simulations. Missing opening predictions are counted explicitly.
  Prediction archives retain waiting/settled outcomes; no live predictions are
  fabricated for earlier markets.

The offline learner compares statistical/ML candidates against zero-drift and
market-implied baselines, with train-only preprocessing, validation calibration,
label-time purging and a separate holdout. Optional disjoint-block expanding-window
walk-forward reports are available with:

```bash
.venv/bin/python3 -m src.signals_display.learning --journal signals_monitor.db --output signals_model.json --walk-forward
```

Insufficient official labels produce an insufficiency report, not trained weights
or invented calibration. Do not repeatedly retune against the same test period.

**MARKET DATA HEALTH** now reports active/expired/ambiguous selection, exact quote
and BTC-reference ages, server UTC, known opening/close times, missing/future sample
timestamps, HTTP cache Age and server Date/clock differences when supplied. HTTP
200 alone never proves freshness. REST requests ask caches to revalidate; stale
cache Age on live responses is rejected. HTTP Date is a coarse transport diagnostic,
not NTP verification or a reason to rewrite event timestamps. No freshness threshold
was widened. Large fill reconciliation now runs off the market-polling thread.

Run all signals-only offline tests with `.venv/bin/python3 -m pytest -q tests/test_signals*.py`.
Trading/execution/live-order suites are deliberately not run. Live production
reconciliation still requires the existing Codespace with its injected secrets;
this environment cannot access that Codespace. Your reported authentication success
is accepted; a new key or broader permissions are not required by this repair.

Performance/history reads now use isolated read-only WAL snapshots, so long-running scoring queries do not hold the market writer lock. Repeated unchanged candle batches are persisted once per receipt timestamp. Contract-metadata cache age is diagnosed separately from live book/spot cache age; HTTP 200 on metadata cannot refresh an expired contract or replace a fresh executable book.
