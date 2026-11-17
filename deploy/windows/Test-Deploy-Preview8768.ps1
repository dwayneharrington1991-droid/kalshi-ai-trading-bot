# Windows PowerShell 5.1 parse and simulation tests for Deploy-Preview8768.ps1.
# Run on Windows:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy\windows\Test-Deploy-Preview8768.ps1
# Uses a mock ssh.exe (a PowerShell function); nothing connects to Oracle.
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'Deploy-Preview8768.ps1'
$failures = 0
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
  if ($Ok) { Write-Host "PASS  $Name" -ForegroundColor Green }
  else { $script:failures++; Write-Host "FAIL  $Name $Detail" -ForegroundColor Red }
}
Write-Host ("PowerShell {0} ({1})" -f $PSVersionTable.PSVersion, $PSVersionTable.PSEdition)
Check 'running under Windows PowerShell 5.1' ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -eq 1)

# Parse: the script file and the pasted "& { ... }" command form.
$tokens = $null; $errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors)
Check 'script parses with no errors' ($errors.Count -eq 0) ($errors | Out-String)
$pasted = '& {' + [Environment]::NewLine + (Get-Content -Raw -LiteralPath $script) + [Environment]::NewLine + '}'
[void][System.Management.Automation.Language.Parser]::ParseInput($pasted, [ref]$tokens, [ref]$errors)
Check 'pasted command form parses with no errors' ($errors.Count -eq 0) ($errors | Out-String)

# Mock ssh.exe: decodes the remote script and answers from per-kind queues.
$Sha = 'a' * 40
function global:Mock-Ssh {
  $remote = [string]$args[-1]
  $b64 = ($remote -replace '^echo ', '') -replace ' \| base64 -d \| bash$', ''
  $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
  $kind = if ($text -match 'SSH_PREFLIGHT_OK') { 'preflight' }
          elseif ($text -match 'deploy_job_8768\.sh" start') { 'start' }
          elseif ($text -match 'deploy_job_8768\.sh status ') { 'status' }
          elseif ($text -match 'deploy_job_8768\.sh recover ') { 'recover' }
          elseif ($text -match 'deploy_job_8768\.sh rollback ') { 'rollback' }
          else { 'other' }
  $global:MockCalls += $kind
  if ($kind -eq 'start' -and $text -match 'start "\$SHA" ([0-9a-f-]{36})') { $global:MockRequestIds += $Matches[1] }
  $queue = $global:MockReplies[$kind]
  $index = [Math]::Min($global:MockIndex[$kind], $queue.Count - 1)
  $global:MockIndex[$kind] = $global:MockIndex[$kind] + 1
  $reply = $queue[$index]
  foreach ($line in $reply.Lines) {
    # 'DEPLOY_JOB_ID=ECHO' stands for the request id this client just sent.
    if ($line -eq 'DEPLOY_JOB_ID=ECHO' -and $global:MockRequestIds.Count) { 'DEPLOY_JOB_ID=' + $global:MockRequestIds[-1] } else { $line }
  }
  $global:LASTEXITCODE = $reply.Exit
}
function Invoke-Scenario($Replies, [hashtable]$Extra = @{}) {
  $global:MockReplies = $Replies
  $global:MockIndex = @{ preflight = 0; start = 0; status = 0; recover = 0; rollback = 0; other = 0 }
  $global:MockCalls = @()
  $global:MockRequestIds = @()
  $env:ORACLE_SSH_KEY = 'C:\nonexistent\test-key'
  try {
    $params = @{ Sha = $Sha; SshExe = 'Mock-Ssh'; PollSeconds = 0 } + $Extra
    if (-not $params.ContainsKey('TestTunnelHealth')) { $params.SkipTunnel = $true }
    $out = & $script @params 6>$null
    return [pscustomobject]@{ Output = $out; Error = $null; Calls = $global:MockCalls; Ids = $global:MockRequestIds }
  } catch {
    return [pscustomobject]@{ Output = $null; Error = $_.Exception.Message; Calls = $global:MockCalls; Ids = $global:MockRequestIds }
  }
}
$ok = @(@{ Lines = @('SSH_PREFLIGHT_OK'); Exit = 0 })
function Finished($Result) { @{ Lines = @('DEPLOY_JOB_STATE=FINISHED', "DEPLOY_RESULT=$Result", 'LOG: DEPLOY: done'); Exit = 0 } }
$running = @{ Lines = @('DEPLOY_JOB_STATE=RUNNING', 'LOG: DEPLOY: installing'); Exit = 0 }
$lost = @{ Lines = @('Connection reset by peer'); Exit = 255 }

