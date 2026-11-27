#!/usr/bin/env bash
# Approval-gated retirement of the 8769 gap-repair TEST service (Phase 5).
#
#   retire_service_8769.sh check
#       Read-only. Runs every gate and prints the exact approval token.
#   retire_service_8769.sh retire --approve <token printed by check>
#       Re-runs every gate, captures and VALIDATES the exact launch state,
#       repeats every dependency gate (supervisors/config included)
#       immediately before stopping, then sends ONE SIGTERM through a pidfd
#       (no PID-reuse race) and waits. A zombie counts as exited. Never
#       force-kills. Afterwards enforces: listener gone, journal handles
#       released, protected identities unchanged, 8768 healthy/signals-only.
#   retire_service_8769.sh rollback <state dir> --approve ROLLBACK-8769
#       Restarts the service with the captured argv, working directory and
#       environment and verifies listener, argv, commit, journal, signals-only
#       configuration, health, readiness, workers, and that 8765/8768 still
#       match the SAVED pre-retirement snapshot.
#
# FAIL CLOSED: any check that cannot be completed (missing tool, timeout,
# permission error, unreadable entry, unexpected value) blocks the operation.
# Exit codes: 0 ok; 2 gate failed / blocked (service not stopped or started);
# 3 SIGTERM sent but the process did not exit (manual decision; never
# force-killed); 4 post-stop or rollback verification failed.
#
# Never touches protected services (8765 production, 8768 preview) beyond
# reading their identity and 8768's /healthz; never deletes, moves or writes
# any journal; never prints environment values or credentials.
set -Euo pipefail
umask 077

