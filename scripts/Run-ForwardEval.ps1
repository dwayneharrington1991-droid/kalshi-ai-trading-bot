# Score the pre-registered opening forward test (research only; read-only on Oracle).
# 1) checks the frozen evaluator is byte-identical to the freeze commit c08fb8c
# 2) exports the three journal tables read-only over SSH (SQLite mode=ro, lowest priority)
# 3) copies the live opening artifact (read-only) and checks its sha256
# 4) prints the frozen report and saves it next to the export
# Nothing on Oracle is changed; no model, threshold, journal, alert or process is touched.
param(
    [string]$Root = "$env:USERPROFILE\kalshi-bot-dev",
    [string]$Key = "$env:USERPROFILE\.ssh\oracle-codespace-deploy",
    [string]$Server = "opc@129.146.120.195"
)
$ErrorActionPreference = 'Stop'
$FreezeCommit = 'c08fb8c'
$ArtifactRemote = '/home/opc/.kalshi-deploy/preview-8768/models/qualified-20261116T210122Z.json'
$ArtifactSha = '934D490A56085F7A2D9A3D679A2A4E7B90E2EAE6989C1625DF82AF170811302B'
$Repo = Join-Path $Root 'repo'
$Py = Join-Path $Root 'venv\Scripts\python.exe'
$Out = Join-Path $Root ('forward\' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Force $Out | Out-Null

git -C $Repo diff --quiet $FreezeCommit -- scripts/forward_eval_openings.py
if ($LASTEXITCODE -ne 0) { throw "The frozen evaluator differs from $FreezeCommit; refusing to score (a changed test is a new experiment)." }

$dump = Join-Path $Out 'journal_dump.json'
$art = Join-Path $Out 'live_artifact.json'
. (Join-Path $Repo 'scripts\ForwardEvalCommon.ps1')
# Compressed, chunked and verified download (long single transfers from this PC were being cut off).
Get-RemoteExport -Key $Key -Server $Server -RemotePython '/home/opc/kalshi-ai-trading-bot/.venv/bin/python' `
    -ScriptFile "$Repo\scripts\export_opening_research.py" -Arguments '' -Destination $dump
cmd /c "ssh -i `"$Key`" -o BatchMode=yes $Server `"cat $ArtifactRemote`" > `"$art`" 2>nul"
if ((Get-FileHash $art).Hash -ne $ArtifactSha) {
    Write-Host "WARNING: the live artifact changed since the freeze; the comparison is against a different live model." -ForegroundColor Yellow
}

$env:PYTHONPATH = $Repo
Push-Location $Repo
try {
    $report = & $Py scripts\forward_eval_openings.py --export $dump --artifact $art
    if ($LASTEXITCODE -ne 0) { throw "Evaluator failed (exit $LASTEXITCODE)." }
} finally { Pop-Location }
$report | Set-Content -Encoding utf8 (Join-Path $Out 'report.json')
$report
$r = $report | Out-String | ConvertFrom-Json
Write-Host ("Forward openings: {0} of {1} required. Decision: {2}. Passed: {3}" -f $r.forward_openings, $r.minimum_for_decision, $r.decision, (($r.passed_preregistered_rule) -join ', ')) -ForegroundColor Cyan
Write-Host "Saved in $Out"
