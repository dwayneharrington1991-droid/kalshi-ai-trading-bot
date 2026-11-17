# Phase 5 preview-8768 deployment client (Windows PowerShell 5.1).
# The deployment runs on Oracle as a DETACHED, server-owned job
# (deploy_job_8768.sh -> deploy_preview_8768.sh under an exclusive lock), so
# an SSH disconnect, closed window or Ctrl+C here cannot interrupt it. This
# client only starts the job with a unique request id, polls its status,
# reconnects if the connection drops, asks the server to recover a job that
# died, and owns the local dashboard tunnel. It never targets 8765 or 8767.
#   Resume watching an existing job:   -ResumeJobId <id printed earlier>
param(
  [string]$Sha = '__DEPLOY_SHA__',
  [string]$Branch = 'claude/phase5-opening-repair',
  [string]$Server = 'opc@129.146.120.195',
  [string]$ResumeJobId = '',
  [string]$SshExe = 'ssh.exe',
  [int]$PollSeconds = 5,
  [int]$MaxMinutes = 45,
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
$sshBase = @('-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4', '-o', 'ConnectTimeout=20')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
if (-not (Get-Command $SshExe -ErrorAction SilentlyContinue)) { throw "DEPLOY BLOCKED: OpenSSH client ($SshExe) is not installed." }
$jobScript = "/home/opc/kalshi-preview-repair-$($Sha.Substring(0,7))/deploy/oracle/deploy_job_8768.sh"

function Invoke-Remote([string]$Script, [switch]$Quiet) {
  $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Script -replace "`r", '')))
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $global:LASTEXITCODE = 0
  $lines = @(& $SshExe @sshBase $Server "echo $encoded | base64 -d | bash" 2>&1 | ForEach-Object {
      $s = "$_"; if (-not $Quiet) { Write-Host $s }; $s })
  $code = $global:LASTEXITCODE
  $ErrorActionPreference = $previous
  [pscustomobject]@{ Exit = $code; Lines = $lines }
}
function Get-Fields($Result) {
  $fields = @{}
  foreach ($line in $Result.Lines) {
    if ($line -match '^(DEPLOY_[A-Z_]+|EXIT|FINISHED_AT)=(.*)$') { $fields[$Matches[1]] = $Matches[2].Trim() }
  }
  return $fields
}

Write-Host '1/4 Checking SSH access to Oracle (no changes)...'
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
[[ $(id -un) == opc ]] || { echo "DEPLOY_JOB: must run as opc"; echo "DEPLOY_JOB_STATE=REJECTED"; exit 2; }
git -C "$REPO" fetch --quiet --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || { echo "DEPLOY_JOB: could not fetch $BRANCH"; echo "DEPLOY_JOB_STATE=REJECTED"; exit 2; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "DEPLOY_JOB: commit $SHA not fetched"; echo "DEPLOY_JOB_STATE=REJECTED"; exit 2; }
git -C "$REPO" merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH" || { echo "DEPLOY_JOB: $SHA is not on $BRANCH"; echo "DEPLOY_JOB_STATE=REJECTED"; exit 2; }
if [[ -e $NEW ]]; then
  [[ $(git -C "$NEW" rev-parse HEAD) == "$SHA" && -z $(git -C "$NEW" status --porcelain) ]] || { echo "DEPLOY_JOB: $NEW is not a clean checkout of $SHA"; echo "DEPLOY_JOB_STATE=REJECTED"; exit 2; }
else
  stage=$(mktemp -d /home/opc/.kalshi-preview-stage.XXXXXX)
  git clone --quiet --no-checkout "$REPO" "$stage/checkout"
  git -C "$stage/checkout" checkout --quiet --detach "$SHA"
  mv "$stage/checkout" "$NEW"
  rmdir "$stage"
fi
exec bash "$NEW/deploy/oracle/deploy_job_8768.sh" start "$SHA" __REQUEST__
'@

if ($ResumeJobId) {
  $JobId = $ResumeJobId
  Write-Host "2/4 Resuming deployment job $JobId (nothing new is started)..."
} else {
  $JobId = [guid]::NewGuid().ToString()
  Write-Host "2/4 Starting server-owned deployment job $JobId for $Sha (preview 8768 only)..."
  Write-Host "    If this window closes, resume with:  -ResumeJobId $JobId" -ForegroundColor Cyan
  $started = $null
  for ($attempt = 1; $attempt -le 3 -and -not $started; $attempt++) {
    # Same request id on every attempt: the server never starts a second installation.
    $start = Invoke-Remote ($stage.Replace('__SHA__', $Sha).Replace('__BRANCH__', $Branch).Replace('__REQUEST__', $JobId))
    $f = Get-Fields $start
    if ($f['DEPLOY_JOB_STATE'] -eq 'BUSY') { throw "DEPLOY BLOCKED: another preview 8768 deployment job ($($f['DEPLOY_ACTIVE_JOB'])) is running on Oracle. Nothing was started." }
    if ($f['DEPLOY_JOB_STATE'] -eq 'REJECTED') { throw 'DEPLOY BLOCKED: the job was rejected (see DEPLOY_JOB lines above). Nothing was changed.' }
    if ($f['DEPLOY_JOB_ID'] -eq $JobId) { $started = $f } else { Start-Sleep -Seconds ([Math]::Min(5, [Math]::Max(0, $PollSeconds))) }
  }
  if (-not $started) { throw "DEPLOY RESULT UNKNOWN: could not confirm that job $JobId started. Rerun with -ResumeJobId $JobId to check it (this never starts a duplicate)." }
}

