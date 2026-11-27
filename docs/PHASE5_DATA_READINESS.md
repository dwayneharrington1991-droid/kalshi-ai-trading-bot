# Phase 5: opening data collection and research readiness

Branch `claude/phase5-data-readiness`, built on the approved Phase 4 commit
`f52e142`. Research and diagnostics only. The running model, live signals,
journals, artifacts and services are unchanged, and nothing is backfilled.

## Phase 4 facts this addresses

From the Oracle run:
- 160 settled contracts; 95 eligible frozen openings.
- 65 excluded for an unavailable opening probability.
- Four components and 87 inputs; 7 inputs present in fewer than 50% of records.
- 82/82 applicable predictions reproduced exactly.
- Zero pre-open executable quotes; Report B insufficient.

## 1. Why openings have no probability (confirmed in code)

1. **A probability-less opening is permanent by design.** If the inputs are not
   valid by `open + 120 s`, `server.py` saves the opening with `up=None`.
   `save_opening` is `INSERT OR IGNORE` on an immutable table, so no later
   attempt replaces it. The stored `reason` names the failing check.
2. **The input snapshot is frozen on the first tick after the open.**
   `freeze_opening_snapshot(..., finalized=True)` stores whatever history and
   reference ticks are in memory at that tick, and the retries inside the
   120 s window reuse that same snapshot. A gap at that instant therefore
   becomes permanent, even if pre-open data would have become available.
3. **A restart near an opening loses the pre-open reference ticks.** The
   reference points (`ProxyEstimator.opening_references`) are in memory only.
   A process that starts, or first ticks, after the open has none, so the
   opening fails with "No fresh BTC reference received by opening". 8768 was
   restarted repeatedly during the Phase 3/4 installs.
4. **Openings before activation also lack a probability for these reasons.**
   The fallback always produces a probability when inputs are valid, so
   "before the current model was active" is a reporting phase, not a cause.

`data_readiness.py` classifies each of the 65 from its own stored record:
- **cause:** the reason category;
- **timing:** saved at the 120 s deadline inside a running process, or created
  late (the process was not ticking at the open);
- **phase:** before or after the current model was first used;
- **evidence:** whether the 24 h candles were RECORDED by the open (a timing
  or snapshot defect) or GENUINELY MISSING, and whether reference-feed rows
  exist in the journal for the 65 s before the open.

## 2. Pipeline audit

The audit reports, for every opening that has a probability:
- history age at open, reference age at open, and generation delay (percentiles);
- the closed-bar count;
- optional-context status and exclusions by reason;
- Coinbase candle gaps per day and receipt lateness;
- per-source feed gaps over the last 24 h.

## 3. Sparse inputs

Each input below 50% coverage is reported with its source group, the
collection rule it depends on, recent-24 h coverage, and a diagnosis:

| Diagnosis | Meaning |
|---|---|
| `NOT_RECORDED_BY_THE_LIVE_CODE` | The model expects an input the frozen record never contains: a vocabulary mismatch. |
| `SUSPECT_COLLECTION_OR_RECORDING` | A core candle or reference input is sparse; it should always be present. |
| `SOURCE_CONTEXT_ABSENT_AT_OPEN` | Most openings had no optional (order-flow/advanced) context at all. |
| `GENUINELY_UNAVAILABLE_OR_EXCLUDED` | The source exists, but the value was missing or excluded by its staleness/post-open rule. |

**Recording gap (fixed forward-only).** Exclusions were counted by reason,
never by input, and a not-live source left no trace. `prediction_engine.py`
now also stores `optional_feature_status` and `optional_context_available`:
`PRESENT`, `ABSENT_SOURCE_NOT_LIVE_OR_EMPTY`, or `EXCLUDED:<reason>` per input.
A test proves the opening record is otherwise identical to `f52e142`: the
same values, probability, direction and feature version. It takes effect
only after an approved deployment; past records cannot be re-diagnosed this way.

## 4. Pre-open executable Kalshi quotes: design

**Current state (confirmed in code):** market discovery keeps only markets with
status `open` or `active`. The quote recorder needs an ask in (0, 1) observed
within 60 s before to 120 s after the open, and no more than 5 s old at receipt.

**Unknown:** whether Kalshi makes the next contract tradable before its open.
This container cannot reach Kalshi. `kalshi_preopen_probe.py` answers it on
Oracle by polling public market data every 10 s for 20 minutes:
- every row is `OBSERVED`, with its receipt time, status, official
  open/close times, listing bid/ask, and the near-open order book;
- "executable" requires a tradable status, an ask in (0, 1), and positive size
  at the best level;
- a listing ask without depth is reported separately.

**Collection design once the probe has answered:**
- *If pre-open quotes exist:* also list the next contract when its status is
  pre-open but tradable. Record best bid/ask with size and full depth, the fee
  schedule, spread, receipt and event times, and staleness (at most 5 s at
  receipt), in the append-only `opening_quote_snapshots`. Never infer missing
  sides.
