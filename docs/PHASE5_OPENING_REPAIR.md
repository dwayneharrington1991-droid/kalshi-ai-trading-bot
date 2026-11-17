# Phase 5: opening-reliability repairs (for independent Codex review)

Branch `claude/phase5-opening-repair`, built on `589ba72` (Phase 5 data
readiness) ← `f52e142` (approved Phase 4) ← release `6c19623`.

**NOT DEPLOYED.** Deployment needs the user's explicit approval after review.

## Live-code scope versus the running build (`6c19623`)

Only these live files change: `server.py`, `monitor.py`, `prediction_engine.py`,
`live_model.py` and `deploy/oracle/switch_preview_8768.sh`. Unchanged: the
dashboard HTML, the opening models and artifact loading, the feature
vocabulary, the decision threshold, probability computation and trading
(none). Every other changed file is a read-only research or diagnostic module.

## What changed

1. **Retry repair.** The opening input snapshot is frozen only when its inputs
   pass the opening model's own checks: a READY, cutoff-safe 24 h window
   received by the opening, and a reference receipt at most 5 s before it
   (`Monitor.opening_inputs_complete`). Before, the first post-open tick
   froze whatever it had, permanently, and every retry reused it.
2. **Original path first.** The opening is generated from the original inputs
   exactly as before. Only if that yields no probability (and no snapshot
   exists) is ONE retry made, from other receipt-proven pre-opening inputs:
   - in-memory pre-open histories, or the journal archive (`first_received_at`
     ≤ open);
   - journal-recovered reference ticks.

   A recovered forecast is labelled with `input_recovery`.
3. **Crash-safe reference receipts.** Every accepted BTC reference tick is
   buffered. The server persists only ticks received in the 70 s before a
   DISCOVERED contract's `opens_at`. They go to the append-only
   `preopen_reference_ticks` table, with UPDATE/DELETE blocked by triggers,
   and record source, receipt time, price, opening and persistence time.

   Recovery (`Journal.preopen_reference_points`) is used only when no
   in-memory points exist, and it rejects:
   - receipts after the opening;
   - receipts outside the 65 s window;
   - rows persisted more than 10 s after receipt, which cannot prove they
     were available before the opening;
   - invalid prices.

   Selection is the same as in memory.
4. **Restart protection.** `deploy/oracle/opening_window_guard.sh` defers a
   planned restart from 90 s before to 150 s after each :00/:15/:30/:45. It is
   called immediately before the switch's planned stop. Rollback and restore
   paths are not guarded. `PREVIEW_EMERGENCY_OVERRIDE=1` skips the wait and
   logs it loudly.
5. **First valid forecast preserved.** Openings remain `INSERT OR IGNORE` on
   an immutable table. A probability-less record is still written only at
   the 120 s deadline. Nothing is overwritten, revised or manufactured.
6. **Per-input availability.** The frozen record stores
   `optional_feature_status` and `optional_context_available`: metadata only,
   with no value, probability or feature-version change.
   `/api/data-quality-alerts` and `data_readiness` report availability for
   every model input (87 for the current artifact).
7. **Quote evidence.** Each recorded opening quote payload also stores:
   - market status;
   - `quote_source` (`DEPTH_REPORTED` or `LISTING_SIZE_UNKNOWN`);
   - `executable` (true only for a tradable status with an ask in (0, 1));
   - proven ask sizes, the spread, and the 1-contract taker fee.

   Nothing is inferred. No schema change; the existing payload column is used.
   Pre-open discovery is unchanged until the read-only probe has verified
   Kalshi's behaviour.
8. **Alerts.** `/api/data-quality-alerts` (read only; does not affect /healthz
   or /readyz) reports:
   - `MISSING_OPENING_PROBABILITY`;
   - `REPEATED_OPENING_FAILURES` (2 or more in a row);
   - `STALE_BTC_FEED` (newest receipt over 10 s old);
   - `CANDLE_WINDOW_NOT_READY`;
   - `MISSING_FEATURE_RECEIPTS`.

## Guarantees and the tests that prove them