Write-Host '3/4 Waiting for the job (safe to lose the connection; polling reconnects)...'
$deadline = (Get-Date).AddMinutes($MaxMinutes)
$recoverRequested = $false
$final = $null
$lastLog = ''
while (-not $final) {
  if ((Get-Date) -gt $deadline) { throw "DEPLOY STILL RUNNING after $MaxMinutes minutes. Nothing is stopped by this client; resume later with -ResumeJobId $JobId" }
  $status = Invoke-Remote "bash $jobScript status $JobId" -Quiet
  $f = Get-Fields $status
  $log = @($status.Lines | Where-Object { $_ -like 'LOG: *' }) | Select-Object -Last 1
  if ($log -and $log -ne $lastLog) { Write-Host $log; $lastLog = $log }
  $state = $f['DEPLOY_JOB_STATE']
  if (-not $state) { Write-Host '    (connection lost; reconnecting...)' -ForegroundColor DarkYellow; $state = 'NO_ANSWER' }
  switch ($state) {
    'FINISHED' { $final = $f }
    'DIED' {
      if ($recoverRequested) { throw "DEPLOY STOPPED: job $JobId died again during recovery. MANUAL RECOVERY REQUIRED; see the job log: bash $jobScript status $JobId" }
      Write-Host 'The server job was interrupted; asking Oracle to recover it under the deployment lock...' -ForegroundColor Yellow
      [void](Invoke-Remote "bash $jobScript recover $JobId")
      $recoverRequested = $true
    }
    'UNKNOWN' { throw "DEPLOY BLOCKED: Oracle has no job $JobId." }
  }
  if (-not $final) { Start-Sleep -Seconds $PollSeconds }
}

$result = $final['DEPLOY_RESULT']
switch ($result) {
  'SUCCESS' { Write-Host "Verified on Oracle: build $Sha, same artifact/model/threshold/journal, 8765/8767 unchanged, trading disabled." -ForegroundColor Green }
  'FAILED_PREVIOUS_STILL_RUNNING' { throw 'DEPLOY FAILED: the install did not complete; preview 8768 is confirmed healthy on the previous build. Nothing else changed.' }
  'ROLLED_BACK' { throw 'DEPLOY ROLLED BACK: preview 8768 was restored to the exact previous process (same artifact, journal and environment).' }
  'BLOCKED_CONCURRENT_DEPLOYMENT' { throw 'DEPLOY BLOCKED: another preview 8768 deployment held the lock. Nothing was changed.' }
  'BLOCKED' { throw 'DEPLOY BLOCKED: preconditions failed. Nothing was changed.' }
  'INTERRUPTED_BEFORE_CHANGE' { throw 'DEPLOY INTERRUPTED before any change; preview 8768 was not modified.' }
  'MANUAL_RECOVERY_REQUIRED' { throw "DEPLOY STOPPED: MANUAL RECOVERY REQUIRED. Automatic recovery was not safe; the exact steps are in the job log: bash $jobScript status $JobId" }
  default { throw "DEPLOY RESULT UNKNOWN ($result). Check: bash $jobScript status $JobId" }
}

function Invoke-ClientRollback([string]$Reason) {
  Write-Host "LOCAL VERIFICATION FAILED: $Reason - asking Oracle for the guarded rollback of job $JobId..." -ForegroundColor Red
  [void](Invoke-Remote "bash $jobScript rollback $JobId")
  $end = (Get-Date).AddMinutes(10)
  while ((Get-Date) -lt $end) {
    Start-Sleep -Seconds $PollSeconds
    $f = Get-Fields (Invoke-Remote "bash $jobScript status $JobId" -Quiet)
    if ($f['DEPLOY_JOB_STATE'] -eq 'FINISHED' -and $f['DEPLOY_RESULT'] -ne 'SUCCESS') {
      if ($f['DEPLOY_RESULT'] -eq 'ROLLED_BACK') { throw "DEPLOY ROLLED BACK after local verification failed: $Reason" }
      throw "DEPLOY STOPPED: rollback result $($f['DEPLOY_RESULT']). MANUAL RECOVERY REQUIRED; see: bash $jobScript status $JobId"
    }
  }
  throw "DEPLOY ROLLBACK STILL RUNNING; check: bash $jobScript status $JobId"
}

if ($SkipTunnel) { return 'SUCCESS' }
Write-Host '4/4 Opening a private local tunnel to the dashboard...'
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
