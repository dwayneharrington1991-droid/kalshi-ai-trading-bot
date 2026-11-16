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
[[ $NEW_ROOT == /home/opc/kalshi-preview-repair-* ]] || fail "unexpected target root"
[[ -d $OLD_ROOT && -d $NEW_ROOT ]] || fail "preview checkout missing"
[[ -f $JOURNAL ]] || fail "existing preview journal missing"
[[ ! -L $JOURNAL ]] || fail "journal must be a regular non-symlink path"
[[ $(git -C "$NEW_ROOT" rev-parse HEAD) == "$TARGET" ]] || fail "staged commit mismatch"
[[ -z $(git -C "$NEW_ROOT" status --porcelain) ]] || fail "staged checkout is dirty"
git -C "$NEW_ROOT" cat-file -e "$TARGET^{commit}"
"$PYTHON" -c 'import httpx, cryptography; from websockets.sync.client import connect'

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
    --disable-scheduled-learning) ;;
    --account-readonly|--private-dashboard) fail "account/private flags are not allowed on preview switch" ;;
    *) new_args+=("$arg") ;;
  esac
done
(( seen_module )) || fail "could not reconstruct module launch"
new_args+=(--host "$HOST" --port "$PORT" --journal "$JOURNAL" \
  --build-commit "$TARGET" --disable-scheduled-learning)

kill -TERM "$old_pid"
wait_stopped "$old_pid" || fail "preview did not stop gracefully within 30 seconds"
wait_port_free || fail "port 8768 remained occupied after graceful stop"
[[ -z $(db_open_pids) ]] || fail "SQLite journal or sidecars remain open after stop"
switched=1

cd "$OLD_ROOT"
nohup env PYTHONPATH="$NEW_ROOT" PYTHONNOUSERSITE=1 \
  "$PYTHON" "${new_args[@]}" >"$LOG_PATH" 2>&1 &
new_pid=$!

for _ in {1..180}; do
  kill -0 "$new_pid" 2>/dev/null || fail "repaired preview exited during startup"
  if "$PYTHON" - "$TARGET" <<'PY' >/dev/null 2>&1
import json, sys
from urllib.request import ProxyHandler, build_opener
opener=build_opener(ProxyHandler({}))
def get(path):
    with opener.open("http://127.0.0.1:8768"+path, timeout=3) as r:
        assert r.status == 200
        return json.load(r)
h=get("/healthz"); r=get("/readyz"); c=get("/api/candle-integrity"); s=get("/api/state?view=main")
assert h.get("status")=="HEALTHY" and h.get("build_commit")==sys.argv[1]
assert h.get("signals_only") is True and (h.get("workers") or {}).get("healthy") is True
assert r.get("status")=="READY" and r.get("signals_only") is True
assert r.get("contract_quotes_fresh") is True and r.get("reference_fresh") is True
assert c.get("status") in {"READY", "INCOMPLETE"}
if c.get("status") == "READY":
    assert c.get("bars")==1440 and c.get("unique_bars")==1440
    assert not c.get("missing_intervals") and not c.get("duplicate_bars")
else:
    assert c.get("missing_intervals")
    opening=(s.get("prediction_engine") or {}).get("initial") or {}
    assert opening.get("up") is None and opening.get("direction") in {"WAIT", "UNCLEAR"}
assert (s.get("decision_intelligence") or {}).get("execution_capability") is False
PY
  then break; fi
  sleep 1
done

# Final checks are repeated outside the loop so timeout is a hard failure.
"$PYTHON" - "$TARGET" <<'PY'
import json, sys
from urllib.request import ProxyHandler, build_opener
opener=build_opener(ProxyHandler({}))
def get(path):
    with opener.open("http://127.0.0.1:8768"+path, timeout=5) as r: return json.load(r)
h,r,c,s=get("/healthz"),get("/readyz"),get("/api/candle-integrity"),get("/api/state?view=main")
assert h.get("status")=="HEALTHY" and h.get("build_commit")==sys.argv[1]
assert h.get("signals_only") is True and (h.get("workers") or {}).get("healthy") is True
assert r.get("status")=="READY" and r.get("signals_only") is True
assert c.get("status")=="READY" or (c.get("missing_intervals") and ((s.get("prediction_engine") or {}).get("initial") or {}).get("up") is None)
assert (s.get("decision_intelligence") or {}).get("execution_capability") is False
print(json.dumps({"health":h.get("status"),"ready":r.get("status"),"candles":c.get("status"),"missing":c.get("missing_intervals"),"signals_only":True},sort_keys=True))
PY

[[ $(listener_pid 8765 2>/dev/null || true) == "$pid_8765" ]] || fail "port 8765 listener changed"
[[ $(listener_pid 8767 2>/dev/null || true) == "$pid_8767" ]] || fail "port 8767 listener changed"
[[ $(listener_pid "$PORT") == "$new_pid" ]] || fail "new process does not exclusively own port 8768"
[[ $(readlink -f "/proc/$new_pid/cwd") == "$OLD_ROOT" ]] || fail "new cwd changed"
tr '\0' '\n' <"/proc/$new_pid/environ" | grep -Fx "PYTHONPATH=$NEW_ROOT" >/dev/null || fail "new code path identity missing"

switched=0
trap - ERR INT TERM
printf 'PREVIEW_DEPLOY_SUCCESS commit=%s pid=%s journal=%s rollback=%s/rollback.sh\n' \
  "$TARGET" "$new_pid" "$JOURNAL" "$STATE_ROOT"
printf 'AUTOMATED_TRADING=DISABLED scheduled_learning=DISABLED ports_8765_8767=UNCHANGED\n'
