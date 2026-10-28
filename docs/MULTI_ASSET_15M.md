# Additional 15-minute market cards

The dashboard dynamically discovers public Kalshi markets near settlement and
shows a side card only when a series can be mapped unambiguously to a supported
asset and its published rules verify the YES-to-UP comparison, settlement
source, threshold, and averaging language. The initial supported registry is
BTC, ETH, SOL, XRP, DOGE, gold, and silver. A listing is never invented.

Each ticker gets one immutable opening record. It requires a proxy observation
received no later than five seconds before the effective opening plus exactly
1,440 contiguous, closed one-minute bars received before opening. The active
experimental baseline uses the opening proxy, official threshold, and measured
24-hour volatility. Momentum and trend fields are recorded as shadow features;
they cannot change a displayed probability until asset-specific chronological
out-of-sample validation supports promotion.

Crypto history and spot observations use documented public Coinbase products
where available. They are proxies, not official settlement indexes. No free,
licensed independent metals reference is configured, so metals remain
`UNAVAILABLE` with an explicit reason. The system does not substitute a later
observation, a midpoint, or a made-up price.

Opening records and official results are stored in `multi_asset_openings` in
the existing SQLite journal. Per-asset sample count and Brier score are exposed
for learning readiness. Insufficient evidence remains blocked from model
promotion and from any verified-edge label. This feature performs public GET
requests and display/storage work only; it has no order or transfer method.
