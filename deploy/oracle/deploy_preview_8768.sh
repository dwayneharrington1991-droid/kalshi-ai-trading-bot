#!/usr/bin/env bash
# Phase 5 preview-8768 deployment orchestrator (server side).
#
#   deploy_preview_8768.sh deploy   <40-char commit>
#   deploy_preview_8768.sh rollback <40-char commit> <run id>
#   deploy_preview_8768.sh status
#
# Under ONE exclusive lock for the whole sequence it records the running
# preview, takes a private snapshot of its complete launch identity (argv,
# cwd and full environment; mode 0600 in a 0700 directory, never printed),
# runs the guarded installer, verifies the result, and on any failure that
# does not positively leave the previous build healthy performs a guarded
# recovery from that snapshot. It only ever stops a process proven to be this
# run's new build on port 8768; an unknown process or the original process is
# never killed. If recovery cannot be made safe it stops with exact manual
# steps. Ports 8765/8767 are only observed (their PIDs must not change).
#
# Every outcome ends with one line:  DEPLOY_RESULT=<status>
#   SUCCESS (0)  BLOCKED (2)  BLOCKED_CONCURRENT_DEPLOYMENT (75)
#   FAILED_PREVIOUS_STILL_RUNNING (10)  ROLLED_BACK (11)
#   MANUAL_RECOVERY_REQUIRED (12)  STATUS (0)
set -Euo pipefail
umask 077

