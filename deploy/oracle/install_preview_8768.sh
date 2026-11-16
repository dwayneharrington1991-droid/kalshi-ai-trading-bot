#!/usr/bin/env bash
set -Eeuo pipefail

# Preview-only installer around the existing guarded switch_preview_8768.sh.
# It never targets systemd, production port 8765, preview port 8767, account
# credentials, journals, model registries or order APIs. It adds artifact
# preservation checks before the switch and prediction checks after it, and
# rolls preview 8768 back through the switch's own rollback command on failure.
#
# Functions are defined first and main runs only when executed, so tests can
# source this file and exercise the validation helpers in isolation.

readonly PREVIEW_PORT=8768
readonly PREVIEW_STATE_ROOT=/home/opc/.kalshi-deploy/preview-8768

say() { printf 'PREVIEW_INSTALL: %s\n' "$*"; }
block() { printf 'PREVIEW_INSTALL_BLOCKED: %s\n' "$*" >&2; exit 1; }

listener_pid() {
  local output pids
  output=$(sudo -n ss -H -ltnp "sport = :$1") || return 1
  [[ -n $output ]] || return 2
  pids=$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$output" | sort -u)
  [[ $(wc -w <<<"$pids") -eq 1 ]] || return 3
  printf '%s\n' "$pids"
}

