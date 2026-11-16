# Oracle preview 8768 install (Windows PowerShell 5.1)

One pasted command installs an exact commit on **preview port 8768 only** and
opens the dashboard through an SSH tunnel. It reuses the guarded deployment:

1. Fetches `claude/accuracy-research` into the existing Oracle checkout and
   verifies the exact 40-character commit is on that branch.
2. Creates a clean staged checkout at `/home/opc/kalshi-preview-repair-<sha7>`
   (an existing directory is reused only if it is already a clean checkout of
   that commit; otherwise the install stops without changes).
3. Runs `deploy/oracle/install_preview_8768.sh`, which:
   - validates that the new code still loads the currently loaded opening
     artifact (blocks before anything stops if not);
   - runs the existing guarded `switch_preview_8768.sh` unchanged (journal
     preserved, 8765/8767 listener identities verified, health / readiness /
     collector / candle-integrity / execution-disabled checks, automatic
     in-memory rollback);
   - re-checks the artifact argument and checksum, the prediction engine, the
     two-field terminal and `execution_capability=false`, and rolls back through
     the switch's own `rollback.sh` if any check fails.
4. Opens a local tunnel `127.0.0.1:18768 → Oracle 127.0.0.1:8768`, confirms
   the tunnel serves the exact commit, then opens the browser.

SSH key: set `$env:ORACLE_SSH_KEY` to force a key file. Otherwise a `*oracle*`
or Oracle Cloud `ssh-key-*.key` file in `%USERPROFILE%\.ssh` is used, falling
back to your normal SSH config and agent.

The final command, with the exact commit in place of `__FINAL_SHA__`:

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
$sshBase = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
else { Write-Host 'No key file found in ~/.ssh; using your SSH config/agent defaults.' }
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) { throw 'OpenSSH client (ssh.exe) is not installed.' }
$bootstrap = @'
set -Eeuo pipefail
SHA=__SHA__
BRANCH=claude/accuracy-research
REPO=/home/opc/kalshi-ai-trading-bot
NEW=/home/opc/kalshi-preview-repair-${SHA:0:7}
[[ $(id -un) == opc ]] || { echo "BLOCKED: must run as opc" >&2; exit 1; }
git -C "$REPO" fetch --quiet --no-tags origin "$BRANCH" || { echo "BLOCKED: Oracle checkout could not fetch $BRANCH from GitHub; nothing changed" >&2; exit 1; }
git -C "$REPO" cat-file -e "$SHA^{commit}"
git -C "$REPO" merge-base --is-ancestor "$SHA" FETCH_HEAD || { echo "BLOCKED: $SHA is not on $BRANCH" >&2; exit 1; }
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
& ssh.exe @sshBase $Server "echo $encoded | base64 -d | bash"
if ($LASTEXITCODE -ne 0) { throw "Install failed (exit $LASTEXITCODE). The guarded installer rolled preview 8768 back automatically; 8765 and 8767 were not touched." }
$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
  $quotedKey = if ($Key) { "-o IdentitiesOnly=yes -i `"$Key`" " } else { '' }
  $tunnelArgs = "-N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15 $quotedKey-L 127.0.0.1:${LocalPort}:127.0.0.1:8768 $Server"
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
