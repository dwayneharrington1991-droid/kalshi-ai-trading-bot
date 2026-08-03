$ErrorActionPreference = "Stop"
$pidPath = Join-Path $env:TEMP "kalshi-overnight-canary.pid"
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No overnight canary PID file exists."
    exit 0
}
$canaryPid = [int](Get-Content -LiteralPath $pidPath -Raw)
$process = Get-Process -Id $canaryPid -ErrorAction SilentlyContinue
if ($null -ne $process) {
    Stop-Process -Id $canaryPid
    Wait-Process -Id $canaryPid -ErrorAction SilentlyContinue
    Write-Host "Stopped overnight canary PID=$canaryPid"
} else {
    Write-Host "Overnight canary PID=$canaryPid was not running."
}
Remove-Item -LiteralPath $pidPath -Force
