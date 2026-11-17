# Windows PowerShell 5.1 parse and simulation tests for Deploy-Preview8768.ps1.
# Run on Windows:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy\windows\Test-Deploy-Preview8768.ps1
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

# 1. Parse: the script file and the pasted "& { ... }" command form.
$tokens = $null; $errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors)
Check 'script parses with no errors' ($errors.Count -eq 0) ($errors | Out-String)
$pasted = '& {' + [Environment]::NewLine + (Get-Content -Raw -LiteralPath $script) + [Environment]::NewLine + '}'
[void][System.Management.Automation.Language.Parser]::ParseInput($pasted, [ref]$tokens, [ref]$errors)
Check 'pasted command form parses with no errors' ($errors.Count -eq 0) ($errors | Out-String)

# 2. Mock ssh.exe: decodes the remote script to see what was requested.
$Sha = 'a' * 40
$global:MockCalls = @()
function global:Mock-Ssh {
  $remote = [string]$args[-1]
  $b64 = ($remote -replace '^echo ', '') -replace ' \| base64 -d \| bash$', ''
  $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
  $kind = if ($text -match 'SSH_PREFLIGHT_OK') { 'preflight' } elseif ($text -match 'deploy_preview_8768\.sh rollback') { 'rollback' } else { 'deploy' }
  $global:MockCalls += $kind
  $reply = $global:MockReplies[$kind]
  foreach ($line in $reply.Lines) { $line }
  $global:LASTEXITCODE = $reply.Exit
}
function Invoke-Scenario($Replies, [scriptblock]$Tunnel = $null) {
  $global:MockReplies = $Replies
  $global:MockCalls = @()
  $env:ORACLE_SSH_KEY = 'C:\nonexistent\test-key'
  try {
    $params = @{ Sha = $Sha; SshExe = 'Mock-Ssh' }
    if ($Tunnel) { $params.TestTunnelHealth = $Tunnel } else { $params.SkipTunnel = $true }
    $out = & $script @params 6>$null
    return [pscustomobject]@{ Output = $out; Error = $null; Calls = $global:MockCalls }
  } catch {
    return [pscustomobject]@{ Output = $null; Error = $_.Exception.Message; Calls = $global:MockCalls }
  }
}
$ok = @{ Lines = @('SSH_PREFLIGHT_OK'); Exit = 0 }
function Deploy($Result, $Exit, $Extra = @()) { @{ Lines = @('DEPLOY_RUN_ID=20261117T000000Z-1') + $Extra + @("DEPLOY_RESULT=$Result"); Exit = $Exit } }

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'SUCCESS' 0) }
Check 'successful installation returns SUCCESS' ($r.Output -contains 'SUCCESS' -and -not $r.Error) $r.Error

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'FAILED_PREVIOUS_STILL_RUNNING' 10) }
Check 'failed installation reports previous build healthy' ($r.Error -like 'DEPLOY FAILED:*previous build*')

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'ROLLED_BACK' 11 @('DEPLOY: guarded recovery: installer failed')) }
Check 'automatic rollback is reported as rolled back' ($r.Error -like 'DEPLOY ROLLED BACK:*')

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'MANUAL_RECOVERY_REQUIRED' 12 @('DEPLOY_MANUAL: port 8768 is held by an unknown process')) }
Check 'incorrect process identity stops with manual recovery' ($r.Error -like '*MANUAL RECOVERY REQUIRED*')

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'BLOCKED_CONCURRENT_DEPLOYMENT' 75) }
Check 'concurrent deployment is rejected' ($r.Error -like 'DEPLOY BLOCKED: another preview 8768 deployment*')

$r = Invoke-Scenario @{ preflight = @{ Lines = @('opc@129.146.120.195: Permission denied (publickey).'); Exit = 255 }; deploy = (Deploy 'SUCCESS' 0) }
Check 'missing SSH credentials block before any deployment' ($r.Error -like 'DEPLOY BLOCKED: SSH connection or authentication*' -and ($r.Calls -notcontains 'deploy'))

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'ROLLED_BACK' 11 @('DEPLOY: guarded recovery: post-install verification failed: threshold changed')) }
Check 'failed post-install verification is rolled back' ($r.Error -like 'DEPLOY ROLLED BACK:*')

$r = Invoke-Scenario @{ preflight = $ok; deploy = (Deploy 'SUCCESS' 0); rollback = @{ Lines = @('DEPLOY_RESULT=ROLLED_BACK'); Exit = 11 } } { [pscustomobject]@{ build_commit = 'b' * 40 } }
Check 'wrong build through the tunnel triggers the guarded rollback' ($r.Error -like 'DEPLOY ROLLED BACK after local verification failed*' -and ($r.Calls -contains 'rollback'))

$r = Invoke-Scenario @{ preflight = $ok; deploy = @{ Lines = @('Connection reset'); Exit = 255 } }
Check 'lost connection reports an unknown result and does not retry' ($r.Error -like 'DEPLOY RESULT UNKNOWN*' -and ($r.Calls -notcontains 'rollback'))

Remove-Item Env:ORACLE_SSH_KEY -ErrorAction SilentlyContinue
if ($failures) { Write-Host "$failures check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host 'ALL CHECKS PASSED' -ForegroundColor Green
