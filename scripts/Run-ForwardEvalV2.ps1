# Score pre-registered forward test v2 (research only; read-only on Oracle except one temp file in /tmp).
# Needs the static export of the live model's training-period journal at research\old_slim.json
# (2026-10-30 .. 11-12; it never changes). The current journal is exported with a chunked, verified download.
param(
    [string]$Root = "$env:USERPROFILE\kalshi-bot-dev",
    [string]$Key = "$env:USERPROFILE\.ssh\oracle-codespace-deploy",
    [string]$Server = "opc@129.146.120.195"
)
$ErrorActionPreference = 'Stop'
$FreezeCommit = 'cc1e621'
$ArtifactRemote = '/home/opc/.kalshi-deploy/preview-8768/models/qualified-20261116T210122Z.json'
$Repo = Join-Path $Root 'repo'
$Py = Join-Path $Root 'venv\Scripts\python.exe'
$Old = Join-Path $Root 'research\old_slim.json'
if (-not (Test-Path $Old)) { throw "Missing $Old (static export of the 2026-10-30..11-12 journal)." }
git -C $Repo diff --quiet $FreezeCommit -- scripts/forward_eval_v2.py scripts/research_opening_v2.py
if ($LASTEXITCODE -ne 0) { throw "The v2 evaluator differs from $FreezeCommit; refusing to score." }
$Out = Join-Path $Root ('forward-v2\' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Force $Out | Out-Null
. (Join-Path $Repo 'scripts\ForwardEvalCommon.ps1')
$dump = Join-Path $Out 'journal_dump.json'
Get-RemoteExport -Key $Key -Server $Server -RemotePython '/home/opc/kalshi-ai-trading-bot/.venv/bin/python' `
    -ScriptFile "$Repo\scripts\export_opening_research.py" -Arguments '' -Destination $dump
$art = Join-Path $Out 'live_artifact.json'
Invoke-Ssh -Key $Key -Server $Server -Command "cat $ArtifactRemote" -OutFile $art | Out-Null
$env:PYTHONPATH = $Repo
Push-Location $Repo
try { $report = & $Py scripts\forward_eval_v2.py --old $Old --new $dump --artifact $art } finally { Pop-Location }
$report | Set-Content -Encoding utf8 (Join-Path $Out 'report.json')
$report
Write-Host "Saved in $Out"
