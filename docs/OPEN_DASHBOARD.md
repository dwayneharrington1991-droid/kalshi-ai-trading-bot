# Open your dashboard on your laptop

The dashboard helps you decide what to watch. It cannot place a trade. Kalshi is
not connected yet. We will configure the optional read-only connection separately;
never paste a key into chat or the webpage.

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
   the code and starts public market-data monitoring. It does not connect Kalshi.

```bash
cd /workspaces/kalshi-ai-trading-bot &&
git fetch origin &&
git switch codex/kxbtc15m-signals-display &&
git pull --ff-only origin codex/kxbtc15m-signals-display &&
python3 -m venv .venv &&
.venv/bin/python3 -m pip install httpx==0.27.0 websockets==16.1.1 &&
.venv/bin/python3 -m src.signals_display.server --mode public --host 0.0.0.0 --port 8765 --journal signals_monitor.db --target 0.05 --stop 0.10 --horizon 60 --manual-delay 3 --slippage 0.01
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
- **Settlement outlook** is an experimental forecast, separate from a buy signal.
- **MY LIVE TRADES** will show actual positions and estimated exit P&L after costs
  once the optional read-only account connection is securely configured. Before
  then, it says the account is not connected. Prices and fees may be unavailable.
- **Details** expands the advanced analysis and experimental model estimates.

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