- *If not:* a pre-open market baseline is impossible. Use the first executable
  post-open quote only as a cost basis, never as an at-open forecast, and say so.

## 5. Monitoring

`ALERT` lines include:
- the probability-unavailable rate (WARN above 5%, CRITICAL above 20%);
- openings created late;
- settled contracts without an opening record;
- candle gaps in the last 24 h;
- feed gaps over 120 s;
- no pre-open executable quotes;
- sparse inputs, and suspect or unrecorded inputs.

These are research-time alerts, printed by the read-only run. No live service
is modified.

## 6. Accumulating records without changing the model

- **Report B needs 288 eligible frozen openings:** 230 development rows after
  sealing 20%. The audit projects the days remaining at the observed rate.
- **Avoid restarts at openings.** Do not restart 8768 from 90 s before to
  150 s after any quarter hour (:00, :15, :30, :45). Each restart in that
  window can permanently lose one opening.
- **Proposed fixes, not implemented, needing explicit approval and a reviewed
  deployment:**
  1. Do not finalize an opening snapshot that lacks fresh reference ticks or a
     complete window before `open + 120 s`; re-freeze only from receipt-proven
     pre-open sources.
  2. Persist pre-open reference ticks with their receipt times, so a restart
     can recover them receipt-proven (never post-open data).
  3. Add a guard to the preview switch so it refuses to switch inside the
     opening window.

None of these changes the model or rewrites history. They reduce future
probability-less openings.

## 7. Limitations

- Existing records cannot say which optional input was missing, or why.
- Feed-observation rows are candidate evidence of recoverable reference ticks,
  not proof of receipt time.
- Activation time is inferred from the first opening made by an
  `opening-candidate/...` model unless the artifact is supplied.
- The projection assumes the recent rate continues.

## Read-only Oracle command (Windows PowerShell)

Audits every running journal (8768 first), then runs the 20-minute public
pre-open quote probe. No service, port, journal, artifact or model is changed.
Reports are copied to `%USERPROFILE%\kalshi-research\`.

```powershell
& {
$ErrorActionPreference = 'Stop'
$Sha = '__FINAL_SHA__'
$Server = 'opc@129.146.120.195'
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$Key = $env:ORACLE_SSH_KEY
if (-not $Key) {
  $Key = Get-ChildItem -LiteralPath $sshDir -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like '*oracle*' -or $_.Name -like 'ssh-key-*.key') -and $_.Extension -notin '.pub', '.ppk' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$sshBase = @('-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) { throw 'OpenSSH client (ssh.exe) is not installed.' }
$bootstrap = @'
set -Eeuo pipefail
SHA=__SHA__
BRANCH=claude/phase5-data-readiness
REPO=/home/opc/kalshi-ai-trading-bot
[[ $(id -un) == opc ]] || { echo "BLOCKED: must run as opc" >&2; exit 1; }
git -C "$REPO" fetch --quiet --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || { echo "BLOCKED: could not fetch $BRANCH; nothing changed" >&2; exit 1; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "BLOCKED: commit $SHA was not fetched; nothing changed" >&2; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH" || { echo "BLOCKED: $SHA is not on $BRANCH" >&2; exit 1; }
code=$(mktemp -d /home/opc/.kalshi-research-code.XXXXXX)
trap 'rm -rf "$code"' EXIT
git -C "$REPO" archive "$SHA" | tar -x -C "$code"
bash "$code/deploy/oracle/data_readiness_readonly.sh"
'@
$bootstrap = $bootstrap.Replace('__SHA__', $Sha) -replace "`r", ''
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
Write-Host "Running read-only Phase 5 data-readiness audit for $Sha (no service, port, journal or artifact is changed)..."
$ErrorActionPreference = 'Continue'
& ssh.exe @sshBase $Server "echo $encoded | base64 -d | bash" | Tee-Object -Variable captured | Out-Host
$remoteExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$reportDir = ($captured | Where-Object { $_ -like 'READINESS: REPORT_DIR=*' } | Select-Object -Last 1) -replace '^READINESS: REPORT_DIR=', ''
if ($reportDir) {
  $local = Join-Path $env:USERPROFILE 'kalshi-research'
  New-Item -ItemType Directory -Force -Path $local | Out-Null
  $scpBase = @('-o', 'StrictHostKeyChecking=yes')
  if ($Key) { $scpBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key) }
  $ErrorActionPreference = 'Continue'
  & scp.exe @scpBase -r "${Server}:$reportDir" $local
  $ErrorActionPreference = 'Stop'
  Write-Host "Report copied to $local\$(Split-Path $reportDir -Leaf)"
}
if ($remoteExit -ne 0) { throw "Data-readiness audit did not complete (exit $remoteExit). Nothing on the server was changed." }
else { Write-Host 'Phase 5 data-readiness audit complete.' -ForegroundColor Green }
}
```
