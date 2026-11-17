# Phase 5 deployment command: safety-reviewed (author review, not independent)

Deploys `95bb941d7fe51ad2ed5056d00e8fe45d697580eb` (Claude fixes merged with
Codex `cf61cac`) to **preview 8768 only**. Do not run until approved.

## Safety properties

| Requirement | How it is met |
|---|---|
| Failed verification rolls back | Remote post-checks: the installer restores the exact previous process. Server-side verification by this command (build, health, signals-only, execution disabled, alerts endpoint, same artifact sha256/model/threshold/components/inputs/journal, 8765/8767 unchanged) triggers a guarded rollback. A wrong build seen through this run's own tunnel also triggers it. |
| Rollback preserves artifact, configuration and journal | It runs the switch's persisted `rollback.sh`, rewritten at every switch: the exact previous argv (same `--opening-model-artifact`, `--journal` and flags) and previous `PYTHONPATH`. It then waits for the previous build to answer. |
| Rollback cannot hit the wrong process | It refuses unless the 8768 listener is the exact PID this install started AND its argv carries the new commit. It refuses if this run did not replace the process. |
| 8765/8767 never affected | Only the 8768 listener is ever stopped. The installer, switch and this command all compare the 8765/8767 PIDs before and after. |
| Tunnel safety | It picks a free local port (18768-18799), keeps the `ssh.exe` handle, closes only that tunnel on failure, and never reuses an unknown listener. A tunnel that cannot connect is reported as a local issue (the server-side check already passed); no rollback. |
| Opening window | Planned switch: waits outside 90 s before to 150 s after each quarter hour. Rollback is emergency recovery and is never delayed. |

Simulated locally against fake services (a wrong PID or wrong build is
refused; the correct process rolls back to the old build; 8765/8767 untouched).
The probe reports the artifact identity. All embedded scripts pass `bash -n`;
bootstrap staging was dry-run. PowerShell itself was not executed.

```powershell
& {
$ErrorActionPreference = 'Stop'
$Sha = '95bb941d7fe51ad2ed5056d00e8fe45d697580eb'
$Server = 'opc@129.146.120.195'
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$Key = $env:ORACLE_SSH_KEY
if (-not $Key) {
  $Key = Get-ChildItem -LiteralPath $sshDir -File -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like '*oracle*' -or $_.Name -like 'ssh-key-*.key') -and $_.Extension -notin '.pub', '.ppk' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
$sshBase = @('-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8')
if ($Key) { $sshBase += @('-o', 'IdentitiesOnly=yes', '-i', $Key); Write-Host "Using SSH key: $Key" }
else { Write-Host 'No key file found in ~/.ssh; using your SSH config/agent defaults.' }
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) { throw 'OpenSSH client (ssh.exe) is not installed.' }

# Runs one bash script on Oracle; streams and returns its output lines.
function Invoke-Remote([string]$Script) {
  $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Script -replace "`r", '')))
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $lines = @(& ssh.exe @sshBase $Server "echo $encoded | base64 -d | bash" 2>&1 | ForEach-Object { $s = "$_"; Write-Host $s; $s })
  $code = $LASTEXITCODE
  $ErrorActionPreference = $previous
  [pscustomobject]@{ Exit = $code; Lines = $lines }
}
function Get-RemoteState {
  $result = Invoke-Remote $probe
  $line = $result.Lines | Where-Object { $_ -like 'STATE *' } | Select-Object -Last 1
  if (-not $line) { return $null }
  return ($line.Substring(6) | ConvertFrom-Json)
}