function Run-Job($StatusQueue, $Extra = @{}, $Recover = @(@{ Lines = @('DEPLOY_JOB_STATE=RUNNING'); Exit = 0 }), $Rollback = @(@{ Lines = @('DEPLOY_JOB_STATE=RUNNING'); Exit = 0 }), $Preflight = $ok, $Start = $null) {
  if (-not $Start) { $Start = @(@{ Lines = @('DEPLOY_JOB_ID=ECHO', 'DEPLOY_JOB_STATE=RUNNING'); Exit = 0 }) }
  Invoke-Scenario @{ preflight = $Preflight; start = $Start; status = $StatusQueue; recover = $Recover; rollback = $Rollback } $Extra
}

$r = Run-Job @($running, (Finished 'SUCCESS'))
Check 'successful installation returns SUCCESS' ($r.Output -contains 'SUCCESS' -and -not $r.Error) $r.Error

$r = Run-Job @((Finished 'FAILED_PREVIOUS_STILL_RUNNING'))
Check 'failed installation reports previous build healthy' ($r.Error -like 'DEPLOY FAILED:*previous build*') $r.Error

$r = Run-Job @($running, (Finished 'ROLLED_BACK'))
Check 'automatic rollback is reported' ($r.Error -like 'DEPLOY ROLLED BACK:*') $r.Error

$r = Run-Job @((Finished 'MANUAL_RECOVERY_REQUIRED'))
Check 'incorrect process identity stops with manual recovery' ($r.Error -like '*MANUAL RECOVERY REQUIRED*') $r.Error

$r = Run-Job @($running) @{} -Start @(@{ Lines = @('DEPLOY_JOB_STATE=BUSY', 'DEPLOY_ACTIVE_JOB=1111-2222'); Exit = 0 })
Check 'concurrent deployment is rejected without starting' ($r.Error -like 'DEPLOY BLOCKED: another preview 8768 deployment job*' -and ($r.Calls -notcontains 'status')) $r.Error

$r = Run-Job @($running) @{} -Preflight @(@{ Lines = @('opc@129.146.120.195: Permission denied (publickey).'); Exit = 255 })
Check 'missing SSH credentials block before any job starts' ($r.Error -like 'DEPLOY BLOCKED: SSH connection or authentication*' -and ($r.Calls -notcontains 'start')) $r.Error

$r = Run-Job @($running, (Finished 'ROLLED_BACK'))
Check 'failed post-install verification is rolled back' ($r.Error -like 'DEPLOY ROLLED BACK:*') $r.Error

$r = Run-Job @($running, $lost, $lost, $running, (Finished 'SUCCESS'))
Check 'SSH disconnect during polling reconnects and never restarts' ($r.Output -contains 'SUCCESS' -and @($r.Calls | Where-Object { $_ -eq 'start' }).Count -eq 1) $r.Error

$r = Run-Job @(@{ Lines = @('DEPLOY_JOB_STATE=DIED'); Exit = 0 }, $running, (Finished 'ROLLED_BACK'))
Check 'a died job is recovered by the server once' ($r.Error -like 'DEPLOY ROLLED BACK:*' -and @($r.Calls | Where-Object { $_ -eq 'recover' }).Count -eq 1) $r.Error

$r = Run-Job @($running, (Finished 'SUCCESS')) @{ ResumeJobId = '12345678-aaaa-bbbb-cccc-1234567890ab' }
Check 'resuming by job id never starts a new job' ($r.Output -contains 'SUCCESS' -and ($r.Calls -notcontains 'start')) $r.Error

$r = Run-Job @($running, (Finished 'SUCCESS')) @{} -Start @($lost, @{ Lines = @('DEPLOY_JOB_ID=ECHO', 'DEPLOY_JOB_STATE=RUNNING'); Exit = 0 })
Check 'a lost start is retried with the SAME request id' ($r.Output -contains 'SUCCESS' -and $r.Ids.Count -eq 2 -and $r.Ids[0] -eq $r.Ids[1]) $r.Error

$r = Run-Job @((Finished 'SUCCESS'), (Finished 'ROLLED_BACK')) @{ TestTunnelHealth = { [pscustomobject]@{ build_commit = ('b' * 40) } } }
Check 'wrong build through the tunnel triggers the server-side rollback job' ($r.Error -like 'DEPLOY ROLLED BACK after local verification failed*' -and ($r.Calls -contains 'rollback')) $r.Error

Remove-Item Env:ORACLE_SSH_KEY -ErrorAction SilentlyContinue
if ($failures) { Write-Host "$failures check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'ALL CHECKS PASSED' -ForegroundColor Green
