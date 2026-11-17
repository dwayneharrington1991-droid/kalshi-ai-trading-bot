# Phase 5 preview-8768 deployment client (Windows PowerShell 5.1).
# All deployment decisions run on Oracle in deploy_preview_8768.sh under an
# exclusive lock (install, verification, guarded recovery). This client only
# checks SSH access, starts the orchestrator, reports its DEPLOY_RESULT, and
# owns the local dashboard tunnel. It never targets ports 8765 or 8767.
param(
  [string]$Sha = '__DEPLOY_SHA__',
  [string]$Branch = 'claude/phase5-opening-repair',
  [string]$Server = 'opc@129.146.120.195',
  [string]$SshExe = 'ssh.exe',
  [switch]$SkipTunnel,
  [scriptblock]$TestTunnelHealth  # test hook only: returns a /healthz object
)
$ErrorActionPreference = 'Stop'
if ($Sha -notmatch '^[0-9a-f]{40}$') { throw 'DEPLOY BLOCKED: an exact 40-character commit is required.' }
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$Key = $env:ORACLE_SSH_KEY
if (-not $Key -and (Test-Path -LiteralPath $sshDir)) {
  $Key = Get-ChildItem -LiteralPath $sshDir -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like '*oracle*' -or $_.Name -like 'ssh-key-*.key') -and $_.Extension -notin '.pub', '.ppk' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$sshBase = @('-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
if (-not (Get-Command $SshExe -ErrorAction SilentlyContinue)) { throw "DEPLOY BLOCKED: OpenSSH client ($SshExe) is not installed." }

function Invoke-Remote([string]$Script) {
  $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Script -replace "`r", '')))
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $global:LASTEXITCODE = 0
  $lines = @(& $SshExe @sshBase $Server "echo $encoded | base64 -d | bash" 2>&1 | ForEach-Object { $s = "$_"; Write-Host $s; $s })
  $code = $global:LASTEXITCODE
  $ErrorActionPreference = $previous
  [pscustomobject]@{ Exit = $code; Lines = $lines }
}
function Get-Marker($Result, [string]$Name) {
  $line = $Result.Lines | Where-Object { $_ -like "$Name=*" } | Select-Object -Last 1
  if ($line) { return $line.Substring($Name.Length + 1).Trim() }
  return $null
}

Write-Host '1/3 Checking SSH access to Oracle (no changes)...'
$pre = Invoke-Remote 'echo SSH_PREFLIGHT_OK'
if (-not ($pre.Lines -contains 'SSH_PREFLIGHT_OK')) {
  throw "DEPLOY BLOCKED: SSH connection or authentication to $Server failed (exit $($pre.Exit)). Check the key in $sshDir or ORACLE_SSH_KEY; nothing was changed."
}

$stage = @'
set -Eeuo pipefail
SHA=__SHA__
BRANCH=__BRANCH__
REPO=/home/opc/kalshi-ai-trading-bot
NEW=/home/opc/kalshi-preview-repair-${SHA:0:7}
[[ $(id -un) == opc ]] || { echo "DEPLOY: must run as opc"; echo "DEPLOY_RESULT=BLOCKED"; exit 2; }
git -C "$REPO" fetch --quiet --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || { echo "DEPLOY: could not fetch $BRANCH"; echo "DEPLOY_RESULT=BLOCKED"; exit 2; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "DEPLOY: commit $SHA not fetched"; echo "DEPLOY_RESULT=BLOCKED"; exit 2; }
git -C "$REPO" merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH" || { echo "DEPLOY: $SHA is not on $BRANCH"; echo "DEPLOY_RESULT=BLOCKED"; exit 2; }
if [[ -e $NEW ]]; then
  [[ $(git -C "$NEW" rev-parse HEAD) == "$SHA" && -z $(git -C "$NEW" status --porcelain) ]] || { echo "DEPLOY: $NEW is not a clean checkout of $SHA"; echo "DEPLOY_RESULT=BLOCKED"; exit 2; }
else
  stage=$(mktemp -d /home/opc/.kalshi-preview-stage.XXXXXX)
  git clone --quiet --no-checkout "$REPO" "$stage/checkout"
  git -C "$stage/checkout" checkout --quiet --detach "$SHA"
  mv "$stage/checkout" "$NEW"
  rmdir "$stage"
fi
exec bash "$NEW/deploy/oracle/deploy_preview_8768.sh" __MODE__
'@

