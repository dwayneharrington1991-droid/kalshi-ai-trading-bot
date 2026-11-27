# Live BTC order-book data contract (for the dashboard and Codex research)

The collector is `scripts/orderbook_service.py`, run as the `kalshi-orderbook` service. The logic is
`src/signals_display/orderbook_live.py`. Data is read-only public market data.

## Sources (venues are never combined)
| Venue | Book | Trades | Integrity |
|---|---|---|---|
| Coinbase BTC-USD | Advanced Trade `level2` (full book) | `market_trades`; `side` is the MAKER side, so aggressor = opposite | `sequence_num` must step by 1, else RESYNC and reconnect |
| Kraken BTC/USD | WebSocket v2 `book`, depth 25 | `trade`; `side` is the taker/aggressor | CRC32 checksum of the top 10 asks then bids (price 1 dp, qty 8 dp), else RESYNC and reconnect |

Units: price in USD, sizes in BTC. Kalshi contract liquidity is separate; it stays in the existing
`scalping_monitor` and is never mixed with spot depth.

## `/api/orderbook` (from `~/dual-ai-alerts/orderbook_latest.json`, written every second)

```
status: OK | STALE | UNAVAILABLE;  snapshot_age_s
venues.{coinbase,kraken}:
  status LIVE | STALE | RESYNC | CONNECTING | DISCONNECTED   (LIVE only if event age < 5 s and book not crossed)
  last_event / last_receipt (epoch s), age_s
  best_bid, best_ask, mid, spread, spread_bps
  bids / asks: [[price, size_btc, cumulative_btc] x 20]
  bands {"0.05%","0.10%","0.25%","0.50%"}: bid_btc, ask_btc, imbalance
  walls: up to 3 per side with size >= 5x the side's top-20 median (price, size_btc, x_median, distance_pct)
  wall_events vs the previous second: NEW, GREW (>+20%), SHRANK (<-20%), GONE with classification
      MATCHING EXECUTIONS OBSERVED | NO MATCHING EXECUTIONS OBSERVED, with matching_trade_btc
  trades {1s,5s,15s,30s,60s,300s}: buy_btc, sell_btc, trades, large_trades (>=1 BTC), imbalance
  cvd_btc (since collector start), pressure {label BUY|SELL|MIXED|UNAVAILABLE + rule; descriptive only}
```

A level can disappear from the displayed wall list because its size, rank, or relative size changed.
Matching executions must share the exact price and compatible aggressor side. The label does not
assign the decrease to trades or cancellations.

## Research capture
`~/dual-ai-alerts/orderbook_research.sqlite3`, table `book_seconds`. One row per venue per second:
ts, venue, status, mid, spread, depth ±0.25%, imbalance ±0.10%/±0.25%, 1 s buy/sell, CVD, largest walls, and event
age. Retention is 14 days (about 35 MB/day). Point-in-time: a row is written when the second ends, from data
already received.

**Not yet validated as predictive.** Any use in a model must go through a chronological test.
