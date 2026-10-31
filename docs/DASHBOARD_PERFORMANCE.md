# Dashboard responsiveness verification

Measurements below are from this development environment, using the existing
approximately 961 MB journal. They are local measurements, not guarantees for
your laptop or GitHub's forwarded-port network.

| Check | Before | Updated implementation |
| --- | --- | --- |
| Main state payload | 152,713 bytes | 32,555 bytes, about 79% smaller |
| Main state HTTP response | 31 ms cold; 10.3 ms warm mean | 2.5 ms first sample; 9.5 ms mean across 10 refreshes |
| History UI request | Full research plus records: 10.2 s | 20-record indexed page: 4.0 ms |
| Full historical research | Performed repeatedly by the browser | 8.5 s, background and cached; requested explicitly |
| Main state during background research | 35 ms during the old full-history request | 2.9 ms sample |
| Desktop browser load / first contract display | Not measured | 116 ms / 188 ms |
| Mobile browser load / first contract display | Not measured | 101 ms / 166 ms |
| Main browser render | Not measured | 0.4–0.6 ms samples |
| Public collection / prediction calculation | Previously serial with settlement checks | 216 ms / 23 ms sample, separate collection and prediction workers |
| Mock account snapshot, 2,576 fills | 12.3 ms median | 0.28 ms median |
| Engine performance, 36,000 synthetic revisions | 309 ms median | 7.2 ms median |

History pagination and complete research are different operations; the 4 ms page
measurement does not mean all historical metrics calculate in 4 ms. Browser,
server and exchange timestamps are shown separately. Exchange event-to-receipt
latency is unknown when the public API does not provide an event timestamp.

## Changes

- The first panel is the immutable **OPENING PREDICTION**. The separate live panel
  appears directly below it. Neither is an order instruction.
- Public collection uses a size-one mailbox; old frames do not accumulate behind
  calculations. Account reconciliation, settlement results and research run in
  separate background workers. Main snapshots do not hold the publication lock
  while querying account/manual state.
- Main browser responses omit raw venue books and trade-flow datasets. Details
  retrieve technical data only while expanded. New prediction journal rows
  reference already archived raw feed observations instead of duplicating those
  large observations every second. Existing records are not rewritten or deleted.
- Indexed keyset pages load 20 historical records only on request. Account history
  is independently paginated. Account snapshots cache confirmed reconciliation
  results; guidance reads open manual records rather than all closed records.
- Browser updates preserve the opening record, reject older published responses,
  and expire live forecasts on stale data or contract expiry.
- Discovery uses the official `min_close_ts` and `max_close_ts` filters with no
  `status` filter, as required by Kalshi's compatibility table. The old static
  `status=open` listing was observed returning no active contract through rollover
  even while the supported time-filtered query returned the active contract and
  its official strike. No ticker or threshold is guessed.

## Forecast timing and limitations

An opening calculation took approximately 9 ms in the isolated 1,440-bar test.
Actual opening latency also includes discovery, publication of the official
threshold and data access. Each record stores its actual generation time, opening
delay, data cutoff and source receipts. This calculation time is not a promise of
an opening-second prediction.

The last 24 hours supply historical context for **one specific 15-minute event**.
The opening model excludes observations received after opening and is never
replaced by a later forecast. Missing frozen inputs produce **OPENING PREDICTION
UNAVAILABLE**, including a cold start partway through a contract. Running before
the next opening allows preloading; it cannot guarantee a complete provider feed.

Current forecasts assume zero drift, measured volatility, and a constant
proxy/index opening basis. Indicators are candidate context, not independent
votes or validated directional drift. The projected settlement reference price
is a conditional model mean. The official live CF Benchmarks index is not
connected; exchange proxies and Gaussian ranges have unmeasured model/basis
error. Probabilities remain experimental and uncalibrated; **edge unverified**.

Regression tests cover immutable openings and restart recovery, no future-data
leakage, official rules, stale quotes, rollover, delayed metadata, bounded/indexed
queries, blocked-history responsiveness, older browser responses, read-only
account security, account reconciliation, and desktop/mobile rendering. No order
submission or trading-account modification tests are run.
