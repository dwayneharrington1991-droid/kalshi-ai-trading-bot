#!/usr/bin/env bash
set -Eeuo pipefail

# Dedicated preview-only switch. It never targets systemd, production paths,
# ports 8765/8767, account credentials, backups, or order APIs.
readonly TARGET=${PREVIEW_TARGET_COMMIT:-a739646155cbe36e324e4b5f9c6f58a0cedbc4a8}
readonly OLD_ROOT=/home/opc/kalshi-preview-upgrade
readonly NEW_ROOT=${PREVIEW_TARGET_ROOT:-/home/opc/kalshi-preview-repair-a739646}
readonly JOURNAL=/home/opc/kalshi-preview-upgrade/logs/upgraded-live-8768.db
readonly PYTHON=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
readonly HOST=127.0.0.1
readonly PORT=8768
readonly MIN_FREE_BYTES=$((10 * 1024 * 1024 * 1024))
readonly STATE_ROOT=/home/opc/.kalshi-deploy/preview-8768
readonly LOG_PATH="$OLD_ROOT/logs/preview-8768-${TARGET:0:7}.log"
readonly OPENING_ARTIFACT=${PREVIEW_OPENING_MODEL_ARTIFACT:-}
readonly READY_TIMEOUT=${PREVIEW_READY_TIMEOUT_SECONDS:-600}

old_pid=
new_pid=
old_cwd=
switched=0
declare -a old_argv=()
declare -a old_env=()

fail() { printf 'PREVIEW_DEPLOY_BLOCKED: %s\n' "$*" >&2; return 1; }

listener_pid() {
  local port=$1 output pids
  output=$(sudo -n ss -H -ltnp "sport = :$port") || return 1
  [[ -n $output ]] || return 2
  pids=$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$output" | sort -u)
  [[ $(wc -w <<<"$pids") -eq 1 ]] || return 3
  printf '%s\n' "$pids"
}

wait_stopped() {
  local pid=$1 count
  for ((count=0; count<300; count++)); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.1
  done
  return 1
}

wait_port_free() {
  local count rc
  for ((count=0; count<100; count++)); do
    rc=0; listener_pid "$PORT" >/dev/null 2>&1 || rc=$?
    [[ $rc -eq 2 ]] && return 0
    sleep 0.1
  done
  return 1
}

