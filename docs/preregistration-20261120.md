# Pre-registered forward test of opening challengers (frozen 2026-11-20 06:15 UTC)

Research only. Nothing here changes the live model `b50b9dc30f6d664c`, the 0.55 threshold, the
journal, alerts or production 8768. Automatic promotion on Oracle is disabled (2026-11-20 05:47 UTC).

## Why
In-sample and walk-forward research on 2026-11-20 (docs/research-20261120-edge-decay.md) tried more
than ten variants. Over the 113 openings from 2026-11-18 22:30, the best (core16) had Brier 0.2390
vs the live model's 0.2489, but its 95% interval for the difference (-0.032 to +0.013) included zero,
and choosing the best of many inflates it. The only honest test left is on openings that had not
happened when the candidates were fixed.

## Frozen definitions (`scripts/forward_eval_openings.py`; this commit is the freeze)
- **dist**: L2 logistic (ridge 1.0) on `distance_scaled`.
- **core16**: L2 logistic (ridge 20.0) on the live artifact's 16 price_core inputs, using the live
  model's own preprocessing (missing input = neutral).
- Refit before every opening on openings that opened at least 30 minutes earlier. Both were settled
  by then.
- Compared with the live model's immutable P(UP) and a past-only base rate.

## Decision rule
- Only openings with `opens_at >= 2026-11-20T06:15Z` count. At least **200** are required, about
  2 days of openings.
- A candidate passes only if BOTH of these hold against the live model on the same openings:
  - the 95% bootstrap interval of (candidate Brier minus live Brier) is entirely below zero;
  - the 95% interval of after-cost P&L per $1 contract is entirely above zero. Trades use the 0.55
    call rule, buying at the quoted ask nearest the open plus Kalshi's quadratic fee.
- Passing makes a candidate eligible for a separate promotion review, which needs your approval. It
  never deploys anything. A changed candidate is a new experiment with a new freeze time.

## How to run (read-only)
1. Export the journal tables with the read-only research export.
2. Run
   `python scripts/forward_eval_openings.py --export journal_dump.json --artifact <live artifact json>`.

The script prints the report and writes nothing else.

## Resuming after 2026-11-22 (no deployment, no running session needed)
The running 8768 bot (3ce9071) records everything this test needs at every opening: the immutable opening
record with its frozen inputs (`btc_engine_opening`), the official settlement (`forecast_outcomes`)
and the Kalshi quotes near the open (`opening_quote_snapshots`). No code on the branch or on Oracle
deletes from these tables. Automatic promotion is disabled, so the live model compared against stays
`b50b9dc30f6d664c`.

The 200th forward opening is 2026-11-22 08:00 UTC and settles about 08:16, if none is missing.
Run any time after that, from Windows PowerShell on this PC:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\kalshi-bot-dev\repo\scripts\Run-ForwardEval.ps1"
```

What it does:
1. Refuses to run if `scripts/forward_eval_openings.py` differs from the freeze commit `c08fb8c`.
2. Exports the three tables read-only over SSH (`scripts/export_opening_research.py`, SQLite mode=ro,
   nice 19).
3. Copies the live artifact read-only and warns if its sha256 is not `934d490a…302b`.
4. Prints and saves `report.json` under `kalshi-bot-dev\forward\<UTC time>\`.

Reading the result:
- `NOT ENOUGH FORWARD DATA` means rerun later.
- `NO CANDIDATE PASSED` means the challengers are rejected; the live model stays.
- `ELIGIBLE FOR A SEPARATE PROMOTION REVIEW` means a candidate passed. Nothing is deployed; promotion
  needs a separate, explicit approval.

Verified 2026-11-20 06:45 UTC: the full path ran end to end (1 forward opening, NOT ENOUGH FORWARD DATA),
and the script refused to run after a deliberate edit to the frozen evaluator.

What could interrupt collection:
- 8768 stopped or redeployed. Openings missed while it is down are simply absent; the report counts
  only openings that exist.
- A change of live model. The script warns about this.
