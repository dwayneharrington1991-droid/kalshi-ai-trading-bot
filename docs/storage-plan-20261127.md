# Oracle storage plan for 8768 (2026-11-27)

## Facts measured on the server
- **Boot disk:** `/dev/sda` is 150 GB. LVM `ocivolume` has root 132.9 GB (xfs) and `/var/oled` 15 GB (89% used,
  separate). The volume group has no free space. Oracle's `oci-growfs` and `growpart` are installed.
- **Instance:** VM.Standard.A1.Flex, 1 OCPU, 6 GB RAM, us-phoenix-1. The Always Free allowance is 200 GB of block
  storage **in total**, so at most about 50 GB of headroom if no other block volumes exist. No OCI CLI or API key
  is configured on the server, so other volumes, the tenancy type and limits must be checked in the OCI console.
- **Root after recovery (2026-11-27 02:00 UTC):** 17 GB free. The journal is about 38.5 GB and grows about
  4.1 GB/day:

  | Table | Rows/day | Bytes/row | GB/day |
  |---|---|---|---|
  | `predictions` | 56,934 (about one per 1.5 s) | 32,010 | 1.82 |
  | `feed_observations` | 268,161 | 6,094 | 1.63 |
  | `scalp_book_observations` | 56,934 | 2,208 | 0.13 |
  | `btc_engine_predictions` | 56,934 | 1,499 | 0.09 |
  | `intelligence_minute_memory` | 1,423 | 53,880 | 0.08 |
  | `edge_observations` | 113,869 | 245 | 0.03 |
  | everything else | | | < 0.02 |

- **Done already:** removed 23 of my temporary exports and 34 old release checkouts. Compressed and verified the
  two Nov 19 journal test snapshots (18.5 GB down to 4.4 GB). The watchdog alerts at 12 GB free, about 3 days'
  warning.

## Step 1 — expand the boot volume (needs you, in the OCI console; no downtime)
1. Check in Console → Storage that the total of boot and block volumes stays ≤ 200 GB if you want to remain in
   Always Free. Today the boot volume alone is 150 GB, so the safe target is ≤ 200 GB.
2. Take a boot-volume backup first (Console → Boot volume → Create backup). This is the restore point; it is free
   within the Always Free backup allowance (5 backups).
3. Console → Compute → instance → Boot volume → Edit → size 200 GB. Online resize is supported.
4. On the server (I can run these when you confirm the console step is done):
   `sudo /usr/libexec/oci-growfs -y` rescans the disk, grows partition 3, the PV and the root LV, and xfs grows
   online. Then verify with `df -h /`.
5. Result: about 67 GB free, roughly 16 days at the current growth. This buys time; it does not stop growth.

## Step 2 — write less from now on (code change; needs your approval)
The locked opening calls (`btc_engine_opening`), MINUTE-6 FINAL and FIRST 6 MIN (alert-service tables), official
settlements (`forecast_outcomes`), the 7-day scores and the order-book research table do **not** depend on the
1.5 s `predictions` rows. They are unaffected by any of the following.

**a) `predictions`:** keep one compact row every tick (ticker, timestamp, direction, P(UP), scalp; about
200 B). Write the full ~32 KB payload only every 10 s, or when the direction changes, or when P(UP) moves ≥ 0.01.
That keeps the tick-level prediction path for scoring and charts, and cuts about 1.6 GB/day.
- Readers that parse `payload` (learning, reliability, opening_research, research_slice, the history page and the
  performance reports) must accept compact rows. They would be updated and tested first; historical rows are
  never touched.
- Trade-off: future research gets full engine snapshots at 10 s resolution instead of 1.5 s.

**b) `feed_observations`:** keep every exchange trade and quote event, but store each source's raw payload at most
once per second plus every trade. Executed trades and second-by-second order flow are preserved. The new
order-book collector stores 1 s book and flow aggregates separately, about 35 MB/day with 14-day retention.

Expected growth after both: about 0.7 GB/day (from 4.1).

## Step 3 — optional off-box archive (needs your approval per item)
Copy the compressed Nov 19 snapshots to the already-configured Google Drive remote (`kalshi_oracle_backup`), verify
checksums remotely, and only then remove local copies. The 23 GB Nov 7 backup belongs to the 8765 system and is
**not** touched without your explicit instruction.

## Not allowed (unchanged)
No VACUUM, no deletion or rewriting of journal rows, no change to 8765 data, and no modification of immutable
prediction, settlement or research records.