readonly PORT=8768
readonly STATE_ROOT=${PREVIEW_STATE_ROOT:-/home/opc/.kalshi-deploy/preview-8768}
readonly PY=${PREVIEW_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
readonly HEALTH_TIMEOUT=${PREVIEW_HEALTH_TIMEOUT_SECONDS:-180}
readonly SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly NEW_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
# Test hook only (unset in production): substitute the installer.
readonly INSTALLER=${PREVIEW_TEST_INSTALLER:-$NEW_ROOT/deploy/oracle/install_preview_8768.sh}
export PREVIEW_DEPLOY_LOCK_PATH=${PREVIEW_DEPLOY_LOCK_PATH:-$STATE_ROOT/deploy.lock}

say() { printf 'DEPLOY: %s\n' "$*"; }
finish() { printf 'DEPLOY_RESULT=%s\n' "$1"; exit "$2"; }

listener() {
  local l
  l=$(sudo -n ss -H -ltnp "sport = :$1" 2>/dev/null || ss -H -ltnp "sport = :$1" 2>/dev/null || true)
  sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$l" | sort -u | head -1
}

# JSON description of the 8768 process and service. Prints no environment.
state_json() {
  "$PY" -P - "$(listener 8768)" "$(listener 8765)" "$(listener 8767)" <<'PY'
import hashlib, json, sys
from urllib.request import ProxyHandler, build_opener
p68, p65, p67 = sys.argv[1:4]
def argv(pid):
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0")
    except OSError:
        return []
def value(args, flag):
    return args[args.index(flag) + 1] if flag in args[:-1] else None
opener = build_opener(ProxyHandler({}))
def get(path):
    try:
        with opener.open("http://127.0.0.1:8768" + path, timeout=8) as r:
            return json.load(r)
    except Exception as exc:
        return {"_error": type(exc).__name__}
args = argv(p68) if p68 else []
health, state, alerts = get("/healthz"), get("/api/state?view=main"), get("/api/data-quality-alerts")
info = {"pid8768": p68, "pid8765": p65, "pid8767": p67,
        "argv_build": value(args, "--build-commit"), "port_arg": value(args, "--port"),
        "artifact": value(args, "--opening-model-artifact"), "journal": value(args, "--journal"),
        "build": health.get("build_commit"), "health": health.get("status"),
        "signals_only": health.get("signals_only"),
        "execution_capability": (state.get("decision_intelligence") or {}).get("execution_capability"),
        "alerts_endpoint": alerts.get("read_only") is True}
if info["artifact"]:
    try:
        data = open(info["artifact"], "rb").read()
        artifact = json.loads(data)
        model = artifact.get("model") or {}
        parts = model.get("components") or [model]
        info.update(artifact_sha256=hashlib.sha256(data).hexdigest(), model_id=artifact.get("model_id"),
                    threshold=(artifact.get("decision_policy") or {}).get("threshold"),
                    components=len(parts),
                    inputs=len({n for p in parts for n in ((p.get("preprocessor") or {}).get("names") or [])}))
    except Exception as exc:
        info["artifact_error"] = type(exc).__name__
print(json.dumps(info, sort_keys=True))
PY
}

# Compare current state with the snapshot. Prints OK or a reason list.
check_state() {
  local pre=$1 expected=$2 need_alerts=$3 now
  now=$(state_json)
  # The script arrives on stdin (heredoc), so the state is passed as an argument.
  "$PY" -P - "$pre" "$expected" "$need_alerts" "$now" <<'PY'
import json, sys
pre = json.load(open(sys.argv[1]))
expected, need_alerts = sys.argv[2], sys.argv[3] == "1"
now = json.loads(sys.argv[4])
problems = []
if now.get("build") != expected: problems.append(f"build is {now.get('build')}")
if now.get("health") != "HEALTHY": problems.append(f"health is {now.get('health')}")
if now.get("signals_only") is not True: problems.append("not signals-only")
if now.get("execution_capability") is not False: problems.append("execution capability not disabled")
if need_alerts and now.get("alerts_endpoint") is not True: problems.append("/api/data-quality-alerts not answering")
for key in ("artifact", "artifact_sha256", "model_id", "threshold", "components", "inputs", "journal",
            "pid8765", "pid8767"):
    if str(now.get(key)) != str(pre.get(key)):
        problems.append(f"{key} changed")
print("OK" if not problems else "; ".join(problems))
PY
}

wait_for_build() {
  local pre=$1 expected=$2 need_alerts=$3 deadline verdict
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while :; do
    verdict=$(check_state "$pre" "$expected" "$need_alerts")
    [[ $verdict == OK ]] && return 0
    (( $(date +%s) >= deadline )) && { printf '%s\n' "$verdict"; return 1; }
    sleep 3
  done
}

# Start the snapshot's exact process: full environment via env -i, same argv
# (artifact, journal, flags) and cwd. Never prints the environment.
start_snapshot() {
  local run=$1
  local -a environment=() args=()
  mapfile -d '' -t environment <"$run/environ"
  mapfile -d '' -t args <"$run/argv"
  # Only the launch itself is backgrounded (a backgrounded `cd && nohup`
  # list leaves a bash subshell holding the caller's SSH pipes until the
  # preview exits). stdin detached; deployment-lock fd 9 closed.
  (
    cd "$(cat "$run/cwd")" || exit 1
    nohup env -i "${environment[@]}" "${args[@]}" >>"$run/restore.log" 2>&1 </dev/null 9>&- &
  )
}

manual_steps() {
  local run=$1 reason=$2 current=$3
  printf 'DEPLOY_MANUAL: automatic recovery was not safe: %s\n' "$reason"
  printf 'DEPLOY_MANUAL: current port 8768 listener pid: %s\n' "${current:-none}"
  printf 'DEPLOY_MANUAL: 1) inspect it:   ss -ltnp "sport = :8768"; tr "\\0" " " </proc/%s/cmdline\n' "${current:-PID}"
  printf 'DEPLOY_MANUAL: 2) only if it is NOT the previous build, stop only that pid:   kill -TERM %s\n' "${current:-PID}"
  printf 'DEPLOY_MANUAL: 3) restart the exact previous process:   bash %s/restore.sh\n' "$run"
  printf 'DEPLOY_MANUAL: 4) confirm:   curl -s http://127.0.0.1:8768/healthz   (expect build %s)\n' \
    "$("$PY" -P -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"])' "$run/pre.json")"
  printf 'DEPLOY_MANUAL: never touch ports 8765 or 8767.\n'
}

