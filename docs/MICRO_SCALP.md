# Bitcoin 15-minute micro-scalp simulator (PAPER ONLY)

`src/micro_scalp/` is a self-contained paper-trading simulator for Kalshi's
`KXBTC15M` (Bitcoin 15-minute UP/DOWN) contracts. It is **not wired into the
running signals service**, imports nothing from it, and cannot place,
modify or cancel a real order. Results are in
[`MICRO_SCALP_RESULTS.md`](MICRO_SCALP_RESULTS.md).

## 1. Safety: why it cannot trade live

| Layer | What it does |
|---|---|
| `safety.PAPER_TRADING_ONLY` | Constant `True`; there is no flag, env var or setting that changes it. |
| `safety.PaperOrderGateway` | The only order interface. `submit_live()` and any `*order*` call (place/create/cancel/amend/batch) raise `LiveOrderSubmissionBlocked`. Orders flagged `real` are refused. |
| `safety.install_guard()` | Called first by every entry point. Blocks importing the repo's authenticated Kalshi client, order executor, order repository and account reader; refuses to start if any is already loaded; refuses `LIVE_TRADING_ENABLED`-style flags; deletes Kalshi key/secret/PEM variables from the process environment. |
| `public_data.PublicGetClient` | Unauthenticated **GET only**, only to Kalshi public market-data paths (`/markets`, `/series/`, `/events`) or a loopback `/api/state`. `/portfolio`, `/orders`, auth headers and other hosts are rejected **before** any network call. |
| Dashboard | Binds `127.0.0.1`, refuses ports 8765/8767/8768, answers POST/PUT/PATCH/DELETE with 405, has no order endpoint. |

All of these are covered by `tests/test_micro_scalp_safety.py` (including a
source scan asserting the package contains no order-submission code).

## 2. How the strategy works (plain English)

Every second the simulator looks at the current BTC 15-minute contract and
asks one question: *"If I buy now and sell soon, do I expect to make money
after the spread, slippage and both Kalshi fees?"* It answers **WAIT** unless
the answer is clearly yes.

1. **Inputs (only data already received):** best bid/ask for UP and DOWN,
   order-book depth (live), the public trade tape (who was the aggressor and
   how much), recent price changes (10/30/60s), short-term volatility,
   trading volume, time left in the contract, and, when enabled, the
   **existing AI**'s live probability and its OPENING forecast as two separate
   inputs.
2. **Short-horizon forecast:** a ridge regression predicts how much the UP
   mid-price will move over the next 30 seconds. It is trained *walk-forward*:
   a training example only becomes usable after its 30 seconds have passed.
3. **Out-of-sample skill gate:** every forecast is scored later against what
   actually happened. If, over the last ~3000 forecasts, the model has **not**
   beaten the naive "price won't change" forecast (R² ≤ 0), or fewer than
   1000 forecasts have been scored, the simulator
   refuses to trade. No edge is assumed.
4. **Cost hurdle:** expected profit per contract =
   `expected exit bid − (ask + slippage) − entry fee − exit fee`.
   It must be at least **+1¢/contract** for BUY UP or BUY DOWN. At a 50¢ price
   the round-trip hurdle is roughly 5–6¢ (1¢ spread, 1¢ slippage allowance,
   ~3.5¢ in fees), so most moments are WAIT.
5. **Other entry filters:** spread ≤ 3¢; price between 0.10 and 0.90; quotes
   no older than 5s; no new entries in the last 2 minutes; 15s cooldown after
   an exit; risk manager must allow it.
6. **SELL rules** (first one that applies): risk limit hit → flatten;
   contract closes in ≤ 30s; profit target +3¢/contract net; stop −5¢/contract
   net (a stop is **not** a guaranteed price: it sells at whatever bid is
   really there); 90s time limit; or the model now expects the held side to
   fall. If no exit fill is possible, the position settles at $1 or $0 at the
   official result.

Multiple entries and exits per 15-minute contract are allowed (one position at
a time).

