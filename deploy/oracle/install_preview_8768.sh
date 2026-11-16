#!/usr/bin/env bash
set -Eeuo pipefail

# Preview-only installer around the existing guarded switch_preview_8768.sh.
# It never targets systemd, production port 8765, preview port 8767, account
# credentials, journals, model registries or order APIs. It adds artifact
# preservation checks before the switch and prediction checks after it, and
# rolls preview 8768 back through the switch's own rollback command on failure.

readonly TARGET=${1:?exact 40-character target commit required}
readonly SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly NEW_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
readonly PYTHON=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
readonly STATE_ROOT=/home/opc/.kalshi-deploy/preview-8768
readonly PORT=8768

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

artifact_of() {
  local pid=$1 index
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline"
  for ((index=0; index<${#argv[@]}-1; index++)); do
    if [[ ${argv[index]} == --opening-model-artifact ]]; then
      printf '%s\n' "${argv[index+1]}"
      return 0
    fi
  done
  return 0
}

[[ $(id -un) == opc ]] || block "must run as opc"
[[ $TARGET =~ ^[0-9a-f]{40}$ ]] || block "target must be an exact 40-character commit"
[[ $NEW_ROOT == /home/opc/kalshi-preview-repair-* ]] || block "unexpected staged checkout $NEW_ROOT"
[[ $(git -C "$NEW_ROOT" rev-parse HEAD) == "$TARGET" ]] || block "staged commit mismatch"
[[ -z $(git -C "$NEW_ROOT" status --porcelain) ]] || block "staged checkout is dirty"
[[ -x $PYTHON ]] || block "Python interpreter missing: $PYTHON"

pid_8765=$(listener_pid 8765 2>/dev/null || true)
pid_8767=$(listener_pid 8767 2>/dev/null || true)
old_pid=$(listener_pid "$PORT") || block "exactly one process must listen on port $PORT"

# Preserve the currently loaded opening artifact: the guarded switch carries
# the running --opening-model-artifact argument through unchanged. Verify now
# that the new code still accepts it, so an incompatible artifact blocks the
# install before anything is stopped.
artifact=$(artifact_of "$old_pid")
artifact_digest=
if [[ -n $artifact ]]; then
  [[ -f $artifact && ! -L $artifact ]] || block "loaded opening artifact missing or unsafe: $artifact"
  artifact_digest=$(sha256sum "$artifact" | cut -d' ' -f1)
  PYTHONPATH="$NEW_ROOT" PYTHONNOUSERSITE=1 "$PYTHON" -P - "$artifact" <<'PY' ||
import sys
from src.signals_display.opening_models import load_artifact
artifact, reason = load_artifact(sys.argv[1])
if artifact is None:
    raise SystemExit("opening artifact rejected by new code: " + reason)
PY
    block "new code would not load the current opening artifact; nothing was changed"
  say "opening artifact preserved: $artifact sha256=$artifact_digest"
else
  say "no opening artifact loaded; preview keeps the shadow statistical fallback"
fi

say "running guarded switch_preview_8768.sh for $TARGET"
PREVIEW_TARGET_COMMIT="$TARGET" PREVIEW_TARGET_ROOT="$NEW_ROOT" \
  bash "$NEW_ROOT/deploy/oracle/switch_preview_8768.sh"

rollback_after_switch() {
  printf 'PREVIEW_INSTALL_POSTCHECK_FAILED: %s; rolling back preview 8768\n' "$*" >&2
  local pid
  pid=$(listener_pid "$PORT" 2>/dev/null || true)
  if [[ -n $pid ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..300}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
  fi
  for _ in {1..100}; do
    listener_pid "$PORT" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if [[ -x $STATE_ROOT/rollback.sh ]]; then
    "$STATE_ROOT/rollback.sh" >&2 || true
  fi
  exit 1
}

new_pid=$(listener_pid "$PORT") || rollback_after_switch "no listener on $PORT after switch"
[[ $(artifact_of "$new_pid") == "$artifact" ]] || rollback_after_switch "opening artifact argument changed"
if [[ -n $artifact ]]; then
  [[ $(sha256sum "$artifact" | cut -d' ' -f1) == "$artifact_digest" ]] ||
    rollback_after_switch "opening artifact contents changed"
fi

"$PYTHON" -P - "$TARGET" <<'PY' || rollback_after_switch "prediction or terminal checks failed"
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
        assert health.get("build_commit") == sys.argv[1] and health.get("signals_only") is True
        assert "initial" in engine, "prediction engine has no opening record"
        assert (state.get("decision_intelligence") or {}).get("execution_capability") is False
        assert 'id="minimal-opening-call"' in page and "function terminalView(" in page
        break
    except Exception as exc:
        if time.time() > deadline:
            raise SystemExit(f"postcheck: {exc}")
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
PY

[[ $(listener_pid 8765 2>/dev/null || true) == "$pid_8765" ]] || rollback_after_switch "port 8765 listener changed"
[[ $(listener_pid 8767 2>/dev/null || true) == "$pid_8767" ]] || rollback_after_switch "port 8767 listener changed"

printf 'PREVIEW_INSTALL_SUCCESS commit=%s pid=%s opening_artifact=%s\n' \
  "$TARGET" "$new_pid" "${artifact:-SHADOW_FALLBACK_ONLY}"
printf 'AUTOMATED_TRADING=DISABLED ports_8765_8767=UNCHANGED journals_and_registries=UNTOUCHED\n'
