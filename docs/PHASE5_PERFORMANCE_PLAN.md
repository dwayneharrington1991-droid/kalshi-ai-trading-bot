# Phase 5 performance repair and Oracle CPU action plan (for Codex review)

Author: Claude (implementation). This is NOT an independent review.

## Evidence (Oracle, 2026-11-18 02:20 UTC, read-only profile)

- 1 CPU core, load 8.4, 0% idle, 4-5% steal, 1.7 GB swap in use, 143 MB free.
- Four dashboard servers share that core (CPU over 60 s):

  | Port | CPU | RSS | Uptime | Directory | Journal |
  |---|---|---|---|---|---|
  | 8765 production | 42.2% | 1418 MiB | 258.7 h | release `8e0ff24…` | 43.8 GB `signals_monitor.db` |
  | 8767 older preview | 22.4% | 704 MiB | 366.7 h | `kalshi-new-dashboard-preview` | 41.2 GB `private-preview.db` |
  | 8769 gap-repair test | 22.2% | 571 MiB | 47.6 h | `kalshi-gap-repair-stage` | 4.1 GB `repair-test-8769.db` |
  | 8768 Phase 5 preview | 20.8% | 403 MiB | 1.6 h | `kalshi-preview-upgrade` | 4.35 GB |

- 8768 (build 6c19623) cycle: prediction calculation p50 1.70 s / p95 3.30 s,
  advanced analysis p50 0.79 s, publish-after-collection p50 2.59 s,
  p95 4.25 s, p99 5.15 s. Quote/reference ages swing 2.4 s to 8.3 s, so
  the 5-second freshness limit is met only intermittently.
- A 23.7 GB backup from 2026-11-07 sits in `/home/opc/kalshi-backups`; disk 92%.

## Root cause of the slow prediction cycle (measured offline, realistic inputs)

Profiling the real `Monitor.tick` with 1,440 candles, a Coinbase quote/trade
stream and the live model (`scratchpad live_harness.py`, see commit message):
one cycle cost 169 ms of CPU on a fast idle core. At ~21% of a slower shared
core that is the observed 1.7–3.3 s. The dominant cost was re-validating and
re-deriving the SAME 1,440 candles several times every cycle although they
change about once a minute:

| Function | Per cycle | Calls/cycle |
|---|---|---|
| `context24` (24 h context) | ~33 ms | 1 |
| `candle_context` | ~24 ms | 2 (estimate + settlement) |
| `technical_context` | ~20 ms | 1 |
| opening bar counter (grace window only) | ~18 ms | 6 |

## Code repairs (identical outputs; no model, threshold or decision change)

Commit b6af8ad (previous): paper-strategy summary recomputed only when its
insert-only inputs change; `/readyz` without the full dashboard snapshot;
background analytics at per-thread nice 10.

This commit:
1. `candle_memo.py` + wrappers for `context24`, `candle_context`,
   `technical_context`: exact reuse keyed by full candle CONTENT and the exact
   quantity through which the clock selects bars; only successful results are
   cached; every clock-dependent freshness check is re-run each call; every
   error path calls the original. Randomized equivalence tests across gaps,
   duplicates, misaligned/invalid bars, missing fields, stale receipts and
   clock values crossing every threshold.
2. Opening-window bar counter: per-bar constants computed once (same
   arithmetic).

Measured on the realistic harness: cycle 169 ms -> 78 ms (-54%), prediction
calculation 148.5 ms -> 58.4 ms (-61%). Expected effect on Oracle at the same
CPU share: roughly halves 8768's cycle and its quote age. Not yet measured on
Oracle.

## 8767 and 8769: what is known and what must be confirmed

- 8769 started ~2026-11-16 02:45 UTC, minutes after Codex commit 7ba1763
  ("Correct historical trade candle recovery", branch
  `codex/opening-gap-repair`, 02:42 UTC); directory name and journal name
  (`repair-test-8769.db`) indicate a staging test. Likely owner: Codex.
- 8767 started ~2026-11-02 15:40 UTC, matching Codex's 2026-11-02 development
  preview (`private-preview.db`, branch `codex/kxbtc15m-signals-display`).
  Earlier user rule: "Never modify 8765 or 8767."
- UNCONFIRMED until `collect_service_inventory.sh` runs: launch mechanism
  (systemd/cron/shell), checked-out commit, whether any client (browser via
  SSH tunnel) uses them, and whether any other process reads their journals.

## Proposed action plan (each step needs explicit owner approval)

1. Run `collect_service_inventory.sh` (read-only) and confirm 8769/8767
   ownership, launch mechanism, clients and dependents.
2. Ask Codex/owner whether 8769's gap-repair test is finished. If yes and no
   clients or dependents are seen: stop 8769 gracefully (SIGTERM, verify the
   port frees, keep its journal and directory untouched). Frees ~22% CPU.
3. Decide on 8767 separately (protected by an earlier rule): if superseded by
   8768, stop it the same way; keep its 41 GB journal. Frees ~22% CPU.
4. Codex independently reviews b6af8ad + this commit; then deploy 8768 with the
   readiness gate from d61b0e0, only after steps 2/3 reduce contention.
5. Disk: decide whether the 2026-11-07 23.7 GB backup can be moved off-host;
   never delete without the owner's explicit approval.
6. Production 8765 is not touched by any step; its upgrade (which would bring
   the same CPU savings) is a separate decision.