# ---- configuration (environment overrides exist ONLY for local rehearsal) --
PORT=${RETIRE_PORT:-8769}
EXPECT_PID=${RETIRE_EXPECT_PID:-475993}
EXPECT_CWD=${RETIRE_EXPECT_CWD:-/home/opc/kalshi-gap-repair-stage}
EXPECT_COMMIT=${RETIRE_EXPECT_COMMIT:-7ba1763}
JOURNAL_NAME=${RETIRE_JOURNAL_NAME:-repair-test-8769.db}
read -r -a PROTECTED <<<"${RETIRE_PROTECTED_PORTS:-8765 8768}"
read -r -a HEALTH_PORTS <<<"${RETIRE_HEALTH_PROBE_PORTS:-8768}"
STATE_ROOT=${RETIRE_STATE_ROOT:-/home/opc/.kalshi-deploy/retire-8769}
DEPLOY_LOCK=${RETIRE_DEPLOY_LOCK:-/home/opc/.kalshi-deploy/preview-8768/deploy.lock}
PYTHON=${RETIRE_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
CLIENT_SAMPLES=${RETIRE_CLIENT_SAMPLES:-3}
CLIENT_INTERVAL=${RETIRE_CLIENT_INTERVAL:-10}
STOP_WAIT=${RETIRE_STOP_WAIT:-30}
RESPAWN_WATCH=${RETIRE_RESPAWN_WATCH:-15}
READY_TIMEOUT=${RETIRE_READY_TIMEOUT:-300}
REQUIRE_READY=${RETIRE_REQUIRE_READY:-1}
# Accepted direct parents: an orphaned nohup process is adopted by init or a
# systemd manager. Any other living parent may be a restart loop.
ALLOWED_PARENTS=${RETIRE_ALLOWED_PARENTS:-systemd init}
SEARCH_HOME=${RETIRE_SEARCH_HOME:-$HOME}
SYSTEM_DIRS=${RETIRE_SYSTEM_DIRS:-/etc/systemd/system /usr/lib/systemd/system /lib/systemd/system /run/systemd/system /etc/systemd/user /usr/lib/systemd/user /etc/crontab /etc/cron.d /etc/cron.hourly /etc/cron.daily /etc/cron.weekly /etc/cron.monthly /var/spool/cron /etc/anacrontab /etc/nginx /etc/caddy /etc/httpd /etc/haproxy /etc/cloudflared /etc/supervisord.conf /etc/supervisord.d /etc/rc.local}

T() { timeout -k 2 "${TMO:-10}" "$@"; }
ere() {  # escape a literal string for grep -E (pure bash)
  local in=$1 out="" c i
  for ((i=0; i<${#in}; i++)); do
    c=${in:i:1}
    case $c in '.'|'['|']'|'\'|'*'|'^'|'$'|'('|')'|'+'|'?'|'{'|'}'|'|') out+="\\$c" ;; *) out+=$c ;; esac
  done
  printf '%s' "$out"
}
PORT_RE="(^|[^0-9])${PORT}([^0-9]|$)"
say() { printf '%s\n' "$*"; }
FAILURES=()
gate_fail() { FAILURES+=("$*"); say "GATE FAIL: $*"; }
gate_ok() { say "gate ok: $*"; }
die() { say "BLOCKED: $*"; exit 2; }

for tool in ss timeout flock awk grep sha256sum stat cmp; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool missing: $tool (cannot verify; fail closed)"
done
[[ -x $PYTHON ]] || die "python not found at $PYTHON (needed for pidfd stop and health checks)"
SUDO_OK=0 SUDO=()
if [[ $(id -u) == 0 ]]; then SUDO_OK=1
elif timeout 5 sudo -n true >/dev/null 2>&1; then SUDO=(sudo -n); SUDO_OK=1
fi
# With hidepid, other users' processes are invisible: the /proc scans would
# be incomplete, so they cannot be verified without root/sudo.
if grep -qE '^proc /proc proc .*hidepid=([12]|invisible|noaccess)' /proc/mounts && (( SUDO_OK == 0 )); then
  die "/proc is mounted with hidepid and no passwordless sudo: process checks would be incomplete"
fi

# ---- identity helpers -------------------------------------------------------
listener_pids() {  # all PIDs listening on a port; non-zero only if ss fails
  local out; out=$(T ss -Hltnp "sport = :$1") || return 1
  { grep -oE 'pid=[0-9]+' <<<"$out" || true; } | cut -d= -f2 | sort -u
}
proc_stat() {  # pid -> "state starttime" (fields 3 and 22), non-zero if gone/unreadable
  local raw; raw=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  raw=${raw##*) }
  awk '{print $1, $20}' <<<"$raw"
}
starttime() { local s; s=$(proc_stat "$1") || return 1; printf '%s' "${s#* }"; }
alive_same() {  # pid start -> 0 while the SAME process exists and is not a zombie
  local s; s=$(proc_stat "$1") || return 1
  [[ ${s#* } == "$2" && ${s%% *} != Z && ${s%% *} != X ]]
}
identity() {  # pid|starttime|cwd|sha256(argv)
  local pid=$1 st cwd sum
  st=$(starttime "$pid") || return 1
  cwd=$(readlink -f "/proc/$pid/cwd") || return 1
  sum=$(sha256sum <"/proc/$pid/cmdline") || return 1
  printf '%s|%s|%s|%s\n' "$pid" "$st" "$cwd" "${sum%% *}"
}
protected_snapshot() {  # one line per protected port: port=identity (or a failure marker)
  local port pids id
  for port in "${PROTECTED[@]}"; do
    pids=$(listener_pids "$port") || { echo "$port=SS_FAILED"; continue; }
    [[ $(wc -w <<<"$pids") == 1 ]] || { echo "$port=NOT_EXACTLY_ONE_LISTENER:$(tr '\n' ',' <<<"$pids")"; continue; }
    id=$(identity "$pids") || id=UNREADABLE
    echo "$port=$id"
  done
}
snapshot_valid() {  # every line must be port=pid|start|cwd|sha
  local line n=0
  while IFS= read -r line; do
    [[ $line =~ ^[0-9]+=[0-9]+\|[0-9]+\|/[^|]*\|[0-9a-f]{64}$ ]] || return 1
    n=$((n + 1))
  done <<<"$1"
  (( n == ${#PROTECTED[@]} ))
}
http_json() {  # port path -> "code json"; read-only GET to localhost
  "$PYTHON" - "$1" "$2" <<'PY'
import json, sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
port, path = sys.argv[1], sys.argv[2]
try:
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        print(r.status, json.dumps(json.load(r)))
except HTTPError as e:
    try: body = json.load(e)
    except Exception: body = {}
    print(e.code, json.dumps(body))
except Exception as e:
    print("000", json.dumps({"error": type(e).__name__}))
PY
}
healthy() {  # port -> 0 only for HTTP 200, HEALTHY, workers healthy, signals_only
  "$PYTHON" -c '
import json, sys
code, body = sys.argv[1].split(" ", 1)
b = json.loads(body)
ok = (code == "200" and b.get("status") == "HEALTHY" and b.get("signals_only") is True
      and (b.get("workers") or {}).get("healthy") is True)
sys.exit(0 if ok else 1)' "$(http_json "$1" /healthz)"
}
ready() {  # port -> 0 only for HTTP 200, READY, signals_only
  "$PYTHON" -c '
import json, sys
code, body = sys.argv[1].split(" ", 1)
b = json.loads(body)
sys.exit(0 if code == "200" and b.get("status") == "READY" and b.get("signals_only") is True else 1)' "$(http_json "$1" /readyz)"
}
journal_holders() {  # files... -> PIDs holding any of them; non-zero if not verifiable
  local out rc err
  if command -v fuser >/dev/null 2>&1; then
    out=$(TMO=20 T "${SUDO[@]}" fuser "$@" 2>/dev/null); rc=$?
    (( rc == 0 || rc == 1 )) || return 1            # 1 = nobody; anything else = cannot verify
    tr -cs '0-9' '\n' <<<"$out" | grep -v '^$' | sort -u
    return 0
  fi
  # Fallback: descriptor scan. Permission errors fail; vanished processes are benign.
  out=$(TMO=30 T "${SUDO[@]}" find /proc/[0-9]*/fd -maxdepth 1 \( -lname "$1" -o -lname "$1-wal" -o -lname "$1-shm" -o -lname "$1.dashboard.lock" \) 2>&1); rc=$?
  err=$(grep -v '^/proc/' <<<"$out" | grep -v 'No such file or directory' || true)
  [[ -z $err ]] || return 1
  (( rc == 0 || rc == 1 )) || return 1                # 124/137 = timeout
  { grep '^/proc/' <<<"$out" || true; } | cut -d/ -f3 | sort -u
}

# ---- gates ------------------------------------------------------------------
TARGET_PID="" TARGET_START="" TARGET_JOURNAL="" TARGET_HEAD=""
gate_listener() {
  local pids argv=() i module="" port_arg="" journal="" acct=0 cwd
  pids=$(listener_pids "$PORT") || { gate_fail "listener: ss failed"; return; }
  [[ $(wc -w <<<"$pids") == 1 ]] || { gate_fail "listener: expected exactly one PID on $PORT, got '$(tr '\n' ' ' <<<"$pids")'"; return; }
  [[ $pids == "$EXPECT_PID" ]] || { gate_fail "listener: PID $pids is not the expected $EXPECT_PID"; return; }
  TARGET_PID=$pids
  TARGET_START=$(starttime "$pids") || { gate_fail "listener: cannot read start time"; TARGET_PID=""; return; }
  cwd=$(readlink -f "/proc/$pids/cwd") || { gate_fail "listener: cannot read cwd"; return; }
  [[ $cwd == "$EXPECT_CWD" ]] || { gate_fail "listener: cwd is not $EXPECT_CWD"; return; }
  mapfile -d '' -t argv <"/proc/$pids/cmdline" || { gate_fail "listener: cannot read argv"; return; }
  (( ${#argv[@]} > 0 )) || { gate_fail "listener: empty argv"; return; }
  for ((i=0; i<${#argv[@]}; i++)); do
    case ${argv[i]} in
      -m) module=${argv[i+1]:-} ;;
      --port) port_arg=${argv[i+1]:-} ;;
      --journal) journal=${argv[i+1]:-} ;;
      --account-readonly|--private-dashboard|--require-readonly-scope) acct=1 ;;
    esac
  done
  [[ $module == src.signals_display.server ]] || { gate_fail "listener: module is '$module'"; return; }
  [[ $port_arg == "$PORT" ]] || { gate_fail "listener: --port is '$port_arg'"; return; }
  [[ $(basename "$journal") == "$JOURNAL_NAME" ]] || { gate_fail "listener: journal is '$(basename "$journal")'"; return; }
  (( acct == 0 )) || { gate_fail "listener: account/private flags present (not a signals-only test service)"; return; }
  if [[ $journal == /* ]]; then TARGET_JOURNAL=$journal; else TARGET_JOURNAL=$EXPECT_CWD/$journal; fi
  TARGET_HEAD=$(TMO=5 T git -C "$EXPECT_CWD" rev-parse HEAD) || { gate_fail "listener: cannot read git HEAD"; return; }
  [[ $TARGET_HEAD =~ ^[0-9a-f]{40}$ && $TARGET_HEAD == "$EXPECT_COMMIT"* ]] || { gate_fail "listener: HEAD '$TARGET_HEAD' is not $EXPECT_COMMIT"; return; }
  gate_ok "listener PID $TARGET_PID start=$TARGET_START cwd=$EXPECT_CWD commit=${TARGET_HEAD:0:12} journal=$JOURNAL_NAME signals-only"
}

PARENT_LAUNCHER="" PARENT_LAUNCHER_START=""
# A living parent is accepted ONLY as a strictly one-shot launcher:
#   * `bash|sh|dash -c <one command string>`, itself orphaned (its PPID is 1);
#   * its ONLY child is the service;
#   * the command launches src.signals_display.server on exactly this port;
#   * the command has no loop/retry construct and runs no other script.
# Anything else may be a restart supervisor and fails closed. The launcher's
# full command is printed (credential-like text redacted) for the record.
launcher_reason() {  # pid -> prints a refusal reason, or nothing if one-shot
  local pp=$1 comm gp kids script
  local -a argv=()
  comm=$(cat "/proc/$pp/comm") || { echo "cannot read parent $pp"; return; }
  [[ $comm =~ ^(bash|sh|dash)$ ]] || { echo "parent $pp is '$comm', not a shell launcher"; return; }
  gp=$(awk '/^PPid:/{print $2}' "/proc/$pp/status") || { echo "cannot read parent's parent"; return; }
  [[ $gp == 1 ]] || { echo "launcher $pp still has a living parent $gp (session or supervisor)"; return; }
  mapfile -d '' -t argv <"/proc/$pp/cmdline" || { echo "cannot read launcher command"; return; }
  (( ${#argv[@]} == 3 )) && [[ ${argv[1]} == -c ]] || { echo "launcher is not '<shell> -c <command>' (${#argv[@]} arguments)"; return; }
  script=${argv[2]}
  say >&2 "parent launcher $pp command (redacted): $(sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:])[^[:space:]]*/\1[REDACTED]/Ig' <<<"$script" | head -c 1500)"
  if grep -qE '(^|[^[:alnum:]_])(while|until|for|select|trap|sleep|watch|restart|retry|respawn|loop|wait|timeout|supervise|systemd-run|setsid)([^[:alnum:]_]|$)' <<<"$script"; then
    echo "launcher command contains a loop/retry/supervision construct"; return
  fi
  if grep -qE '\.sh([^[:alnum:]_]|$)|(^|[;&|[:space:]])(bash|sh|dash|source|\.)[[:space:]]' <<<"$script"; then
    echo "launcher command runs another script or shell"; return
  fi
  [[ $script == *src.signals_display.server* && $script =~ $PORT_RE ]] ||
    { echo "launcher command does not launch src.signals_display.server on port $PORT"; return; }
  # The server launch must appear exactly once and be the LAST command: after
  # it only redirections may follow, so nothing can run once the server exits.
  local count tail
  count=$(grep -o 'signals_display\.server' <<<"$script" | wc -l)
  (( count == 1 )) || { echo "launcher starts the server $count times"; return; }
  tail=${script#*signals_display.server}
  tail=$(sed -E 's/[0-9]*>&[0-9]+//g; s/[0-9]*>>?[[:space:]]*[^[:space:];&|]+//g; s/<[[:space:]]*[^[:space:];&|]+//g' <<<"$tail")
  # One trailing `&` (and nothing after it) backgrounds the WHOLE list: bash
  # runs it in a subshell that waits for the server and then has nothing left
  # to run. That subshell is the living parent seen on Oracle (PPID 1,
  # do_wait). Anything after the `&`, or `&&`/`|`/`;`, is still refused.
  tail=$(sed -E 's/[[:space:]]&[[:space:]]*$//' <<<"$tail")
  [[ $tail != *[\;\&\|]* && $(wc -l <<<"$tail") -le 1 ]] ||
    { echo "launcher runs further commands after the server (would restart or continue after it exits)"; return; }
  kids=$(ps -o pid= --ppid "$pp" 2>/dev/null | tr -d ' ' | grep -v '^$' || true)
  [[ $kids == "$TARGET_PID" ]] || { echo "launcher children are '$(tr '\n' ' ' <<<"$kids")', not only $TARGET_PID"; return; }
}

gate_parent() {
  local pp comm reason
  pp=$(awk '/^PPid:/{print $2}' "/proc/$TARGET_PID/status") || { gate_fail "parent: cannot read status"; return; }
  [[ $pp =~ ^[0-9]+$ ]] || { gate_fail "parent: unreadable parent PID"; return; }
  comm=$(cat "/proc/$pp/comm") || { gate_fail "parent: cannot read parent $pp"; return; }
  if [[ $pp == 1 ]] || grep -qwF -- "$comm" <<<"$ALLOWED_PARENTS"; then
    PARENT_LAUNCHER="" PARENT_LAUNCHER_START=""
    gate_ok "parent $pp ($comm): no restart loop above the service"
    return
  fi
  reason=$(launcher_reason "$pp")
  if [[ -n $reason ]]; then
    PARENT_LAUNCHER="" PARENT_LAUNCHER_START=""
    gate_fail "parent: living parent $pp ($comm) could be a restart loop: $reason"
    return
  fi
  PARENT_LAUNCHER=$pp
  PARENT_LAUNCHER_START=$(starttime "$pp") || { gate_fail "parent: cannot read launcher start time"; return; }
  gate_ok "parent $pp ($comm) is a one-shot launcher (orphaned, single child, no loop); it must exit with the service"
}

gate_clients() {  # $1 = samples
  local n out count
  for ((n=1; n<=$1; n++)); do
    out=$(T ss -Htn state established "( sport = :$PORT )") || { gate_fail "clients: ss failed"; return; }
    count=$(grep -c . <<<"$out" || true)
    (( count == 0 )) || { gate_fail "clients: $count established connection(s) to $PORT"; return; }
    (( n < $1 )) && sleep "$CLIENT_INTERVAL"
  done
  gate_ok "clients: 0 established connections in $1 sample(s)"
}

JOURNAL_FILES=()
gate_journal_users() {
  local f mode users other=() p comm
  [[ -f $TARGET_JOURNAL ]] || { gate_fail "journal: $TARGET_JOURNAL does not exist"; return; }
  JOURNAL_FILES=()
  for f in "$TARGET_JOURNAL" "$TARGET_JOURNAL-wal" "$TARGET_JOURNAL-shm" "$TARGET_JOURNAL.dashboard.lock"; do
    [[ -e $f ]] && JOURNAL_FILES+=("$f")
  done
  if (( SUDO_OK == 0 )); then
    # Without root only this user's processes are visible; complete only if
    # no other user can open the files at all.
    for f in "${JOURNAL_FILES[@]}"; do
      mode=$(stat -c %a "$f") || { gate_fail "journal: cannot stat $f"; return; }
      (( (8#$mode & 8#077) == 0 )) || { gate_fail "journal: $f is group/other accessible; other users' processes cannot be inspected without sudo"; return; }
    done
  fi
  users=$(journal_holders "$TARGET_JOURNAL" "${JOURNAL_FILES[@]:1}") || { gate_fail "journal: holder check failed, timed out or was incomplete"; return; }
  for p in $users; do
    [[ $p == "$TARGET_PID" ]] && continue
    comm=$(cat "/proc/$p/comm" 2>/dev/null || echo exited)
    other+=("$p:$comm")
  done
  (( ${#other[@]} == 0 )) || { gate_fail "journal: other processes hold it open: ${other[*]}"; return; }
  grep -qx "$TARGET_PID" <<<"$users" || { gate_fail "journal: the service itself does not hold its journal (unexpected)"; return; }
  gate_ok "journal: held only by PID $TARGET_PID"
}

gate_process_refs() {  # any other process naming the port, journal or directory
  local p args refs=() unreadable=()
  for p in /proc/[0-9]*; do
    p=${p#/proc/}
    [[ $p == "$TARGET_PID" || $p == "$$" || $p == "$BASHPID" ]] && continue
    # The verified one-shot launcher (same PID AND start time) is the only exemption.
    [[ -n $PARENT_LAUNCHER && $p == "$PARENT_LAUNCHER" && $(starttime "$p" 2>/dev/null) == "$PARENT_LAUNCHER_START" ]] && continue
    if ! args=$(tr '\0' ' ' 2>/dev/null <"/proc/$p/cmdline"); then
      [[ -d /proc/$p ]] && unreadable+=("$p")            # exists but unreadable: cannot verify
      continue                                            # vanished meanwhile: benign
    fi
    [[ -z $args || $args == *retire_service_8769* ]] && continue
    if [[ $args == *"$JOURNAL_NAME"* || $args == *"$EXPECT_CWD"* || $args =~ $PORT_RE ]]; then
      refs+=("$p:$(cat "/proc/$p/comm" 2>/dev/null || echo exited)")
    fi
  done
  (( ${#unreadable[@]} == 0 )) || { gate_fail "process references: cannot read command lines of PIDs ${unreadable[*]}"; return; }
  (( ${#refs[@]} == 0 )) || { gate_fail "process references to $PORT/journal/directory: ${refs[*]} (tunnel, proxy, restart loop or reader)"; return; }
  gate_ok "no other process references $PORT, $JOURNAL_NAME or $EXPECT_CWD"
}

search_refs() {  # paths... -> matching files; non-zero if anything could not be searched
  local out rc
  out=$(TMO=10 T grep -rlsE -- "$SUP_PATTERN" "$@"); rc=$?
  if (( rc == 2 )) && (( ${#SUDO[@]} )); then
    out=$(TMO=10 T "${SUDO[@]}" grep -rlsE -- "$SUP_PATTERN" "$@"); rc=$?
  fi
  (( rc == 0 || rc == 1 )) || return 1      # 2 = unreadable/permission error, 124/137 = timeout
  printf '%s' "$out"
}
gate_supervisors() {
  local d hits out rc a b scripts=() f scope
  a=$(ere "$JOURNAL_NAME"); b=$(ere "$EXPECT_CWD")
  [[ -n $a && -n $b && -n $PORT ]] || { gate_fail "supervisors: could not build the search pattern"; return; }
  SUP_PATTERN="${PORT_RE}|${a}|${b}"
  for d in $SYSTEM_DIRS "$SEARCH_HOME/.config/systemd/user" "$SEARCH_HOME/.cloudflared" "$SEARCH_HOME/.ssh/config" \
           "$SEARCH_HOME/bin" "$SEARCH_HOME/.bashrc" "$SEARCH_HOME/.bash_profile" "$SEARCH_HOME/.profile"; do
    [[ -e $d || -L $d ]] || continue
    hits=$(search_refs "$d") || { gate_fail "supervisors: could not fully search $d (permission error, unreadable entry or timeout)"; return; }
    [[ -z $hits ]] || { gate_fail "supervisors: references in $(tr '\n' ' ' <<<"$hits")"; return; }
  done
  for f in "$SEARCH_HOME"/*.sh "$SEARCH_HOME"/*.py "$EXPECT_CWD"/*.sh "$EXPECT_CWD"/scripts/*.sh; do
    [[ -e $f ]] && scripts+=("$f")
  done
  if (( ${#scripts[@]} )); then
    hits=$(search_refs "${scripts[@]}") || { gate_fail "supervisors: could not fully search launch scripts"; return; }
    [[ -z $hits ]] || { gate_fail "supervisors: launch/restart scripts reference it: $(tr '\n' ' ' <<<"$hits")"; return; }
  fi
  command -v crontab >/dev/null 2>&1 || { gate_fail "supervisors: crontab command missing; cannot verify user cron"; return; }
  out=$(TMO=5 T crontab -l 2>&1); rc=$?
  if (( rc == 0 )); then
    { grep -vE '^[[:space:]]*#' <<<"$out" || true; } | grep -qE -- "$SUP_PATTERN" && { gate_fail "supervisors: user crontab references it"; return; }
  elif ! [[ $rc == 1 && $out == *"no crontab"* ]]; then
    gate_fail "supervisors: cannot read user crontab (rc=$rc)"; return
  fi
  command -v systemctl >/dev/null 2>&1 || { gate_fail "supervisors: systemctl missing; cannot verify systemd"; return; }
  for scope in --system --user; do
    out=$(TMO=10 T systemctl "$scope" list-units --all --no-pager --plain 2>&1) || { gate_fail "supervisors: systemctl $scope list-units failed"; return; }
    grep -qE -- "$SUP_PATTERN" <<<"$out" && { gate_fail "supervisors: systemd $scope units reference it"; return; }
    out=$(TMO=10 T systemctl "$scope" list-timers --all --no-pager 2>&1) || { gate_fail "supervisors: systemctl $scope list-timers failed"; return; }
    grep -qE -- "$SUP_PATTERN" <<<"$out" && { gate_fail "supervisors: systemd $scope timers reference it"; return; }
  done
  gate_ok "supervisors: no unit/timer, cron, proxy, tunnel config, profile or launch script references it (sudo=$SUDO_OK)"
}

gate_protected() {  # validates the snapshot and protected health
  local p
  PROTECTED_NOW=$(protected_snapshot)
  while IFS= read -r p; do say "protected: $p"; done <<<"$PROTECTED_NOW"
  snapshot_valid "$PROTECTED_NOW" || { gate_fail "protected services are not each exactly one readable listener"; return; }
  for p in "${HEALTH_PORTS[@]}"; do
    healthy "$p" || { gate_fail "protected $p is not HEALTHY with healthy workers and signals-only"; return; }
  done
  gate_ok "protected services identified; ${HEALTH_PORTS[*]} healthy and signals-only"
}

run_gates() {  # $1 = client samples
  FAILURES=()
  gate_listener
  [[ -n $TARGET_PID && ${#FAILURES[@]} == 0 ]] || return 1
  gate_parent; gate_clients "$1"; gate_journal_users; gate_process_refs; gate_supervisors; gate_protected
  (( ${#FAILURES[@]} == 0 ))
}

token() { echo "RETIRE-8769-PID$TARGET_PID-START$TARGET_START"; }

secure_lock() {  # fd path -> create/open (0600, own, not a symlink) and lock non-blocking
  local fd=$1 path=$2 dir
  dir=$(dirname "$path")
  if [[ ! -d $dir ]]; then (umask 077; mkdir -p -- "$dir") || die "cannot create $dir"; fi
  [[ $(stat -c %u "$dir") == "$(id -u)" ]] || die "lock directory $dir is not owned by $(id -un)"
  (( (8#$(stat -c %a "$dir") & 8#022) == 0 )) || die "lock directory $dir is group/other writable"
  [[ -L $path ]] && die "lock $path is a symlink; refusing"
  if [[ ! -e $path ]]; then ( set -o noclobber; : >"$path" ) 2>/dev/null || [[ -f $path ]] || die "cannot create lock $path"; fi
  [[ -f $path && ! -L $path ]] || die "lock $path is not a regular file"
  [[ $(stat -c %u "$path") == "$(id -u)" ]] || die "lock $path is not owned by $(id -un)"
  chmod 600 "$path" || die "cannot secure lock $path"
  eval "exec $fd>>\"\$path\"" || die "cannot open lock $path"
  flock -n "$fd" || die "lock $path is held (another retirement, rollback or preview-8768 deployment is running)"
}
acquire_locks() {
  secure_lock 8 "$STATE_ROOT/retire.lock"
  secure_lock 7 "$DEPLOY_LOCK"          # always: no preview-8768 deployment may run concurrently
  say "locks held: $STATE_ROOT/retire.lock, $DEPLOY_LOCK"
}

validate_state() {  # dir -> 0 only if every required file exists, is 0600 and is consistent
  local S=$1 f
  [[ -d $S && ! -L $S && $(stat -c %a "$S") == 700 && $(stat -c %u "$S") == "$(id -u)" ]] || { say "state: directory not 0700/owned"; return 1; }
  for f in argv env cwd journal commit port identity journal-files protected-before; do
    [[ -f $S/$f && ! -L $S/$f && -s $S/$f ]] || { say "state: $f missing or empty"; return 1; }
    [[ $(stat -c %a "$S/$f") == 600 ]] || { say "state: $f is not 0600"; return 1; }
  done
  [[ $(<"$S/port") =~ ^[0-9]+$ ]] || { say "state: bad port"; return 1; }
  [[ $(<"$S/commit") =~ ^[0-9a-f]{40}$ ]] || { say "state: bad commit"; return 1; }
  [[ $(<"$S/cwd") == /* && -d $(<"$S/cwd") ]] || { say "state: bad cwd"; return 1; }
  [[ $(<"$S/journal") == /* ]] || { say "state: bad journal path"; return 1; }
  [[ $(<"$S/identity") =~ ^[0-9]+\|[0-9]+\|/[^|]*\|[0-9a-f]{64}$ ]] || { say "state: bad identity"; return 1; }
  snapshot_valid "$(<"$S/protected-before")" || { say "state: bad protected snapshot"; return 1; }
  grep -qz . "$S/argv" && grep -qz . "$S/env" || { say "state: argv/env empty"; return 1; }
  return 0
}

pidfd_term() {  # pid starttime -> SIGTERM through a pidfd after re-verifying start time and state
  "$PYTHON" - "$1" "$2" <<'PY'
import os, signal, sys
pid, start = int(sys.argv[1]), sys.argv[2]
fd = os.pidfd_open(pid)            # from here the PID cannot be reused while fd is open
try:
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    if fields[19] != start or fields[0] in ("Z", "X"):
        print("PID_REUSED_OR_EXITED"); sys.exit(5)
    signal.pidfd_send_signal(fd, signal.SIGTERM)
    print("SIGTERM_SENT")
finally:
    os.close(fd)
PY
}

cmd=${1:-}
case $cmd in
  check)
    say "== CHECK (read-only) $(date -u +%FT%TZ)"
    if run_gates "$CLIENT_SAMPLES"; then
      say "ALL GATES PASSED"
      say "APPROVAL TOKEN: $(token)"
      exit 0
    fi
    say "CHECK FAILED (${#FAILURES[@]} gate(s)); nothing was changed"; exit 2 ;;

  retire)
    [[ ${2:-} == --approve && -n ${3:-} && $# -eq 3 ]] || die "usage: $0 retire --approve <token from check>"
    acquire_locks
    say "== RETIRE $(date -u +%FT%TZ)"
    run_gates "$CLIENT_SAMPLES" || { say "nothing was changed"; exit 2; }
    [[ $3 == "$(token)" ]] || die "approval token does not match the current service identity ($(token))"
    PROTECTED_BEFORE=$PROTECTED_NOW
    S="$STATE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-pid$TARGET_PID"
    mkdir -m 700 "$S" || die "cannot create state directory"
    cp -- "/proc/$TARGET_PID/cmdline" "$S/argv" || die "capture failed: argv"
    cp -- "/proc/$TARGET_PID/environ" "$S/env" || die "capture failed: environment"
    printf '%s\n' "$EXPECT_CWD" >"$S/cwd" || die "capture failed: cwd"
    printf '%s\n' "$TARGET_JOURNAL" >"$S/journal" || die "capture failed: journal"
    printf '%s\n' "$TARGET_HEAD" >"$S/commit" || die "capture failed: commit"
    printf '%s\n' "$PORT" >"$S/port" || die "capture failed: port"
    identity "$TARGET_PID" >"$S/identity" || die "capture failed: identity"
    # SQLite may delete -wal/-shm between the journal gate and this capture (last
    # connection closed or checkpointed). Record the files that still exist; the
    # main journal itself is required.
    [[ -f $TARGET_JOURNAL ]] || die "capture failed: journal $TARGET_JOURNAL disappeared"
    present=()
    for f in "${JOURNAL_FILES[@]}"; do [[ -e $f ]] && present+=("$f"); done
    stat -c '%n %i %s %Y' -- "${present[@]}" >"$S/journal-files" || die "capture failed: journal files"
    printf '%s\n' "$PROTECTED_BEFORE" >"$S/protected-before" || die "capture failed: protected snapshot"
    if [[ -n $PARENT_LAUNCHER ]]; then
      cp -- "/proc/$PARENT_LAUNCHER/cmdline" "$S/launcher-argv" || die "capture failed: launcher argv"
      printf '%s|%s\n' "$PARENT_LAUNCHER" "$PARENT_LAUNCHER_START" >"$S/launcher-identity" || die "capture failed: launcher identity"
    fi
    chmod 600 "$S"/* || die "capture failed: permissions"
    validate_state "$S" || die "captured state failed validation; nothing was stopped (state kept in $S)"
    # /proc files report size 0, so compare the streamed contents, not the files.
    cat "/proc/$TARGET_PID/cmdline" | cmp -s "$S/argv" - || die "captured argv no longer matches the running process"
    [[ $(cut -d'|' -f1,2 "$S/identity") == "$TARGET_PID|$TARGET_START" ]] || die "captured identity mismatch"
    say "launch state captured and validated in $S (environment never printed)"

    say "-- final re-check immediately before SIGTERM (all dependency gates repeated)"
    FAILURES=()
    pid_now=$(listener_pids "$PORT") || gate_fail "listener: ss failed"
    [[ ${pid_now:-} == "$TARGET_PID" ]] && alive_same "$TARGET_PID" "$TARGET_START" || gate_fail "listener identity changed"
    gate_parent; gate_supervisors; gate_journal_users; gate_process_refs
    [[ $(protected_snapshot) == "$PROTECTED_BEFORE" ]] || gate_fail "protected services changed before the stop"
    for p in "${HEALTH_PORTS[@]}"; do healthy "$p" || gate_fail "protected $p not healthy before the stop"; done
    gate_clients 1                                           # last check before the signal
    (( ${#FAILURES[@]} == 0 )) || { say "final re-check failed; nothing was stopped (state kept in $S)"; exit 2; }
    # Residual race (documented): a client could connect in the milliseconds
    # between this re-check and the signal. The PID cannot be reused (pidfd).
    result=$(pidfd_term "$TARGET_PID" "$TARGET_START") || { say "stop not sent: ${result:-error}"; exit 2; }
    say "$result to PID $TARGET_PID"
    for ((i=0; i<STOP_WAIT*2; i++)); do
      alive_same "$TARGET_PID" "$TARGET_START" || break       # gone, zombie or replaced = exited
      sleep 0.5
    done
    if alive_same "$TARGET_PID" "$TARGET_START"; then
      say "RETIRE_INCOMPLETE: PID $TARGET_PID still running after ${STOP_WAIT}s; NOT force-killed. Decide manually."
      exit 3
    fi
    say "process exited$( [[ $(proc_stat "$TARGET_PID" 2>/dev/null) == Z* ]] && echo ' (zombie awaiting its parent: treated as exited)')"
    rc=0
    if [[ -n $PARENT_LAUNCHER ]]; then
      for ((i=0; i<20; i++)); do alive_same "$PARENT_LAUNCHER" "$PARENT_LAUNCHER_START" || break; sleep 0.5; done
      if alive_same "$PARENT_LAUNCHER" "$PARENT_LAUNCHER_START"; then
        say "POST FAIL: launcher $PARENT_LAUNCHER is still alive after the service exited (possible restart loop); NOT killed"; rc=4
      else
        say "post: one-shot launcher $PARENT_LAUNCHER exited with the service"
      fi
    fi
    # The listener and journal handles must be released (bounded wait).
    for ((i=0; i<20; i++)); do
      l=$(listener_pids "$PORT") || { l=SS_FAILED; }
      h=$(journal_holders "$TARGET_JOURNAL" "${JOURNAL_FILES[@]:1}") || h=CHECK_FAILED
      [[ -z $l && -z $h ]] && break
      sleep 0.5
    done
    [[ -z $l ]] && say "post: port $PORT free" || { say "POST FAIL: port $PORT listener: $l"; rc=4; }
    # Respawn watch: nothing may start listening on the port again.
    for ((i=0; i<RESPAWN_WATCH; i++)); do
      sleep 1
      l2=$(listener_pids "$PORT") || l2=SS_FAILED
      [[ -z $l2 ]] || { say "POST FAIL: a process ($l2) started listening on $PORT again (respawn); NOT killed"; rc=4; break; }
    done
    (( rc == 0 )) && say "post: no respawn on $PORT for ${RESPAWN_WATCH}s"
    [[ -z $h ]] && say "post: journal handles released" || { say "POST FAIL: journal still held/unverifiable: $h"; rc=4; }
    after=$(protected_snapshot)
    printf '%s\n' "$after" >"$S/protected-after" && chmod 600 "$S/protected-after"
    if [[ $after == "$PROTECTED_BEFORE" ]]; then say "post: protected services unchanged"
    else say "POST FAIL: protected services changed"; diff <(echo "$PROTECTED_BEFORE") <(echo "$after"); rc=4; fi
    for p in "${HEALTH_PORTS[@]}"; do
      if healthy "$p"; then say "post: $p HEALTHY, workers healthy, signals-only"
      else say "POST FAIL: $p not healthy/signals-only: $(http_json "$p" /healthz | cut -c1-200)"; rc=4; fi
    done
    [[ -s $TARGET_JOURNAL ]] && say "post: journal preserved ($(stat -c '%s bytes' "$TARGET_JOURNAL"))" || { say "POST FAIL: journal missing"; rc=4; }
    printf 'result=%s finished=%s\n' "$([[ $rc == 0 ]] && echo RETIRED || echo POST_CHECK_FAILED)" "$(date -u +%FT%TZ)" >"$S/result"
    chmod 600 "$S/result"
    (( rc == 0 )) && say "RETIRED: PID $TARGET_PID stopped gracefully. Rollback: $0 rollback $S --approve ROLLBACK-8769"
    exit "$rc" ;;

  rollback)
    S=${2:-}
    [[ -n $S && ${3:-} == --approve && ${4:-} == ROLLBACK-8769 && $# -eq 4 ]] || die "usage: $0 rollback <state dir> --approve ROLLBACK-8769"
    acquire_locks
    say "== ROLLBACK $(date -u +%FT%TZ) from $S"
    validate_state "$S" || die "state directory failed validation; nothing started"
    port=$(<"$S/port"); journal=$(<"$S/journal"); cwd=$(<"$S/cwd"); commit=$(<"$S/commit")
    SAVED=$(<"$S/protected-before")
    l=$(listener_pids "$port") || die "ss failed; nothing started"
    [[ -z $l ]] || die "port $port is already in use; nothing started"
    [[ -s $journal ]] || die "journal $journal missing; nothing started"
    h=$(journal_holders "$journal" "$journal-wal" "$journal-shm" "$journal.dashboard.lock") || die "journal holder check failed; nothing started"
    [[ -z $h ]] || die "journal is held by PID(s) $h; nothing started"
    head=$(TMO=5 T git -C "$cwd" rev-parse HEAD) || die "cannot read commit; nothing started"
    [[ $head == "$commit" ]] || die "checkout $cwd is no longer at $commit; nothing started"
    now=$(protected_snapshot)
    [[ $now == "$SAVED" ]] || { say "protected services differ from the saved pre-retirement snapshot:"; diff <(echo "$SAVED") <(echo "$now"); die "nothing started (manual review required)"; }
    say "pre-rollback: protected services match the saved pre-retirement snapshot"
    mapfile -d '' -t ENVV <"$S/env"; mapfile -d '' -t ARGV <"$S/argv"
    ( cd "$cwd" && exec setsid nohup env -i "${ENVV[@]}" "${ARGV[@]}" \
        >>"$(dirname "$journal")/restart-$port.log" 2>&1 </dev/null 7>&- 8>&- ) &
    say "relaunched; verifying (up to ${READY_TIMEOUT}s)"
    deadline=$((SECONDS + READY_TIMEOUT)) ok=0 new=""
    while (( SECONDS < deadline )); do
      new=$(listener_pids "$port" || true)
      if [[ $(wc -w <<<"$new") == 1 ]] && healthy "$port"; then
        if [[ $REQUIRE_READY == 0 ]] || ready "$port"; then ok=1; break; fi
      fi
      sleep 3
    done
    rc=0
    (( ok )) || { say "VERIFY FAIL: not healthy/ready within ${READY_TIMEOUT}s (process left running for inspection)"; rc=4; }
    if [[ $(wc -w <<<"$new") == 1 ]]; then
      mapfile -d '' -t NOWV <"/proc/$new/cmdline"
      same=$(( ${#ARGV[@]} == ${#NOWV[@]} ))
      for ((i=0; same && i<${#ARGV[@]}; i++)); do [[ ${ARGV[i]} == "${NOWV[i]}" ]] || same=0; done
      if (( same )); then say "verify: argv identical, ${#NOWV[@]} arguments (same program, module, port, journal, signals-only flags)"
      else say "VERIFY FAIL: argv differs (saved ${#ARGV[@]} args, running ${#NOWV[@]})"; rc=4; fi
      [[ $(readlink -f "/proc/$new/cwd") == "$cwd" ]] && say "verify: cwd $cwd" || { say "VERIFY FAIL: cwd differs"; rc=4; }
      [[ $(T git -C "$cwd" rev-parse HEAD) == "$commit" ]] && say "verify: commit ${commit:0:12}" || { say "VERIFY FAIL: commit differs"; rc=4; }
      if printf '%s\n' "${NOWV[@]}" | grep -qxE -- '--account-readonly|--private-dashboard|--require-readonly-scope'; then
        say "VERIFY FAIL: account flags present"; rc=4
      else say "verify: signals-only (no account flags)"; fi
      h=$(journal_holders "$journal" "$journal.dashboard.lock") || h=CHECK_FAILED
      [[ $h == "$new" ]] && say "verify: journal held only by restored PID $new" || { say "VERIFY FAIL: journal holders '$h'"; rc=4; }
      healthy "$port" && say "verify: HEALTHY, workers healthy, signals-only" || { say "VERIFY FAIL: health"; rc=4; }
      if [[ $REQUIRE_READY == 0 ]]; then say "verify: readyz $(http_json "$port" /readyz | cut -c1-120) [readiness NOT required: rehearsal override]"
      elif ready "$port"; then say "verify: READY"; else say "VERIFY FAIL: not READY"; rc=4; fi
    else
      say "VERIFY FAIL: expected one listener on $port, got '$new'"; rc=4
    fi
    now=$(protected_snapshot)
    [[ $now == "$SAVED" ]] && say "verify: protected services match the saved pre-retirement snapshot" || { say "VERIFY FAIL: protected services differ from the saved snapshot"; rc=4; }
    for p in "${HEALTH_PORTS[@]}"; do healthy "$p" && say "verify: protected $p healthy" || { say "VERIFY FAIL: protected $p unhealthy"; rc=4; }; done
    (( rc == 0 )) && say "ROLLBACK VERIFIED: PID $new"
    exit "$rc" ;;

  *) say "usage: $0 check | retire --approve <token> | rollback <state dir> --approve ROLLBACK-8769"; exit 2 ;;
esac