## 3. Position sizing and risk limits

* **Size = floor(1% of equity ÷ worst-case loss per contract)**, where
  worst-case loss = entry price + entry fee (a binary contract can go to $0).
  Equity includes open positions valued at their *current bid after fees*,
  so size shrinks automatically after losses and grows only with equity.
* Capped by: 5% of equity in total open cost; 25% of displayed depth at or
  better than the limit price; 1000 contracts per order.
* **No martingale or recovery logic exists:** size never depends on recent
  wins or losses (tested).
* **Daily loss limit:** −3% from the start-of-UTC-day equity → flatten, no new
  trades until the next UTC day.
* **Consecutive losses:** 5 losing trades in a row → 1 hour pause.
* **Kill switch:** −10% from peak equity → flatten and stop simulated trading
  permanently for that run.

All values live in `RiskConfig`/`StrategyConfig` (`src/micro_scalp/risk.py`,
`engine.py`) and are validated (e.g. risk per trade above 5% is refused).

## 4. Execution realism

* **Latency:** an order decided at time T reaches the simulated exchange at
  T + 1.0s and is immediate-or-cancel for 2.0s. Limit = decision quote ± 1¢.
* **Live recordings (with depth):** the order fills against the **first
  order-book snapshot received after arrival**, walking price levels up to the
  limit, taking at most 25% of each level's displayed size (queue position
  and competing traders are unknown). Liquidity we take is remembered for 5s
  so later orders can't reuse it.
* **Historical data (Kalshi publishes no past depth):** the order fills only
  if a real trade print *after arrival* proves the price was executable on
  our side (a taker-YES print at p shows a YES ask at p), up to 25% of that
  print's size, each print used once, plus an extra 1¢ slippage.
* **Fees:** Kalshi taker fee `ceil₍¢₎(0.07 × contracts × P × (1−P))`, charged
  per simulated execution, both legs. Settlement payouts have no fee.
* **Unfilled orders** are recorded as UNFILLED and never count as trades.
  Partial fills open/close only the filled quantity.
* **No look-ahead:** every input carries the time it became available
  (receipt time live; exchange time + 0.25s for historical prints; candle
  *end* for candle closes). A decision at T uses only events strictly before
  T. Tests assert this for decisions, training labels and fills.

## 5. AI integration (existing models untouched)

The existing models in `src/signals_display/` are not modified. The recorder
reads their published state **GET-only** from a separately running signals
dashboard and stores:

* `live`: the prediction engine's current UP probability (`prediction_engine.up`),
  with its `updated_at` / `data_cutoff`. It's used only if generated no later
  than receipt, not stale, and ≤ 5s old.
* `opening`: the frozen OPENING forecast (`prediction_engine.initial`),
  kept in its own field and its own model input.

The evaluation runs the same strategy **with and without** these inputs
(`market_only` vs `with_existing_ai`), and separately computes the Brier score
of the existing live probability against official outcomes compared with the
market's own mid-price. No confidence score is created by this package.

## 6. Running it

All commands are run from the repository root inside a virtualenv with
`pip install -e '.[dev,dashboard]'`. None of them needs credentials.

```bash
# Tests
python -m pytest -q tests/test_micro_scalp_safety.py tests/test_micro_scalp_engine.py

# 1. Cache settled history (GET-only, public)
python -m src.micro_scalp.historical --out data/micro_scalp/historical --markets 150 --before 2026-11-19T03:30:00Z

# 2. Record fresh, unseen data (optionally with existing AI state from a SEPARATE
#    signals dashboard you started yourself on a free port such as 8790)
python -m src.micro_scalp.recorder --out data/micro_scalp/fresh.jsonl --minutes 120 \
    --signals-url http://127.0.0.1:8790/api/state

# 3. Evaluate (historical dev/holdout + fresh, with no-trade baseline and AI ablation)
python -m src.micro_scalp.backtest --historical data/micro_scalp/historical \
    --fresh data/micro_scalp/fresh.jsonl --out data/micro_scalp/reports/report.json

# 4. Dashboard: replay a recording, or run live paper simulation
python -m src.micro_scalp.dashboard --replay data/micro_scalp/fresh.jsonl --speed 20 --port 8791
python -m src.micro_scalp.dashboard --live --port 8791 --record data/micro_scalp/live.jsonl \
    --warm-start data/micro_scalp/historical
```