# Print the value following an exact argv flag of a process (empty if absent).
argv_value() {
  local pid=$1 flag=$2 index
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline"
  for ((index=0; index<${#argv[@]}-1; index++)); do
    if [[ ${argv[index]} == "$flag" ]]; then
      printf '%s\n' "${argv[index+1]}"
      return 0
    fi
  done
  return 0
}

argv_has() {
  local pid=$1 flag=$2 arg
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline"
  for arg in "${argv[@]}"; do
    [[ $arg == "$flag" ]] && return 0
  done
  return 1
}

# Succeeds only when the code in new_root loads the artifact. Reason on stderr.
artifact_loads_with_new_code() {
  local python=$1 new_root=$2 artifact=$3
  PYTHONPATH="$new_root" PYTHONNOUSERSITE=1 "$python" -P -c '
import sys
from src.signals_display.opening_models import load_artifact
artifact, reason = load_artifact(sys.argv[1])
if artifact is None:
    sys.exit("opening artifact rejected by new code: " + reason)
' "$artifact"
}

wait_preview_healthy() {
  local python=$1 seconds=$2
  "$python" -P -c '
import json, sys, time
from urllib.request import ProxyHandler, build_opener
opener = build_opener(ProxyHandler({}))
deadline = time.time() + float(sys.argv[1])
while time.time() < deadline:
    try:
        with opener.open("http://127.0.0.1:8768/healthz", timeout=3) as response:
            if json.load(response).get("status") in {"HEALTHY", "STARTING"}:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(2)
sys.exit(1)
' "$seconds"
}

stop_preview_listener() {
  local pid
  pid=$(listener_pid "$PREVIEW_PORT" 2>/dev/null || true)
  if [[ -n $pid ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..300}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
  fi
  for _ in {1..100}; do
    listener_pid "$PREVIEW_PORT" >/dev/null 2>&1 || return 0
    sleep 0.1
  done
  return 1
}

main() {
  local target=${1:?exact 40-character target commit required}
  local script_dir new_root python=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
  script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  new_root=$(cd "$script_dir/../.." && pwd)

  [[ $(id -un) == opc ]] || block "must run as opc"
  [[ $target =~ ^[0-9a-f]{40}$ ]] || block "target must be an exact 40-character commit"
  [[ $new_root == /home/opc/kalshi-preview-repair-* ]] || block "unexpected staged checkout $new_root"
  [[ $(git -C "$new_root" rev-parse HEAD) == "$target" ]] || block "staged commit mismatch"
  [[ -z $(git -C "$new_root" status --porcelain) ]] || block "staged checkout is dirty"
  [[ -x $python ]] || block "Python interpreter missing: $python"

  local pid_8765 pid_8767 old_pid
  pid_8765=$(listener_pid 8765 2>/dev/null || true)
  pid_8767=$(listener_pid 8767 2>/dev/null || true)
  old_pid=$(listener_pid "$PREVIEW_PORT") || block "exactly one process must listen on port $PREVIEW_PORT"

  # Capture the exact running preview (argv, environment, cwd) so a post-check
  # rollback restarts precisely this process, including the PYTHONPATH that
  # selects its code, exactly like the switch's own in-memory rollback.
  local old_cwd
  local -a old_argv=() old_env=()
  mapfile -d '' -t old_argv <"/proc/$old_pid/cmdline"
  mapfile -d '' -t old_env <"/proc/$old_pid/environ"
  old_cwd=$(readlink -f "/proc/$old_pid/cwd")
  (( ${#old_argv[@]} > 0 )) || block "cannot read the running preview command line"

  # Preserve the currently loaded opening artifact. The guarded switch carries
  # the running --opening-model-artifact argument through unchanged; verify
  # first that the new code accepts it so an incompatible artifact blocks the
  # install before anything is stopped.
  local artifact artifact_digest=
  artifact=$(argv_value "$old_pid" --opening-model-artifact)
  if [[ -n $artifact ]]; then
    [[ -f $artifact && ! -L $artifact ]] || block "loaded opening artifact missing or unsafe: $artifact"
    artifact_digest=$(sha256sum "$artifact" | cut -d' ' -f1)
    if ! artifact_loads_with_new_code "$python" "$new_root" "$artifact"; then
      block "new code would not load the current opening artifact $artifact; nothing was changed"
    fi
    say "opening artifact preserved: $artifact sha256=$artifact_digest"
  else
    say "no opening artifact loaded; preview keeps the shadow statistical fallback"
  fi

  say "running guarded switch_preview_8768.sh for $target"
  if ! PREVIEW_TARGET_COMMIT="$target" PREVIEW_TARGET_ROOT="$new_root" \
      bash "$new_root/deploy/oracle/switch_preview_8768.sh"; then
    block "guarded switch failed; it restored the previous preview process itself"
  fi

  local new_pid switched=1
  new_pid=$(listener_pid "$PREVIEW_PORT" 2>/dev/null || true)
  # The switch exits early when the target is already running. Nothing was
  # stopped then, so a failed post-check must never kill that process or
  # restore an older build from a stale rollback command.
  [[ -n $new_pid && $new_pid == "$old_pid" ]] && switched=0

  postcheck_failed() {
    printf 'PREVIEW_INSTALL_POSTCHECK_FAILED: %s\n' "$*" >&2
    if (( switched == 0 )); then
      block "target was already running before this install; left unchanged for inspection"
    fi
    printf 'PREVIEW_INSTALL_ROLLBACK: stopping new preview and restoring the previous process\n' >&2
    stop_preview_listener || printf 'PREVIEW_INSTALL_ROLLBACK: port 8768 did not free cleanly\n' >&2
    (
      cd "$old_cwd"
      nohup env -i "${old_env[@]}" "${old_argv[@]}" \
        >"$old_cwd/logs/preview-8768-install-rollback.log" 2>&1 &
    ) || true
    if wait_preview_healthy "$python" 120; then
      printf 'PREVIEW_INSTALL_ROLLED_BACK: previous preview process is healthy\n' >&2
    elif [[ -x $PREVIEW_STATE_ROOT/rollback.sh ]] && stop_preview_listener &&
        "$PREVIEW_STATE_ROOT/rollback.sh" >&2 && wait_preview_healthy "$python" 120; then
      printf 'PREVIEW_INSTALL_ROLLED_BACK: restored through %s/rollback.sh\n' "$PREVIEW_STATE_ROOT" >&2
    else
      printf 'PREVIEW_INSTALL_ROLLBACK_FAILED: run %s/rollback.sh manually\n' "$PREVIEW_STATE_ROOT" >&2
    fi
    exit 1
  }

  [[ -n $new_pid ]] || postcheck_failed "no listener on $PREVIEW_PORT after switch"
  [[ $(argv_value "$new_pid" --opening-model-artifact) == "$artifact" ]] ||
    postcheck_failed "opening artifact argument changed"
  if [[ -n $artifact && $(sha256sum "$artifact" | cut -d' ' -f1) != "$artifact_digest" ]]; then
    postcheck_failed "opening artifact contents changed"
  fi
  # Signals-only identity of the running preview process.
  [[ $(argv_value "$new_pid" --mode) == public ]] || postcheck_failed "preview is not in public signals mode"
  if argv_has "$new_pid" --account-readonly || argv_has "$new_pid" --private-dashboard; then
    postcheck_failed "preview unexpectedly has account mode enabled"
  fi

  local report
  if ! report=$("$python" -P -c '
import json, sys, time
from urllib.request import ProxyHandler, build_opener
opener = build_opener(ProxyHandler({}))
def get(path, raw=False):
    with opener.open("http://127.0.0.1:8768" + path, timeout=5) as response:
        body = response.read()
        return body.decode() if raw else json.loads(body)
deadline = time.time() + 90
while True:
    try:
        health = get("/healthz")
        state = get("/api/state?view=main")
        page = get("/", raw=True)
        engine = state.get("prediction_engine") or {}
        assert health.get("build_commit") == sys.argv[1], "build commit mismatch"
        assert health.get("signals_only") is True, "not signals-only"
        assert "initial" in engine, "prediction engine has no opening record"
        assert (state.get("decision_intelligence") or {}).get("execution_capability") is False, "execution capability not disabled"
        assert "id=\"minimal-opening-call\"" in page and "function terminalView(" in page, "terminal page missing"
        break
    except Exception as exc:
        if time.time() > deadline:
            sys.exit("postcheck: " + str(exc))
        time.sleep(3)
opening = engine.get("initial") or {}
print(json.dumps({
    "prediction_engine": "PRESENT",
    "opening_ticker": opening.get("ticker"),
    "opening_direction": opening.get("direction"),
    "opening_confidence_status": (opening.get("confidence") or {}).get("status"),
    "terminal": "AI OPENING CALL + AI CONFIDENCE",
    "execution_capability": False,
}, sort_keys=True))
' "$target"); then
    postcheck_failed "prediction, terminal or signals-only checks failed"
  fi
  printf '%s\n' "$report"

  [[ $(listener_pid 8765 2>/dev/null || true) == "$pid_8765" ]] || postcheck_failed "port 8765 listener changed"
  [[ $(listener_pid 8767 2>/dev/null || true) == "$pid_8767" ]] || postcheck_failed "port 8767 listener changed"

  printf 'PREVIEW_INSTALL_SUCCESS commit=%s pid=%s opening_artifact=%s\n' \
    "$target" "$new_pid" "${artifact:-SHADOW_FALLBACK_ONLY}"
  printf 'AUTOMATED_TRADING=DISABLED ports_8765_8767=UNCHANGED journals_and_registries=UNTOUCHED\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