# Read-only probe: listeners, build, artifact identity, journal, endpoints.
$probe = @'
set -Euo pipefail
PY=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
listener() {
  local l
  l=$(sudo -n ss -H -ltnp "sport = :$1" 2>/dev/null || ss -H -ltnp "sport = :$1" 2>/dev/null || true)
  sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$l" | sort -u | head -1
}
argv_value() {
  local pid=$1 flag=$2 i
  local -a a=()
  mapfile -d '' -t a <"/proc/$pid/cmdline" || return 0
  for ((i=0; i<${#a[@]}-1; i++)); do
    if [[ ${a[i]} == "$flag" ]]; then printf '%s' "${a[i+1]}"; return 0; fi
  done
}
p68=$(listener 8768); p65=$(listener 8765); p67=$(listener 8767)
art=; jr=
if [[ -n $p68 ]]; then art=$(argv_value "$p68" --opening-model-artifact); jr=$(argv_value "$p68" --journal); fi
"$PY" -P - "$p68" "$p65" "$p67" "$art" "$jr" <<'PY'
import hashlib, json, sys
from urllib.request import ProxyHandler, build_opener
p68, p65, p67, art, jr = sys.argv[1:6]
opener = build_opener(ProxyHandler({}))
def get(path):
    try:
        with opener.open("http://127.0.0.1:8768" + path, timeout=8) as r:
            return json.load(r)
    except Exception as exc:
        return {"_error": type(exc).__name__}
health, state, alerts = get("/healthz"), get("/api/state?view=main"), get("/api/data-quality-alerts")
info = {"pid8768": p68, "pid8765": p65, "pid8767": p67, "artifact": art, "journal": jr,
        "build": health.get("build_commit"), "health": health.get("status"),
        "signals_only": health.get("signals_only"),
        "execution_capability": (state.get("decision_intelligence") or {}).get("execution_capability"),
        "alerts_endpoint": alerts.get("read_only") is True, "alerts_status": alerts.get("status")}
if art:
    try:
        data = open(art, "rb").read()
        artifact = json.loads(data)
        model = artifact.get("model") or {}
        parts = model.get("components") or [model]
        info.update(artifact_sha256=hashlib.sha256(data).hexdigest(), model_id=artifact.get("model_id"),
                    threshold=(artifact.get("decision_policy") or {}).get("threshold"),
                    components=len(parts),
                    inputs=len({n for p in parts for n in ((p.get("preprocessor") or {}).get("names") or [])}))
    except Exception as exc:
        info["artifact_error"] = type(exc).__name__
print("STATE " + json.dumps(info, sort_keys=True))
PY
'@

# Guarded rollback: stops ONLY the process this install started (pid and
# commit verified), runs the switch's persisted rollback.sh (exact previous
# argv: same artifact, journal and flags; previous PYTHONPATH), then waits for
# the PREVIOUS build to answer. Never touches 8765 or 8767.
$rollbackTemplate = @'
set -Euo pipefail
STATE=/home/opc/.kalshi-deploy/preview-8768
PY=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
listener() {
  local l
  l=$(sudo -n ss -H -ltnp "sport = :$1" 2>/dev/null || ss -H -ltnp "sport = :$1" 2>/dev/null || true)
  sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$l" | sort -u | head -1
}
pid=$(listener 8768)
# Stop ONLY the exact process this install started, at the new commit.
[[ -n $pid && $pid == "__NEW_PID__" ]] || { echo "ROLLBACK_BLOCKED: port 8768 is not the process this install started (pid=${pid:-none}); nothing stopped"; exit 3; }
tr '\0' '\n' <"/proc/$pid/cmdline" | grep -qx -- "__NEW_SHA__" || { echo "ROLLBACK_BLOCKED: port 8768 process is not build __NEW_SHA__; nothing stopped"; exit 3; }
[[ -x $STATE/rollback.sh ]] || { echo "ROLLBACK_BLOCKED: $STATE/rollback.sh is missing; nothing stopped"; exit 3; }
# Emergency recovery: deliberately not subject to the opening-window guard.
kill -TERM "$pid"
for _ in $(seq 300); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
for _ in $(seq 100); do [[ -z $(listener 8768) ]] && break; sleep 0.1; done
[[ -z $(listener 8768) ]] || { echo "ROLLBACK_FAILED: port 8768 did not free"; exit 4; }
"$STATE/rollback.sh"
"$PY" -P - "__OLD_BUILD__" <<'PY'
import json, sys, time
from urllib.request import ProxyHandler, build_opener
opener = build_opener(ProxyHandler({}))
deadline = time.time() + 180
while time.time() < deadline:
    try:
        with opener.open("http://127.0.0.1:8768/healthz", timeout=4) as r:
            health = json.load(r)
        if health.get("build_commit") == sys.argv[1] and health.get("status") in {"HEALTHY", "STARTING"}:
            print("ROLLBACK_OK previous build " + sys.argv[1] + " is answering on 8768")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(3)
print("ROLLBACK_FAILED: previous build did not become healthy within 180 s")
sys.exit(5)
PY
rc=$?
if [[ $(listener 8765) != "__P65__" || $(listener 8767) != "__P67__" ]]; then
  echo "ROLLBACK_WARNING: an 8765/8767 listener changed during rollback (this script never touches them)"
fi
exit $rc
'@

# Unchanged guarded installer bootstrap.
$bootstrap = @'
set -Eeuo pipefail
SHA=__SHA__
BRANCH=claude/phase5-opening-repair
REPO=/home/opc/kalshi-ai-trading-bot
NEW=/home/opc/kalshi-preview-repair-${SHA:0:7}
[[ $(id -un) == opc ]] || { echo "BLOCKED: must run as opc" >&2; exit 1; }
git -C "$REPO" fetch --quiet --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || { echo "BLOCKED: Oracle checkout could not fetch $BRANCH from GitHub; nothing changed" >&2; exit 1; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "BLOCKED: commit $SHA was not fetched from $BRANCH; nothing changed" >&2; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH" || { echo "BLOCKED: $SHA is not on $BRANCH" >&2; exit 1; }
if [[ -e $NEW ]]; then
  [[ $(git -C "$NEW" rev-parse HEAD) == "$SHA" && -z $(git -C "$NEW" status --porcelain) ]] || { echo "BLOCKED: $NEW exists and is not a clean checkout of $SHA; nothing changed" >&2; exit 1; }
else
  stage=$(mktemp -d /home/opc/.kalshi-preview-stage.XXXXXX)
  git clone --quiet --no-checkout "$REPO" "$stage/checkout"
  git -C "$stage/checkout" checkout --quiet --detach "$SHA"
  mv "$stage/checkout" "$NEW"
  rmdir "$stage"
fi
echo "STAGED_CHECKOUT=$NEW COMMIT=$(git -C "$NEW" rev-parse HEAD)"
exec bash "$NEW/deploy/oracle/install_preview_8768.sh" "$SHA"
'@

function Invoke-Rollback($Reason) {
  Write-Host "POST-DEPLOYMENT VERIFICATION FAILED: $Reason" -ForegroundColor Red
  if (-not $switched) {
    throw "Verification failed but this run did not replace the running process (it was already $Sha). Nothing was rolled back; inspect preview 8768."
  }
  Write-Host 'Rolling back preview 8768 to the exact previous process...' -ForegroundColor Yellow
  $rb = $rollbackTemplate.Replace('__NEW_PID__', [string]$post.pid8768).Replace('__NEW_SHA__', $Sha).Replace('__OLD_BUILD__', [string]$pre.build).Replace('__P65__', [string]$pre.pid8765).Replace('__P67__', [string]$pre.pid8767)
  $r = Invoke-Remote $rb
  if ($r.Lines -match '^ROLLBACK_OK') { throw "Deployment rolled back: preview 8768 is again running $($pre.build). Reason: $Reason" }
  throw "ROLLBACK DID NOT COMPLETE (exit $($r.Exit)). Run /home/opc/.kalshi-deploy/preview-8768/rollback.sh on Oracle after stopping only the port 8768 process. Reason: $Reason"
}

Write-Host '1/4 Recording the current preview 8768 state (read only)...'
$pre = Get-RemoteState
if (-not $pre -or -not $pre.pid8768 -or $pre.health -ne 'HEALTHY' -or -not $pre.build) { throw 'Preview 8768 is not running and healthy now; nothing was changed.' }
if (-not $pre.artifact -or $pre.artifact_error) { throw 'Could not identify the running opening artifact; nothing was changed.' }
Write-Host ("Before: build {0}, model {1}, threshold {2}, components {3}, inputs {4}" -f $pre.build, $pre.model_id, $pre.threshold, $pre.components, $pre.inputs)

Write-Host "2/4 Installing $Sha on preview 8768 only (guarded; waits outside the opening window)..."
$install = Invoke-Remote ($bootstrap.Replace('__SHA__', $Sha))
if ($install.Exit -ne 0) {
  Write-Host 'Installer did not succeed. Checking what preview 8768 is running now...' -ForegroundColor Yellow
  $after = Get-RemoteState
  if ($after -and $after.build -eq $pre.build -and $after.health -eq 'HEALTHY') {
    throw "Install failed (exit $($install.Exit)); preview 8768 is running the previous build $($pre.build) (blocked before change or rolled back automatically)."
  }
  throw "Install failed (exit $($install.Exit)) and preview 8768 is NOT confirmed on the previous build. Read the PREVIEW_INSTALL lines above; run /home/opc/.kalshi-deploy/preview-8768/rollback.sh if needed."
}

Write-Host '3/4 Verifying the deployment on Oracle...'
$post = Get-RemoteState
$switched = [bool]($post -and $post.pid8768 -and $post.pid8768 -ne $pre.pid8768)
$problems = @()
if (-not $post) { $problems += 'no state from Oracle' } else {
  if ($post.build -ne $Sha) { $problems += "build is $($post.build)" }
  if ($post.health -ne 'HEALTHY') { $problems += "health is $($post.health)" }
  if ($post.signals_only -ne $true) { $problems += 'not signals-only' }
  if ($post.execution_capability -ne $false) { $problems += 'execution capability is not disabled' }
  if ($post.alerts_endpoint -ne $true) { $problems += '/api/data-quality-alerts not answering' }
  foreach ($k in 'artifact', 'artifact_sha256', 'model_id', 'threshold', 'components', 'inputs', 'journal', 'pid8765', 'pid8767') {
    if ([string]$post.$k -ne [string]$pre.$k) { $problems += "$k changed ($($pre.$k) -> $($post.$k))" }
  }
}
if ($problems.Count -gt 0) { Invoke-Rollback ($problems -join '; ') }
Write-Host ("Verified on Oracle: build {0}, same artifact sha256 {1}, model {2}, threshold {3}, {4} components, {5} inputs, same journal; 8765/8767 unchanged; trading disabled." -f $post.build, $post.artifact_sha256, $post.model_id, $post.threshold, $post.components, $post.inputs) -ForegroundColor Green

Write-Host '4/4 Opening a private local tunnel to the dashboard...'
$LocalPort = $null
foreach ($candidate in 18768..18799) {
  if (-not (Get-NetTCPConnection -LocalPort $candidate -State Listen -ErrorAction SilentlyContinue)) { $LocalPort = $candidate; break }
}
if (-not $LocalPort) { Write-Host 'No free local port in 18768-18799; the deployment is verified on Oracle. Open a tunnel manually to view it.' -ForegroundColor Yellow; return }
$quotedKey = if ($Key) { "-o IdentitiesOnly=yes -i `"$Key`" " } else { '' }
$tunnelArgs = "-N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o ServerAliveInterval=15 $quotedKey-L 127.0.0.1:${LocalPort}:127.0.0.1:8768 $Server"
$tunnel = Start-Process -FilePath ssh.exe -ArgumentList $tunnelArgs -WindowStyle Minimized -PassThru
$health = $null
for ($i = 0; $i -lt 40 -and -not $health; $i++) {
  Start-Sleep -Seconds 1
  if ($tunnel.HasExited) { break }
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalPort/healthz" -TimeoutSec 3 } catch { $health = $null }
}
if (-not $health) {
  if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
  Write-Host "Deployment is verified ON ORACLE, but this PC's tunnel did not answer (closed it; no rollback for a local network issue). Rerun only the tunnel to view the dashboard." -ForegroundColor Yellow
  return
}
if ($health.build_commit -ne $Sha) {
  if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
  Invoke-Rollback "this run's own tunnel answered with build $($health.build_commit), expected $Sha"
}
Write-Host "Preview 8768 is healthy at $Sha (signals only; trading disabled). Dashboard: http://127.0.0.1:$LocalPort/ (close the minimized ssh window to end the tunnel)." -ForegroundColor Green
Start-Process "http://127.0.0.1:$LocalPort/"
}
```