## 7. Future isolated deployment (NOT done; requires your explicit approval)

The goal is a second, independent process that cannot affect the current
deployment. Nothing below has been executed.

**Preconditions:** this branch reviewed and merged by you; CI green. Do not
use ports 8765, 8767 or 8768. Do not stop, restart or edit the existing
PowerShell deployment, `kalshi-signals.service`, or `deploy/oracle/*`.

### Windows (alongside the PowerShell deployment)

```powershell
# New folder: never inside the running deployment's directory
git clone https://github.com/dwayneharrington1991-droid/kalshi-ai-trading-bot C:\kalshi-micro-scalp
cd C:\kalshi-micro-scalp
git checkout feature/btc-micro-scalp-paper-sim   # or main after merge
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -e ".[dev,dashboard]"
.\.venv\Scripts\python -m pytest -q tests/test_micro_scalp_safety.py tests/test_micro_scalp_engine.py
# Start in a NEW PowerShell window; Ctrl+C stops only this process
.\.venv\Scripts\python -m src.micro_scalp.dashboard --live --port 8791 --record data\micro_scalp\live.jsonl
# Open http://127.0.0.1:8791
```

Leave out `--signals-url` at first. Pointing it at the running dashboard
(`http://127.0.0.1:8768/api/state`) adds one read-only GET per second to that
service. Only do that after you approve it.

### Oracle Linux (separate user, directory, unit and port)

```bash
sudo useradd --system --create-home --home-dir /opt/kalshi-micro-scalp microscalp
sudo -u microscalp git clone https://github.com/dwayneharrington1991-droid/kalshi-ai-trading-bot /opt/kalshi-micro-scalp/app
cd /opt/kalshi-micro-scalp/app && sudo -u microscalp git checkout <approved-commit>
sudo -u microscalp python3.12 -m venv /opt/kalshi-micro-scalp/venv
sudo -u microscalp /opt/kalshi-micro-scalp/venv/bin/pip install -e '.[dev,dashboard]'
sudo -u microscalp /opt/kalshi-micro-scalp/venv/bin/python -m pytest -q tests/test_micro_scalp_safety.py tests/test_micro_scalp_engine.py
```

`/etc/systemd/system/kalshi-micro-scalp.service` (new file; existing units untouched):

```ini
[Unit]
Description=Kalshi BTC micro-scalp PAPER simulator (no live orders)
After=network-online.target

[Service]
User=microscalp
WorkingDirectory=/opt/kalshi-micro-scalp/app
ExecStart=/opt/kalshi-micro-scalp/venv/bin/python -m src.micro_scalp.dashboard --live --host 127.0.0.1 --port 8791 --record /opt/kalshi-micro-scalp/data/live.jsonl
# No EnvironmentFile: the simulator needs no credentials and scrubs any it sees.
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/kalshi-micro-scalp/data
ProtectHome=true
PrivateTmp=true
MemoryMax=512M
CPUQuota=50%
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo install -d -o microscalp /opt/kalshi-micro-scalp/data
sudo systemctl daemon-reload
sudo systemctl start kalshi-micro-scalp      # do NOT 'enable' until you've reviewed it running
ss -ltnp | grep 8791                         # must show 127.0.0.1:8791 only
# View via SSH tunnel: ssh -L 8791:127.0.0.1:8791 opc@<host>
# Roll back: sudo systemctl stop kalshi-micro-scalp && sudo rm /etc/systemd/system/kalshi-micro-scalp.service
```

Its Kalshi polling (~1–2 requests/s) shares your IP's public rate limit with
the existing service. Watch for 429s in both before leaving it running.
