# KXBTC15M rule-source policy

The monitor treats the current Kalshi API response as the runtime source of
truth. It does not derive a strike, comparison operator, contract clock, or
settlement rule from a ticker.

Authoritative interfaces that must be checked at deployment time:

- Kalshi Trade API v2 series metadata: `GET /trade-api/v2/series/KXBTC15M`
- Kalshi Trade API v2 market metadata: `GET /trade-api/v2/markets/{ticker}`
- Kalshi API documentation: <https://docs.kalshi.com/>
- CF Benchmarks BRTI product page: <https://www.cfbenchmarks.com/data/indices/BRTI>

The application records the exact primary and secondary rule text, settlement
source, structured settlement specification, price increment, metadata receipt
time, and a SHA-256 rule fingerprint in an immutable opening snapshot. A
structured arithmetic averaging rule is supported when the API supplies its
method, window, and observation interval explicitly. The current compatibility
path also accepts the exact Kalshi-published BRTI sentence already verified by
the collector (including the repeated "simple average of the sixty seconds"
language), fingerprints that text, and rejects changed or paraphrased wording
for review. Otherwise the official settlement calculation is
`SETTLEMENT_CALCULATION_UNVERIFIED`.

External BTC/USD feeds are labeled `ESTIMATED_REFERENCE`. They are never named
or displayed as the official CF Benchmarks index. Equality is taken only from
the API `strike_type`; `greater_or_equal` and `less_or_equal` include equality,
while `greater` and `less` do not.

This repository's November 4, 2026 development environment received HTTP 403
from its outbound proxy when requesting the Kalshi and CF Benchmarks sources.
Consequently, this commit does not claim that a static rule version or a
60-second averaging assumption was independently reverified. Oracle preflight
must record successful source access and current metadata before any production
cutover.