| Guarantee | Test |
|---|---|
| Already-valid openings are computed exactly as before (whole record identical to `589ba72` on the real `Monitor.tick` path) | `test_previously_valid_openings_are_computed_exactly_as_before` |
| A restart inside the grace window recovers the same probability from receipt-proven ticks | `test_restart_inside_grace_window_recovers_identical_forecast_from_receipts` |
| Before/after: without persisted receipts the restarted opening is lost | `test_before_fix_behaviour_restart_without_persisted_receipts_loses_the_opening` |
| Incomplete inputs are never frozen permanently | `test_incomplete_inputs_are_never_frozen_into_a_permanent_snapshot` |
| Post-open, late-persisted and out-of-window receipts are rejected; receipts are immutable | `test_recovery_rejects_...`, `test_persisted_reference_receipts_are_immutable` |
| The first valid forecast is never overwritten | `test_first_valid_opening_is_never_overwritten_by_later_ticks` |
| Guard windows, emergency override, unguarded rollback | `test_planned_restart_waits_...`, `test_emergency_override_...` |
| Quote evidence; alerts; endpoint read only | `test_quote_payload_...`, `test_alerts_...` |

Mutation checks: removing the persistence-slack check, accepting post-open
receipts, re-enabling incomplete snapshot freezes, or disabling journal
recovery each makes a test fail.

## Recoverable-opening analysis (SIMULATED, not live performance)

`data_readiness` → `recovery_simulation` classifies every past
probability-less opening, strictly from pre-opening evidence:

| Outcome | Meaning |
|---|---|
| `SIMULATED_RECOVERABLE` | Every failing input is provably available before the open. |
| `HISTORY_PROVEN_REFERENCE_UNVERIFIABLE` | The 24 h candles were received by the open, but no persisted reference receipts exist. |
| `REFERENCE_UNVERIFIABLE_NO_PERSISTED_TICKS` | The journal predates tick persistence. |
| `NOT_RECOVERABLE` | Otherwise. |

Existing journals predate `preopen_reference_ticks`, so past
reference-missing openings cannot be proven recoverable and are not counted.
Recovery applies to openings collected after deployment. Run the Phase 5 audit
(`docs/PHASE5_DATA_READINESS.md`) on this commit to get the real counts for the
65 past failures.

## Known limitations and risks

- After a restart, in-memory microstructure and order-flow context is not
  persisted. Recovered openings compute `micro_*` features from the
  receipt-proven reference points (a path the base code already uses when
  those windows are cold), and some optional inputs are absent.
- After a restart the artifact is loaded after the opening, so it is
  correctly ineligible: a recovered opening is made by the statistical
  fallback. Its confidence is UNCALIBRATED, so it is **not displayed as a
  call**. It adds valid research data, not dashboard calls.
- Reference recovery depends on receipts being persisted within 10 s; a tick
  loop stalled for longer before an opening is conservatively unrecoverable.
- `preopen_reference_ticks` grows by about 100 rows per opening. Its index
  is built by the deferred secondary-index builder, not at startup.
- The guard defers a planned switch by up to 241 s. Emergency recovery is
  unaffected.

## Deployment plan (DO NOT RUN until the user approves)

1. Codex reviews this commit and the tests above; the user approves.
2. Run the guarded preview installer below on a Windows PC with the reviewed
   commit. It:
   - fetches the branch, verifies the commit's ancestry, and stages a clean
     checkout;
   - validates that the running artifact loads with the new code;
   - waits outside the opening window;
   - switches only preview 8768, keeping the same argv, artifact and journal;
   - post-checks `/healthz`, `/readyz`, state, terminal, scoreboard and
     `execution_capability=false`.

   Production 8765 and preview 8767 are never touched.
3. Schema: `preopen_reference_ticks` is created by `CREATE TABLE IF NOT
   EXISTS` at startup. This is additive; existing tables and rows are untouched.
4. After switching:
   - `/api/data-quality-alerts` must answer;
   - the next opening's record should carry `optional_feature_status`;
   - `preopen_reference_ticks` should gain rows in the 70 s before each opening.

## Rollback plan

- **Automatic:** if the switch's post-checks fail, the switch and installer
  restore the exact previous process (same argv, environment and code).
- **Manual:** rerun the guarded installer with the previous build's commit
  (the staged checkout `/home/opc/kalshi-preview-repair-<sha7>` is retained).
  Alternatively, `/home/opc/.kalshi-deploy/preview-8768/rollback.sh`. Use
  `PREVIEW_EMERGENCY_OVERRIDE=1` only for emergency recovery inside an
  opening window.
- **Data:** the rollback leaves `preopen_reference_ticks` in the journal. Old
  code ignores it; it is append-only and harmless. No historical row is
  modified by deploy or rollback.