# Guarded recovery to the snapshot. Stops ONLY this run's new build.
guarded_restore() {
  local run=$1 sha=$2 reason=$3 current pre_pid pre_build verdict
  pre_pid=$("$PY" -P -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid8768"])' "$run/pre.json")
  pre_build=$("$PY" -P -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"])' "$run/pre.json")
  say "guarded recovery: $reason"
  current=$(listener 8768)
  if [[ -n $current ]]; then
    if [[ $current == "$pre_pid" ]]; then
      manual_steps "$run" "the ORIGINAL process is still listening but not healthy; it is never killed automatically" "$current"
      finish MANUAL_RECOVERY_REQUIRED 12
    fi
    local -a cmd=()
    mapfile -d '' -t cmd <"/proc/$current/cmdline" 2>/dev/null || cmd=()
    local is_new=0 i
    for ((i=0; i<${#cmd[@]}-1; i++)); do
      [[ ${cmd[i]} == --build-commit && ${cmd[i+1]} == "$sha" ]] && is_new=1
    done
    if (( ! is_new )); then
      manual_steps "$run" "port 8768 is held by an unknown process (not the previous pid, not build $sha)" "$current"
      finish MANUAL_RECOVERY_REQUIRED 12
    fi
    say "stopping only this run's new build (pid $current)"
    kill -TERM "$current" 2>/dev/null || true
    for _ in $(seq 300); do kill -0 "$current" 2>/dev/null || break; sleep 0.1; done
    for _ in $(seq 100); do [[ -z $(listener 8768) ]] && break; sleep 0.1; done
    if [[ -n $(listener 8768) ]]; then
      manual_steps "$run" "port 8768 did not free after stopping pid $current" "$(listener 8768)"
      finish MANUAL_RECOVERY_REQUIRED 12
    fi
  fi
  say "restarting the exact previous process (same argv, cwd and full environment)"
  start_snapshot "$run"
  if verdict=$(wait_for_build "$run/pre.json" "$pre_build" 0); then
    say "previous build $pre_build is healthy with the same artifact, threshold and journal"
    finish ROLLED_BACK 11
  fi
  manual_steps "$run" "previous build did not become healthy: $verdict" "$(listener 8768)"
  finish MANUAL_RECOVERY_REQUIRED 12
}

snapshot() {
  local run=$1 pid=$2
  install -d -m 0700 "$run"
  cat "/proc/$pid/environ" >"$run/environ"
  cat "/proc/$pid/cmdline" >"$run/argv"
  printf '%s' "$(readlink -f "/proc/$pid/cwd")" >"$run/cwd"
  chmod 0600 "$run/environ" "$run/argv" "$run/cwd"
  cat >"$run/restore.sh" <<'RESTORE'
#!/usr/bin/env bash
# Restart the exact previous preview 8768 process recorded for this run.
set -Eeuo pipefail
run=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ -z $( (sudo -n ss -H -ltnp "sport = :8768" 2>/dev/null || ss -H -ltnp "sport = :8768") ) ]] ||
  { echo "port 8768 is still in use; stop only the wrong process first"; exit 1; }
mapfile -d '' -t environment <"$run/environ"
mapfile -d '' -t args <"$run/argv"
cd "$(cat "$run/cwd")"
nohup env -i "${environment[@]}" "${args[@]}" >>"$run/restore.log" 2>&1 </dev/null 9>&- &
echo "restored_pid=$!"
RESTORE
  chmod 0700 "$run/restore.sh"
}

prune_runs() {
  local keep=5 dir
  local -a old=()
  [[ -d $STATE_ROOT/runs ]] || return 0
  mapfile -t old < <(ls -1dt "$STATE_ROOT"/runs/*/ 2>/dev/null | tail -n +$((keep + 1)))
  for dir in "${old[@]}"; do rm -rf -- "$dir"; done
}

mode=${1:-}
case $mode in
  status)
    state_json | "$PY" -P -c 'import json,sys;s=json.load(sys.stdin);print("DEPLOY_STATUS "+json.dumps({k:s.get(k) for k in ("pid8768","build","health","model_id","threshold","components","inputs","execution_capability","pid8765","pid8767")},sort_keys=True))'
    finish STATUS 0 ;;
  deploy|rollback) ;;
  *) echo "usage: $0 deploy <commit> | rollback <commit> <run id> | status" >&2; finish BLOCKED 2 ;;
esac

sha=${2:-}
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { say "an exact 40-character commit is required"; finish BLOCKED 2; }
[[ $(id -un) == opc || -n ${PREVIEW_TEST_INSTALLER:-} ]] || { say "must run as opc"; finish BLOCKED 2; }

# shellcheck source=deploy/oracle/preview_deploy_lock.sh
source "$SCRIPT_DIR/preview_deploy_lock.sh"
install -d -m 0700 "$STATE_ROOT"
preview_deploy_lock || finish BLOCKED_CONCURRENT_DEPLOYMENT 75

if [[ $mode == rollback ]]; then
  run_id=${3:-}
  [[ $run_id =~ ^[0-9A-Za-z_-]+$ && -f $STATE_ROOT/runs/$run_id/pre.json ]] ||
    { say "unknown run id"; finish BLOCKED 2; }
  guarded_restore "$STATE_ROOT/runs/$run_id" "$sha" "requested by the deployment client"
fi

[[ $(git -C "$NEW_ROOT" rev-parse HEAD 2>/dev/null) == "$sha" ]] || { say "staged checkout is not $sha"; finish BLOCKED 2; }
pre_pid=$(listener 8768)
[[ -n $pre_pid ]] || { say "no process is listening on 8768; nothing was changed"; finish BLOCKED 2; }
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
run=$STATE_ROOT/runs/$run_id
snapshot "$run" "$pre_pid"
state_json >"$run/pre.json"
chmod 0600 "$run/pre.json"
read -r pre_health pre_build pre_artifact_error < <("$PY" -P -c 'import json,sys;s=json.load(open(sys.argv[1]));print(s.get("health"),s.get("build"),s.get("artifact_error") or "-")' "$run/pre.json")
if [[ $pre_health != HEALTHY || -z $pre_build || $pre_build == None || $pre_artifact_error != - ]]; then
  say "the running preview is not healthy or its artifact is unreadable; nothing was changed"
  finish BLOCKED 2
fi
printf 'DEPLOY_RUN_ID=%s\n' "$run_id"
say "previous build $pre_build (pid $pre_pid) recorded; installing $sha on preview 8768 only"
prune_runs

bash "$INSTALLER" "$sha"
rc=$?

if (( rc == 0 )); then
  if verdict=$(wait_for_build "$run/pre.json" "$sha" 1); then
    say "verified: build $sha healthy; same artifact, model, threshold, components, inputs and journal; 8765/8767 unchanged; trading disabled"
    finish SUCCESS 0
  fi
  guarded_restore "$run" "$sha" "post-install verification failed: $verdict"
fi

say "installer exited $rc; confirming what preview 8768 is running"
current=$(listener 8768)
verdict=$(check_state "$run/pre.json" "$pre_build" 0)
if [[ $verdict == OK ]]; then
  if [[ $current == "$pre_pid" ]]; then
    say "the original process is still running and healthy; nothing was changed"
  else
    say "the installer already restored the previous build (healthy, same artifact and journal)"
  fi
  finish FAILED_PREVIOUS_STILL_RUNNING 10
fi
guarded_restore "$run" "$sha" "installer failed and the previous build is not confirmed healthy ($verdict)"
