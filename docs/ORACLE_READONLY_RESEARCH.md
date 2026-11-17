# Read-only opening research on Oracle (Windows PowerShell 5.1)

One pasted command measures the real opening model on the real preview
journal without changing anything on the server:

1. Fetches `claude/phase4-opening-research`, verifies the exact commit, and extracts
   it into a temporary directory with `git archive` (no service checkout is
   touched).
2. Runs `deploy/oracle/research_readonly.sh`, which reports the model the live
   preview 8768 process actually uses (artifact path, checksum, kind, decision
   threshold, its own validation/test evidence, the current live opening) and
   runs `opening_research` against the preview journal opened read-only, at
   low CPU priority with a 3-hour limit.
3. Deletes the temporary code, copies the report folder to
   `%USERPROFILE%\kalshi-research\`, and prints where it is.

No process is stopped, started or signalled; journals, artifacts, registries
and ports 8765/8767/8768 are not modified; trading stays disabled.

Resumable: the run folder `/home/opc/kalshi-research/phase4-run` pins the
as-of time in `manifest.json` and keeps completed folds in `checkpoints/`.
If the time limit is reached, rerun the same command; finished work is reused.
Delete that folder (research output only) to start a fresh run on newer data.

```powershell
& {
$ErrorActionPreference = 'Stop'
$Sha = '__FINAL_SHA__'
$Server = 'opc@129.146.120.195'
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$Key = $env:ORACLE_SSH_KEY
if (-not $Key) {
  $Key = Get-ChildItem -LiteralPath $sshDir -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like '*oracle*' -or $_.Name -like 'ssh-key-*.key') -and $_.Extension -notin '.pub', '.ppk' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$sshBase = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) { throw 'OpenSSH client (ssh.exe) is not installed.' }
$bootstrap = @'
set -Eeuo pipefail
SHA=__SHA__
BRANCH=claude/phase4-opening-research
REPO=/home/opc/kalshi-ai-trading-bot
[[ $(id -un) == opc ]] || { echo "BLOCKED: must run as opc" >&2; exit 1; }
git -C "$REPO" fetch --quiet --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || { echo "BLOCKED: could not fetch $BRANCH; nothing changed" >&2; exit 1; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "BLOCKED: commit $SHA was not fetched; nothing changed" >&2; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH" || { echo "BLOCKED: $SHA is not on $BRANCH" >&2; exit 1; }
code=$(mktemp -d /home/opc/.kalshi-research-code.XXXXXX)
trap 'rm -rf "$code"' EXIT
git -C "$REPO" archive "$SHA" | tar -x -C "$code"
bash "$code/deploy/oracle/research_readonly.sh"
'@
$bootstrap = $bootstrap.Replace('__SHA__', $Sha) -replace "`r", ''
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
Write-Host "Running read-only opening research for $Sha (no service, port, journal or artifact is changed)..."
$ErrorActionPreference = 'Continue'
& ssh.exe @sshBase $Server "echo $encoded | base64 -d | bash" | Tee-Object -Variable captured | Out-Host
$remoteExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$reportDir = ($captured | Where-Object { $_ -like 'RESEARCH: REPORT_DIR=*' } | Select-Object -Last 1) -replace '^RESEARCH: REPORT_DIR=', ''
if ($reportDir) {
  $local = Join-Path $env:USERPROFILE 'kalshi-research'
  New-Item -ItemType Directory -Force -Path $local | Out-Null
  $scpBase = @('-o', 'StrictHostKeyChecking=accept-new')
  if ($Key) { $scpBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key) }
  $ErrorActionPreference = 'Continue'
  & scp.exe @scpBase -r "${Server}:$reportDir" $local
  $ErrorActionPreference = 'Stop'
  Write-Host "Report copied to $local\$(Split-Path $reportDir -Leaf)"
}
if ($remoteExit -eq 124) { Write-Host 'Time limit reached. Completed work is saved on the server: run this same command again to resume.' -ForegroundColor Yellow }
elseif ($remoteExit -ne 0) { throw "Research did not complete (exit $remoteExit). Nothing on the server was changed." }
else { Write-Host 'Reports A and B complete.' -ForegroundColor Green }
}
```