Write-Host "2/3 Deploying $Sha to preview 8768 only (exclusive lock; waits outside the opening window)..."
$deploy = Invoke-Remote ($stage.Replace('__SHA__', $Sha).Replace('__BRANCH__', $Branch).Replace('__MODE__', "deploy $Sha"))
$result = Get-Marker $deploy 'DEPLOY_RESULT'
$runId = Get-Marker $deploy 'DEPLOY_RUN_ID'
switch ($result) {
  'SUCCESS' { Write-Host "Verified on Oracle: build $Sha, same artifact/model/threshold/journal, 8765/8767 unchanged, trading disabled." -ForegroundColor Green }
  'FAILED_PREVIOUS_STILL_RUNNING' { throw "DEPLOY FAILED: the install did not complete; preview 8768 is confirmed healthy on the previous build. Nothing else changed." }
  'ROLLED_BACK' { throw "DEPLOY ROLLED BACK: preview 8768 was restored to the exact previous process (same artifact, journal and environment). See DEPLOY lines above." }
  'BLOCKED_CONCURRENT_DEPLOYMENT' { throw 'DEPLOY BLOCKED: another preview 8768 deployment is running on Oracle. Nothing was changed; try again later.' }
  'BLOCKED' { throw 'DEPLOY BLOCKED: preconditions failed (see DEPLOY lines above). Nothing was changed.' }
  'MANUAL_RECOVERY_REQUIRED' { throw 'DEPLOY STOPPED: MANUAL RECOVERY REQUIRED. Automatic recovery was not safe; follow the DEPLOY_MANUAL steps printed above. Ports 8765/8767 were not touched.' }
  default { throw "DEPLOY RESULT UNKNOWN (ssh exit $($deploy.Exit)): the connection ended without a DEPLOY_RESULT. Do not rerun blindly; check status with: ssh $Server bash /home/opc/kalshi-preview-repair-$($Sha.Substring(0,7))/deploy/oracle/deploy_preview_8768.sh status" }
}

function Invoke-ClientRollback([string]$Reason) {
  Write-Host "LOCAL VERIFICATION FAILED: $Reason - requesting the guarded rollback for run $runId..." -ForegroundColor Red
  $rb = Invoke-Remote ($stage.Replace('__SHA__', $Sha).Replace('__BRANCH__', $Branch).Replace('__MODE__', "rollback $Sha $runId"))
  $rbResult = Get-Marker $rb 'DEPLOY_RESULT'
  if ($rbResult -eq 'ROLLED_BACK') { throw "DEPLOY ROLLED BACK after local verification failed: $Reason" }
  throw "DEPLOY STOPPED: rollback result $rbResult. MANUAL RECOVERY REQUIRED if not ROLLED_BACK; follow the DEPLOY_MANUAL steps above. Reason: $Reason"
}

if ($SkipTunnel) { return 'SUCCESS' }
Write-Host '3/3 Opening a private local tunnel to the dashboard...'
if ($TestTunnelHealth) {
  $health = & $TestTunnelHealth
  if ($health.build_commit -ne $Sha) { Invoke-ClientRollback "tunnel answered with build $($health.build_commit), expected $Sha" }
  return 'SUCCESS'
}
$LocalPort = $null
foreach ($candidate in 18768..18799) {
  if (-not (Get-NetTCPConnection -LocalPort $candidate -State Listen -ErrorAction SilentlyContinue)) { $LocalPort = $candidate; break }
}
if (-not $LocalPort) { Write-Host 'No free local port in 18768-18799; the deployment is verified on Oracle.' -ForegroundColor Yellow; return 'SUCCESS' }
$quotedKey = if ($Key) { "-o IdentitiesOnly=yes -i `"$Key`" " } else { '' }
$tunnelArgs = "-N -o BatchMode=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o ServerAliveInterval=15 $quotedKey-L 127.0.0.1:${LocalPort}:127.0.0.1:8768 $Server"
$tunnel = Start-Process -FilePath $SshExe -ArgumentList $tunnelArgs -WindowStyle Minimized -PassThru
$health = $null
for ($i = 0; $i -lt 40 -and -not $health; $i++) {
  Start-Sleep -Seconds 1
  if ($tunnel.HasExited) { break }
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/healthz" -TimeoutSec 3 } catch { $health = $null }
}
if (-not $health) {
  if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
  Write-Host 'Deployment is verified ON ORACLE, but this PC''s tunnel did not answer (closed it; a local network issue is not rolled back).' -ForegroundColor Yellow
  return 'SUCCESS'
}
if ($health.build_commit -ne $Sha) {
  if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
  Invoke-ClientRollback "this run's own tunnel answered with build $($health.build_commit), expected $Sha"
}
Write-Host "Preview 8768 is healthy at $Sha. Dashboard: http://127.0.0.1:$LocalPort/ (close the minimized ssh window to end the tunnel)." -ForegroundColor Green
Start-Process "http://127.0.0.1:$LocalPort/"
return 'SUCCESS'
