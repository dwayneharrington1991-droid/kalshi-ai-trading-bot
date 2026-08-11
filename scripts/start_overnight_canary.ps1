param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("I_APPROVE_OVERNIGHT_CANARY")]
    [string]$Approval,
    [ValidateSet("demo", "production")]
    [string]$Environment = "demo"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Project virtual environment is unavailable" }
if ([string]::IsNullOrWhiteSpace($env:KALSHI_API_KEY) -or
    [string]::IsNullOrWhiteSpace($env:KALSHI_PRIVATE_KEY_PATH)) {
    throw "Kalshi credentials are UNSET in this PowerShell process"
}

$env:KALSHI_ENVIRONMENT = $Environment
$env:LIVE_TRADING_ENABLED = "true"
$env:AUTHORITATIVE_LIVE_EXECUTION_ENABLED = "true"
$env:ORDER_RECONCILIATION_ENABLED = "true"
$env:RECONCILIATION_SHADOW_MODE = "true"
$env:RECONCILIATION_STARTUP_REQUIRED = "true"
$env:LIVE_ORDER_SUBMISSION_KILL_SWITCH = "false"
$env:OVERNIGHT_CANARY_ENABLED = "true"
$env:OVERNIGHT_CANARY_MAX_TOTAL_RISK = "20"
$env:OVERNIGHT_CANARY_MAX_MARKET_RISK = "5"
$env:OVERNIGHT_CANARY_MAX_POSITIONS = "5"
$env:OVERNIGHT_CANARY_MAX_REJECTIONS = "3"
$env:OVERNIGHT_CANARY_MAX_DAILY_LOSS = "5"
$env:OVERNIGHT_CANARY_MARKET_DATA_MAX_AGE_SECONDS = "120"
$env:ALLOW_RISK_REDUCING_LIVE_EXITS = "true"
$env:ALLOW_LIVE_ORDER_CANCELLATIONS = "false"
$env:PRODUCTION_EXECUTION_ACKNOWLEDGEMENT = if ($Environment -eq "production") {
    "I_ACKNOWLEDGE_PRODUCTION_ORDER_RISK"
} else { "" }

$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDir "overnight-canary-$stamp.log"
$errorPath = Join-Path $logDir "overnight-canary-$stamp.error.log"
$process = Start-Process -FilePath $python -ArgumentList @("cli.py", "run", "--live") `
    -WorkingDirectory $repoRoot -RedirectStandardOutput $logPath `
    -RedirectStandardError $errorPath -WindowStyle Hidden -PassThru
$pidPath = Join-Path $env:TEMP "kalshi-overnight-canary.pid"
Set-Content -LiteralPath $pidPath -Value $process.Id -NoNewline
Write-Host "Overnight canary started. PID=$($process.Id)"
Write-Host "Log=$logPath"
Write-Host "Stop with: .\scripts\stop_overnight_canary.ps1"