### Guarded preview installer (Windows PowerShell; `__FINAL_SHA__` = the reviewed commit; run only after approval)

```powershell
& {
$ErrorActionPreference = 'Stop'
$Sha = '__FINAL_SHA__'
$Server = 'opc@129.146.120.195'
$LocalPort = 18768
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$Key = $env:ORACLE_SSH_KEY
if (-not $Key) {
  $Key = Get-ChildItem -LiteralPath $sshDir -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like '*oracle*' -or $_.Name -like 'ssh-key-*.key') -and $_.Extension -notin '.pub', '.ppk' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$sshBase = @('-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
else { Write-Host 'No key file found in ~/.ssh; using your SSH config/agent defaults.' }
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) { throw 'OpenSSH client (ssh.exe) is not installed.' }
$bootstrap = @'
set -Eeuo pipefail
SHA=__SHA__
BRANCH=claude/phase5-opening-repair
REPO=/home/opc/kalshi-ai-trading-bot
NEW=/home/opc/kalshi-preview-repair-${SHA:0:7}
[[ $(id -un) == opc ]] || { echo "BLOCKED: must run as opc" >&2; exit 1; }
git -C "$REPO" fetch --quiet --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || { echo "BLOCKED: Oracle checkout could not fetch $BRANCH from GitHub; nothing changed" >&2; exit 1; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "BLOCKED: commit $SHA was not fetched from $BRANCH; nothing changed" >&2; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH" || { echo "BLOCKED: $SHA is not on $BRANCH" >&2; exit 1; }
if [[ -e $NEW ]]; then
  [[ $(git -C "$NEW" rev-parse HEAD) == "$SHA" && -z $(git -C "$NEW" status --porcelain) ]] || { echo "BLOCKED: $NEW exists and is not a clean checkout of $SHA; nothing changed" >&2; exit 1; }
else
  stage=$(mktemp -d /home/opc/.kalshi-preview-stage.XXXXXX)
  git clone --quiet --no-checkout "$REPO" "$stage/checkout"
  git -C "$stage/checkout" checkout --quiet --detach "$SHA"
  mv "$stage/checkout" "$NEW"
  rmdir "$stage"
fi
echo "STAGED_CHECKOUT=$NEW COMMIT=$(git -C "$NEW" rev-parse HEAD)"
exec bash "$NEW/deploy/oracle/install_preview_8768.sh" "$SHA"
'@
$bootstrap = $bootstrap.Replace('__SHA__', $Sha) -replace "`r", ''
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
Write-Host "Installing $Sha on Oracle preview 8768 (production 8765 and preview 8767 are not touched)..."
# Windows PowerShell 5.1 hosts such as the ISE turn native stderr into errors
# under -ErrorAction Stop; judge the remote result by its exit code only.
$ErrorActionPreference = 'Continue'
& ssh.exe @sshBase $Server "echo $encoded | base64 -d | bash"
$remoteExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($remoteExit -ne 0) { throw "Install failed (exit $remoteExit). Read the PREVIEW_INSTALL / BLOCKED lines above: a blocked install changed nothing, and a failed switch or post-check restores the previous preview 8768 automatically. Ports 8765 and 8767 are never touched." }
$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
  $quotedKey = if ($Key) { "-o IdentitiesOnly=yes -i `"$Key`" " } else { '' }
  $tunnelArgs = "-N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o ServerAliveInterval=15 $quotedKey-L 127.0.0.1:${LocalPort}:127.0.0.1:8768 $Server"
  Start-Process -FilePath ssh.exe -ArgumentList $tunnelArgs -WindowStyle Minimized | Out-Null
}
$health = $null
for ($i = 0; $i -lt 40 -and -not $health; $i++) {
  Start-Sleep -Seconds 1
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/healthz" -TimeoutSec 3 } catch { $health = $null }
}
if (-not $health) { throw "Preview 8768 installed, but the local tunnel on port $LocalPort did not answer. Close the minimized ssh window and rerun this command." }
if ($health.build_commit -ne $Sha) { throw "Tunnel answered with build $($health.build_commit), expected $Sha. Close any old tunnel on port $LocalPort and rerun." }
Write-Host "Preview 8768 healthy at $Sha (signals only; automated trading disabled). Opening dashboard..."
Start-Process "http://127.0.0.1:$LocalPort/"
}
```
