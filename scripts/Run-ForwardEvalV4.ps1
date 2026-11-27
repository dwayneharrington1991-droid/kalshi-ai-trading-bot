# Score pre-registered forward test v4 (research only; read-only on Oracle except one temp file in /tmp).
# See docs/preregistration-v4-20261122.md. Counts openings from 2026-11-22 03:30 UTC; decides at >= 200.
param(
    [string]$Root = "$env:USERPROFILE\kalshi-bot-dev",
    [string]$Key = "$env:USERPROFILE\.ssh\oracle-codespace-deploy",
    [string]$Server = "opc@129.146.120.195"
)
$ErrorActionPreference = 'Stop'
$FreezeCommit = '2798cef'
$ArtifactRemote = '/home/opc/.kalshi-deploy/preview-8768/models/qualified-20261116T210122Z.json'
$Repo = Join-Path $Root 'repo'
$Py = Join-Path $Root 'venv\Scripts\python.exe'
git -C $Repo diff --quiet $FreezeCommit -- scripts/forward_eval_v4.py src/signals_display/feature_alignment.py research_artifacts/feature_alignment_20261122.json
if ($LASTEXITCODE -ne 0) { throw "The v4 evaluator or alignment differs from $FreezeCommit; refusing to score." }
$Out = Join-Path $Root ('forward-v4\' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Force $Out | Out-Null
. (Join-Path $Repo 'scripts\ForwardEvalCommon.ps1')
$dump = Join-Path $Out 'journal_dump.json'
Get-RemoteExport -Key $Key -Server $Server -RemotePython '/home/opc/kalshi-ai-trading-bot/.venv/bin/python' `
    -ScriptFile "$Repo\scripts\export_opening_research.py" -Arguments '' -Destination $dump
$art = Join-Path $Out 'live_artifact.json'
Invoke-Ssh -Key $Key -Server $Server -Command "cat $ArtifactRemote" -OutFile $art | Out-Null
$env:PYTHONPATH = $Repo
Push-Location $Repo
try { $report = & $Py scripts\forward_eval_v4.py --journal $dump --artifact $art } finally { Pop-Location }
$report | Set-Content -Encoding utf8 (Join-Path $Out 'report.json')
$report
Write-Host "Saved in $Out"