db_open_pids() {
  local proc fd target
  for proc in /proc/[0-9]*; do
    for fd in "$proc"/fd/*; do
      target=$(readlink -f "$fd" 2>/dev/null || true)
      case "$target" in
        "$JOURNAL"|"$JOURNAL-wal"|"$JOURNAL-shm") basename "$proc" ;;
      esac
    done
  done | sort -nu
}

has_arg_pair() {
  local expected_name=$1 expected_value=$2 index
  for ((index=0; index<${#old_argv[@]}-1; index++)); do
    [[ ${old_argv[index]} == "$expected_name" && ${old_argv[index+1]} == "$expected_value" ]] && return 0
  done
  return 1
}

process_has_environment() {
  local pid=$1 expected=$2
  tr '\0' '\n' <"/proc/$pid/environ" | grep -Fx -- "$expected" >/dev/null
}

readiness_report() {
  # Prints one line per endpoint and exits 0 only when every condition holds.
  "$PYTHON" - "$1" "${PREVIEW_PROBE_PORT:-8768}" <<'PY'
import json, sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
opener = build_opener(ProxyHandler({}))
target, port = sys.argv[1], sys.argv[2]
results, failures = {}, []
for path in ("/healthz", "/readyz", "/api/candle-integrity", "/api/state?view=main"):
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            results[path] = (response.status, json.load(response))
    except HTTPError as exc:
        try:
            body = json.load(exc)
        except Exception:
            body = {}
        results[path] = (exc.code, body)
    except (URLError, OSError, ValueError) as exc:
        results[path] = (None, {"error": type(exc).__name__})
for path, (code, body) in results.items():
    if path != "/api/state?view=main":
        print(f"{path} HTTP {code} " + json.dumps(body, sort_keys=True, default=str)[:600])
h = results["/healthz"][1]; r = results["/readyz"][1]
c = results["/api/candle-integrity"][1]; s = results["/api/state?view=main"][1]
def check(ok, reason):
    if not ok:
        failures.append(reason)
check(results["/healthz"][0] == 200 and h.get("status")=="HEALTHY" and h.get("build_commit")==target, "healthz not HEALTHY for target build")
check(h.get("signals_only") is True and (h.get("workers") or {}).get("healthy") is True, "workers unhealthy or not signals-only")
check(results["/readyz"][0] == 200 and r.get("status")=="READY" and r.get("signals_only") is True, "readyz not READY")
check(r.get("contract_quotes_fresh") is True and r.get("reference_fresh") is True, "contract quotes or reference not fresh")
if c.get("status") == "READY":
    check(c.get("bars")==1440 and c.get("unique_bars")==1440 and not c.get("missing_intervals") and not c.get("duplicate_bars"), "candle integrity inconsistent")
else:
    engine = s.get("prediction_engine") or {}
    opening = engine.get("initial") or {}
    withheld = opening.get("direction") in {"WAIT", "UNCLEAR"}
    quality = opening.get("data_quality") or {}
    # A live rolling-window gap that appears after contract opening does not
    # retroactively invalidate an immutable, cutoff-safe opening snapshot. It
    # must, however, have explicit evidence of its own complete 1,440-bar
    # history and belong to the current contract. Otherwise direction is
    # required to be withheld.
    frozen_safe = (
        opening.get("direction") in {"UP", "DOWN"}
        and quality.get("closed_history_bars") == 1440
        and opening.get("data_cutoff") is not None
        and opening.get("history_last_close") is not None
        and (not opening.get("ticker") or opening.get("ticker") == engine.get("ticker"))
    )
    if frozen_safe:
        try:
            from datetime import datetime
            cutoff = datetime.fromisoformat(str(opening["data_cutoff"]).replace("Z", "+00:00")).timestamp()
            frozen_safe = float(opening["history_last_close"]) <= cutoff
        except (TypeError, ValueError, OverflowError):
            frozen_safe = False
    check(c.get("status")=="INCOMPLETE" and bool(c.get("missing_intervals"))
          and (withheld or frozen_safe),
          "candle window incomplete without a withheld or independently verified frozen opening")
check(results["/api/candle-integrity"][0] == 200 and results["/api/state?view=main"][0] == 200, "candle-integrity or state endpoint not HTTP 200")
check((s.get("decision_intelligence") or {}).get("execution_capability") is False, "execution capability not disabled")
print("READINESS " + ("PASS" if not failures else "FAIL: " + "; ".join(failures)))
print(json.dumps({"health": h.get("status"), "ready": r.get("status"), "candles": c.get("status"),
                  "missing": c.get("missing_intervals"), "signals_only": True}, sort_keys=True, default=str))
sys.exit(1 if failures else 0)
PY
}

write_rollback_command() {
  local rollback="$STATE_ROOT/rollback.sh" arg
  install -d -m 0700 "$STATE_ROOT"
  {
    printf '#!/usr/bin/env bash\nset -Eeuo pipefail\ncd %q\n' "$old_cwd"
    printf 'nohup '
    for arg in "${old_argv[@]}"; do printf '%q ' "$arg"; done
    printf '> %q 2>&1 &\n' "$OLD_ROOT/logs/preview-8768-rollback.log"
    printf 'echo "rollback_pid=$!"\n'
  } >"$rollback.next"
  chmod 0700 "$rollback.next"
  mv -f "$rollback.next" "$rollback"
}

start_old_in_memory() {
  cd "$old_cwd"
  nohup env -i "${old_env[@]}" "${old_argv[@]}" \
    >"$OLD_ROOT/logs/preview-8768-rollback.log" 2>&1 &
  local rollback_pid=$!
  for _ in {1..120}; do
    if "$PYTHON" - "$TARGET" <<'PY' >/dev/null 2>&1
import json, sys
from urllib.request import ProxyHandler, build_opener
try:
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8768/healthz", timeout=2) as r:
        value=json.load(r)
    assert value.get("status") in {"HEALTHY", "STARTING"}
except Exception:
    raise SystemExit(1)
PY
    then
      printf 'PREVIEW_ROLLBACK_STARTED pid=%s\n' "$rollback_pid" >&2
      return 0
    fi
    kill -0 "$rollback_pid" 2>/dev/null || break
    sleep 1
  done
  printf 'PREVIEW_ROLLBACK_FAILED pid=%s\n' "$rollback_pid" >&2
  return 1
}

rollback_on_error() {
  local rc=$?
  trap - ERR INT TERM
  if [[ $switched -eq 1 ]]; then
    if [[ -n ${new_pid:-} ]] && kill -0 "$new_pid" 2>/dev/null; then
      kill -TERM "$new_pid" 2>/dev/null || true
      wait_stopped "$new_pid" || true
    fi
    wait_port_free || true
    start_old_in_memory || true
  fi
  printf 'PREVIEW_DEPLOY_FAILED rc=%s target=%s\n' "$rc" "$TARGET" >&2
  exit "$rc"
}
trap rollback_on_error ERR INT TERM

[[ -x $PYTHON ]] || fail "Python interpreter missing: $PYTHON"
[[ $TARGET =~ ^[0-9a-f]{40}$ ]] || fail "target must be an exact 40-character commit"
[[ $READY_TIMEOUT =~ ^[0-9]+$ ]] && (( READY_TIMEOUT >= 60 && READY_TIMEOUT <= 1800 )) || fail "readiness timeout must be 60..1800 seconds"
[[ $NEW_ROOT == /home/opc/kalshi-preview-repair-* ]] || fail "unexpected target root"
[[ -d $OLD_ROOT && -d $NEW_ROOT ]] || fail "preview checkout missing"
[[ -f $JOURNAL ]] || fail "existing preview journal missing"
[[ ! -L $JOURNAL ]] || fail "journal must be a regular non-symlink path"
[[ $(git -C "$NEW_ROOT" rev-parse HEAD) == "$TARGET" ]] || fail "staged commit mismatch"
[[ -z $(git -C "$NEW_ROOT" status --porcelain) ]] || fail "staged checkout is dirty"
git -C "$NEW_ROOT" cat-file -e "$TARGET^{commit}"
"$PYTHON" -c 'import httpx, cryptography; from websockets.sync.client import connect'
if [[ -n $OPENING_ARTIFACT ]]; then
  [[ $OPENING_ARTIFACT == "$STATE_ROOT"/models/* ]] || fail "opening artifact must be in preview deployment state"
  [[ -f $OPENING_ARTIFACT && ! -L $OPENING_ARTIFACT ]] || fail "opening artifact missing or unsafe"
  [[ $(stat -c %U "$OPENING_ARTIFACT") == $(id -un) ]] || fail "opening artifact owner mismatch"
  PYTHONPATH="$NEW_ROOT" PYTHONNOUSERSITE=1 "$PYTHON" - "$OPENING_ARTIFACT" <<'PY'
import sys
from src.signals_display.opening_models import load_artifact
artifact, reason = load_artifact(sys.argv[1])
if artifact is None:
    raise SystemExit("opening artifact rejected: " + reason)
PY
fi

free_bytes=$(df -B1 --output=avail "$JOURNAL" | tail -1 | tr -d ' ')
[[ $free_bytes =~ ^[0-9]+$ ]] || fail "could not measure free storage"
(( free_bytes >= MIN_FREE_BYTES )) || fail "free storage below 10 GiB: $free_bytes"

# Record but never signal unrelated listeners. Their identities must remain stable.
pid_8765=$(listener_pid 8765 2>/dev/null || true)
pid_8767=$(listener_pid 8767 2>/dev/null || true)

old_pid=$(listener_pid "$PORT") || fail "exactly one process must listen on 127.0.0.1:$PORT"
[[ -r /proc/$old_pid/cmdline && -r /proc/$old_pid/environ ]] || fail "cannot inspect preview process"
mapfile -d '' -t old_argv <"/proc/$old_pid/cmdline"
mapfile -d '' -t old_env <"/proc/$old_pid/environ"
old_cwd=$(readlink -f "/proc/$old_pid/cwd")
[[ $old_cwd == "$OLD_ROOT" ]] || fail "8768 process cwd is not $OLD_ROOT"
[[ ${old_argv[*]} == *"src.signals_display.server"* ]] || fail "8768 listener is not signals server"
has_arg_pair --port 8768 || fail "8768 process command identity mismatch"
has_arg_pair --mode public || fail "8768 process is not public signals mode"
[[ ${old_argv[*]} != *"--account-readonly"* ]] || fail "preview unexpectedly has account mode enabled"
[[ $(stat -c %U "/proc/$old_pid") == $(id -un) ]] || fail "8768 process is owned by another user"

running_health=$(
  "$PYTHON" - <<'PY'
import json
from urllib.request import ProxyHandler, build_opener
with build_opener(ProxyHandler({})).open("http://127.0.0.1:8768/healthz", timeout=5) as r:
    print(json.dumps(json.load(r), separators=(",", ":")))
PY
)
if "$PYTHON" - "$running_health" "$TARGET" <<'PY'
import json, sys
value=json.loads(sys.argv[1])
raise SystemExit(0 if value.get("status")=="HEALTHY" and value.get("build_commit")==sys.argv[2]
                 and value.get("signals_only") is True and (value.get("workers") or {}).get("healthy") else 1)
PY
then
  has_arg_pair --journal "$JOURNAL" || fail "target process does not use absolute preserved journal"
  if [[ -n $OPENING_ARTIFACT ]]; then
    has_arg_pair --opening-model-artifact "$OPENING_ARTIFACT" || fail "target process does not use requested opening artifact"
  fi
  [[ " ${old_argv[*]} " == *" -P "* ]] || fail "target process does not use Python safe-path mode"
  process_has_environment "$old_pid" "PYTHONPATH=$NEW_ROOT" || fail "target process is not loading repaired code"
  printf 'PREVIEW_ALREADY_DEPLOYED commit=%s pid=%s journal=%s\n' "$TARGET" "$old_pid" "$JOURNAL"
  exit 0
fi

write_rollback_command

# Reuse the exact old argv, replacing only identity/safety-critical preview values.
declare -a new_args=(-P)
skip=0
seen_module=0
for arg in "${old_argv[@]:1}"; do
  if (( skip )); then skip=0; continue; fi
  if [[ $arg == -m ]]; then
    seen_module=1
    new_args+=(-m src.signals_display.server)
    skip=1
    continue
  fi
  (( seen_module )) || continue
  case "$arg" in
    --host|--port|--journal|--build-commit) skip=1 ;;
    --opening-model-artifact)
      if [[ -n $OPENING_ARTIFACT ]]; then skip=1; else new_args+=("$arg"); fi ;;
    --disable-scheduled-learning) ;;
    --account-readonly|--private-dashboard) fail "account/private flags are not allowed on preview switch" ;;
    *) new_args+=("$arg") ;;
  esac
done
(( seen_module )) || fail "could not reconstruct module launch"
new_args+=(--host "$HOST" --port "$PORT" --journal "$JOURNAL" \
  --build-commit "$TARGET" --disable-scheduled-learning)
if [[ -n $OPENING_ARTIFACT ]]; then
  new_args+=(--opening-model-artifact "$OPENING_ARTIFACT")
fi

kill -TERM "$old_pid"
wait_stopped "$old_pid" || fail "preview did not stop gracefully within 30 seconds"
wait_port_free || fail "port 8768 remained occupied after graceful stop"
[[ -z $(db_open_pids) ]] || fail "SQLite journal or sidecars remain open after stop"
switched=1

cd "$OLD_ROOT"
nohup env PYTHONPATH="$NEW_ROOT" PYTHONNOUSERSITE=1 \
  "$PYTHON" "${new_args[@]}" >"$LOG_PATH" 2>&1 &
new_pid=$!

# Readiness: every original condition is still required. The wait is bounded
# by wall-clock time (a large preserved journal can make startup slow), and
# each failing endpoint is reported with its HTTP status and body.
ready_deadline=$((SECONDS + READY_TIMEOUT))
next_progress=$((SECONDS + 30))
while (( SECONDS < ready_deadline )); do
  kill -0 "$new_pid" 2>/dev/null || { tail -n 60 "$LOG_PATH" >&2 || true; fail "repaired preview exited during startup"; }
  if readiness_report "$TARGET" >/dev/null 2>&1; then break; fi
  if (( SECONDS >= next_progress )); then
    printf 'PREVIEW_READINESS_PROGRESS elapsed=%ss\n' "$((READY_TIMEOUT - (ready_deadline - SECONDS)))" >&2
    readiness_report "$TARGET" >&2 || true
    next_progress=$((SECONDS + 30))
  fi
  sleep 2
done

# Final checks are repeated outside the loop so timeout is a hard failure.
if ! readiness_report "$TARGET"; then
  printf 'PREVIEW_READINESS_TIMEOUT after %ss; last 60 lines of %s:\n' "$READY_TIMEOUT" "$LOG_PATH" >&2
  tail -n 60 "$LOG_PATH" >&2 || true
  fail "repaired preview did not become healthy and ready"
fi

[[ $(listener_pid 8765 2>/dev/null || true) == "$pid_8765" ]] || fail "port 8765 listener changed"
[[ $(listener_pid 8767 2>/dev/null || true) == "$pid_8767" ]] || fail "port 8767 listener changed"
[[ $(listener_pid "$PORT") == "$new_pid" ]] || fail "new process does not exclusively own port 8768"
[[ $(readlink -f "/proc/$new_pid/cwd") == "$OLD_ROOT" ]] || fail "new cwd changed"
tr '\0' '\n' <"/proc/$new_pid/environ" | grep -Fx "PYTHONPATH=$NEW_ROOT" >/dev/null || fail "new code path identity missing"
if [[ -n $OPENING_ARTIFACT ]]; then
  mapfile -d '' -t running_argv <"/proc/$new_pid/cmdline"
  found_artifact=0
  for ((i=0; i<${#running_argv[@]}-1; i++)); do
    if [[ ${running_argv[i]} == --opening-model-artifact && ${running_argv[i+1]} == "$OPENING_ARTIFACT" ]]; then
      found_artifact=1
      break
    fi
  done
  (( found_artifact )) || fail "running process opening artifact identity mismatch"
fi

switched=0
trap - ERR INT TERM
printf 'PREVIEW_DEPLOY_SUCCESS commit=%s pid=%s journal=%s rollback=%s/rollback.sh\n' \
  "$TARGET" "$new_pid" "$JOURNAL" "$STATE_ROOT"
printf 'AUTOMATED_TRADING=DISABLED scheduled_learning=DISABLED ports_8765_8767=UNCHANGED\n'
running_artifact=
mapfile -d '' -t final_argv <"/proc/$new_pid/cmdline"
for ((i=0; i<${#final_argv[@]}-1; i++)); do
  if [[ ${final_argv[i]} == --opening-model-artifact ]]; then
    running_artifact=${final_argv[i+1]}
  fi
done
# Report the artifact the running process actually loaded (it may have been
# carried through from the previous process rather than requested explicitly).
printf 'OPENING_ARTIFACT=%s\n' "${running_artifact:-NONE_SHADOW_FALLBACK_ONLY}"
