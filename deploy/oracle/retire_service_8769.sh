#!/usr/bin/env bash
# Approval-gated retirement of the 8769 gap-repair TEST service (Phase 5).
#
#   retire_service_8769.sh check
#       Read-only. Runs every gate and prints the exact approval token.
#   retire_service_8769.sh retire --approve <token printed by check>
#       Re-runs every gate, captures the exact launch state privately,
#       re-checks clients, journal users, supervisors and listener identity
#       immediately before stopping, then sends ONE SIGTERM through a pidfd
#       (no PID-reuse race) and waits. Never force-kills.
#   retire_service_8769.sh rollback <state dir> --approve ROLLBACK-8769
#       Restarts the service with the captured argv, working directory and
#       environment and verifies listener, commit, journal, signals-only
#       configuration, health, readiness, workers and protected dashboards.
#
# FAIL CLOSED: any gate that cannot be verified (missing tool, timeout,
# unreadable location, unexpected value) stops the script before anything is
# changed. Exit codes: 0 ok, 2 gate failed/blocked (nothing changed),
# 3 stop sent but the process did not exit (manual decision; never
# force-killed), 4 post-check or rollback verification failed.
#
# Never touches protected services (8765 production, 8768 preview) beyond
# reading their identity (and 8768's /healthz); never deletes, moves or
# rewrites any journal; never prints environment values or credentials.
set -Euo pipefail
umask 077

# ---- configuration (environment overrides exist ONLY for local rehearsal) --
PORT=${RETIRE_PORT:-8769}
EXPECT_PID=${RETIRE_EXPECT_PID:-475993}
EXPECT_CWD=${RETIRE_EXPECT_CWD:-/home/opc/kalshi-gap-repair-stage}
EXPECT_COMMIT=${RETIRE_EXPECT_COMMIT:-7ba1763}
JOURNAL_NAME=${RETIRE_JOURNAL_NAME:-repair-test-8769.db}
read -r -a PROTECTED <<<"${RETIRE_PROTECTED_PORTS:-8765 8768}"
HEALTH_PROBE_PORTS=${RETIRE_HEALTH_PROBE_PORTS:-8768}
STATE_ROOT=${RETIRE_STATE_ROOT:-/home/opc/.kalshi-deploy/retire-8769}
DEPLOY_LOCK=${RETIRE_DEPLOY_LOCK:-/home/opc/.kalshi-deploy/preview-8768/deploy.lock}
PYTHON=${RETIRE_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
CLIENT_SAMPLES=${RETIRE_CLIENT_SAMPLES:-3}
CLIENT_INTERVAL=${RETIRE_CLIENT_INTERVAL:-10}
STOP_WAIT=${RETIRE_STOP_WAIT:-30}
READY_TIMEOUT=${RETIRE_READY_TIMEOUT:-300}
REQUIRE_READY=${RETIRE_REQUIRE_READY:-1}
# Comms accepted as the service's direct parent (an orphaned nohup process is
# adopted by init/systemd). Any other living parent may be a restart loop.
ALLOWED_PARENTS=${RETIRE_ALLOWED_PARENTS:-systemd init}
SEARCH_HOME=${RETIRE_SEARCH_HOME:-$HOME}
SYSTEM_DIRS=${RETIRE_SYSTEM_DIRS:-/etc/systemd/system /usr/lib/systemd/system /lib/systemd/system /run/systemd/system /etc/systemd/user /usr/lib/systemd/user /etc/crontab /etc/cron.d /etc/cron.hourly /etc/cron.daily /etc/cron.weekly /etc/cron.monthly /var/spool/cron /etc/anacrontab /etc/nginx /etc/caddy /etc/httpd /etc/haproxy /etc/cloudflared /etc/supervisord.conf /etc/supervisord.d /etc/rc.local}

T() { timeout -k 2 "${TMO:-10}" "$@"; }
ere() {  # escape a literal string for grep -E (pure bash; never empty)
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

for tool in ss timeout flock awk grep sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool missing: $tool (cannot verify; fail closed)"
done
[[ -x $PYTHON ]] || die "python not found at $PYTHON (needed for pidfd stop and health probes)"
SUDO_OK=0
if [[ $(id -u) == 0 ]]; then SUDO=(); SUDO_OK=1
elif timeout 5 sudo -n true >/dev/null 2>&1; then SUDO=(sudo -n); SUDO_OK=1
else SUDO=(); fi

# ---- identity helpers -------------------------------------------------------
listener_pids() {  # all PIDs listening on a port (fails if ss fails)
  local out; out=$(T ss -Hltnp "sport = :$1") || return 1   # only ss failure is an error
  { grep -oE 'pid=[0-9]+' <<<"$out" || true; } | cut -d= -f2 | sort -u
}
starttime() { awk '{sub(/^.*\) /, ""); print $20}' "/proc/$1/stat" 2>/dev/null; }
identity() {  # pid|starttime|cwd|sha256(argv)
  local pid=$1
  [[ -r /proc/$pid/cmdline ]] || return 1
  printf '%s|%s|%s|%s\n' "$pid" "$(starttime "$pid")" "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" \
    "$(sha256sum <"/proc/$pid/cmdline" | cut -d' ' -f1)"
}
protected_snapshot() {  # one line per protected port: port=identity
  local port pids
  for port in "${PROTECTED[@]}"; do
    pids=$(listener_pids "$port") || { echo "$port=SS_FAILED"; continue; }
    [[ $(wc -w <<<"$pids") == 1 ]] || { echo "$port=NOT_EXACTLY_ONE_LISTENER:$(tr '\n' ',' <<<"$pids")"; continue; }
    echo "$port=$(identity "$pids" || echo UNREADABLE)"
  done
}
http_json() {  # port path -> prints "code json" ; read-only GET to localhost
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

# ---- gates ------------------------------------------------------------------
TARGET_PID="" TARGET_START="" TARGET_JOURNAL=""
gate_listener() {
  local pids argv=() i module="" port_arg="" journal="" acct=0 head
  pids=$(listener_pids "$PORT") || { gate_fail "listener: ss failed"; return; }
  [[ $(wc -w <<<"$pids") == 1 ]] || { gate_fail "listener: expected exactly one PID on $PORT, got '$(tr '\n' ' ' <<<"$pids")'"; return; }
  [[ $pids == "$EXPECT_PID" ]] || { gate_fail "listener: PID $pids is not the expected $EXPECT_PID"; return; }
  TARGET_PID=$pids TARGET_START=$(starttime "$pids")
  [[ -n $TARGET_START ]] || { gate_fail "listener: cannot read start time"; return; }
  [[ $(readlink -f "/proc/$pids/cwd" 2>/dev/null) == "$EXPECT_CWD" ]] || { gate_fail "listener: cwd is not $EXPECT_CWD"; return; }
  mapfile -d '' -t argv <"/proc/$pids/cmdline" || { gate_fail "listener: cannot read argv"; return; }
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
  [[ $journal == /* ]] && TARGET_JOURNAL=$journal || TARGET_JOURNAL=$EXPECT_CWD/$journal
  head=$(TMO=5 T git -C "$EXPECT_CWD" rev-parse HEAD 2>/dev/null) || { gate_fail "listener: cannot read git HEAD"; return; }
  [[ $head == "$EXPECT_COMMIT"* ]] || { gate_fail "listener: HEAD $head is not $EXPECT_COMMIT"; return; }
  gate_ok "listener PID $TARGET_PID start=$TARGET_START cwd=$EXPECT_CWD commit=${head:0:12} journal=$JOURNAL_NAME signals-only"
}

gate_parent() {
  local pp comm
  pp=$(awk '/^PPid:/{print $2}' "/proc/$TARGET_PID/status" 2>/dev/null) || { gate_fail "parent: unreadable"; return; }
  comm=$(cat "/proc/$pp/comm" 2>/dev/null || echo "?")
  if [[ $pp == 1 ]] || grep -qwF -- "$comm" <<<"$ALLOWED_PARENTS"; then
    gate_ok "parent $pp ($comm): no restart loop above the service"
  else
    gate_fail "parent: living parent $pp ($comm) could be a restart loop; inspect it manually"
  fi
}

gate_clients() {  # $1 = samples
  local n out count
  for ((n=1; n<=$1; n++)); do
    out=$(T ss -Htn state established "( sport = :$PORT )") || { gate_fail "clients: ss failed"; return; }
    count=$(grep -c . <<<"$out" || true)
    if (( count != 0 )); then gate_fail "clients: $count established connection(s) to $PORT"; return; fi
    (( n < $1 )) && sleep "$CLIENT_INTERVAL"
  done
  gate_ok "clients: 0 established connections in $1 sample(s)"
}

gate_journal_users() {
  local files=() f users other=()
  for f in "$TARGET_JOURNAL" "$TARGET_JOURNAL-wal" "$TARGET_JOURNAL-shm" "$TARGET_JOURNAL.dashboard.lock"; do
    [[ -e $f ]] && files+=("$f")
  done
  [[ -e $TARGET_JOURNAL ]] || { gate_fail "journal: $TARGET_JOURNAL does not exist"; return; }
  if (( SUDO_OK == 0 )); then
    # Without root, only this user's processes are visible. That is complete
    # only if no other user can open the files at all.
    for f in "${files[@]}"; do
      (( ( 8#$(stat -c %a "$f") & 8#077 ) == 0 )) || { gate_fail "journal: $f is group/other accessible and other users' processes cannot be inspected without sudo"; return; }
    done
  fi
  if command -v fuser >/dev/null 2>&1; then
    users=$(TMO=20 T "${SUDO[@]}" fuser "${files[@]}" 2>/dev/null; echo "rc=$?")
    [[ $users == *"rc=124"* || $users == *"rc=137"* ]] && { gate_fail "journal: fuser timed out"; return; }
    users=$(sed 's/rc=[0-9]*//' <<<"$users" | tr -cs '0-9' '\n' | grep -v '^$' | sort -u)
  else
    # Fallback: bounded scan of open descriptors; any timeout fails closed.
    users=$(TMO=30 T "${SUDO[@]}" find /proc/[0-9]*/fd -maxdepth 1 -lname "$TARGET_JOURNAL*" 2>/dev/null | cut -d/ -f3 | sort -u) \
      || { gate_fail "journal: descriptor scan failed or timed out (fuser unavailable)"; return; }
  fi
  for f in $users; do [[ $f == "$TARGET_PID" ]] || other+=("$f:$(cat /proc/$f/comm 2>/dev/null)"); done
  (( ${#other[@]} == 0 )) || { gate_fail "journal: other processes hold it open: ${other[*]}"; return; }
  grep -qx "$TARGET_PID" <<<"$users" || { gate_fail "journal: the service itself does not hold its journal lock (unexpected)"; return; }
  gate_ok "journal: held only by PID $TARGET_PID"
}

gate_process_refs() {  # any other process naming the port, journal or directory
  local p args refs=()
  for p in /proc/[0-9]*; do
    p=${p#/proc/}
    [[ $p == "$TARGET_PID" || $p == "$$" || $p == "$BASHPID" ]] && continue
    args=$(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null) || continue
    [[ -z $args || $args == *retire_service_8769* ]] && continue
    if [[ $args == *"$JOURNAL_NAME"* || $args == *"$EXPECT_CWD"* || $args =~ $PORT_RE ]]; then
      refs+=("$p:$(cat /proc/$p/comm 2>/dev/null)")
    fi
  done
  (( ${#refs[@]} == 0 )) || { gate_fail "process references to $PORT/journal/directory: ${refs[*]} (tunnel, proxy, restart loop or reader)"; return; }
  gate_ok "no other process references $PORT, $JOURNAL_NAME or $EXPECT_CWD"
}

search_refs() {  # dir-or-file -> prints matching files; returns 1 if it cannot verify
  local d=$1 out rc
  out=$(TMO=10 T grep -rlsE -- "$SUP_PATTERN" "$d"); rc=$?
  if (( rc == 2 )) && (( SUDO_OK )) && (( ${#SUDO[@]} )); then
    out=$(TMO=10 T "${SUDO[@]}" grep -rlsE -- "$SUP_PATTERN" "$d"); rc=$?
  fi
  (( rc == 0 || rc == 1 )) || return 1      # 2 = unreadable files, 124/137 = timeout
  printf '%s' "$out"
}
gate_supervisors() {
  local d hits out
  SUP_PATTERN="${PORT_RE}|$(ere "$JOURNAL_NAME")|$(ere "$EXPECT_CWD")"
  local a b; a=$(ere "$JOURNAL_NAME"); b=$(ere "$EXPECT_CWD")
  [[ -n $a && -n $b && -n $PORT ]] || { gate_fail "supervisors: could not build the search pattern"; return; }
  for d in $SYSTEM_DIRS; do
    [[ -e $d ]] || continue
    if ! [[ -r $d && ( -f $d || -x $d ) ]] && (( ${#SUDO[@]} == 0 )) && [[ $(id -u) != 0 ]]; then
      gate_fail "supervisors: cannot read $d (no passwordless sudo); cannot verify"; return
    fi
    hits=$(search_refs "$d") || { gate_fail "supervisors: could not fully search $d (unreadable or timed out)"; return; }
    [[ -z $hits ]] || { gate_fail "supervisors: references in $(tr '\n' ' ' <<<"$hits")"; return; }
  done
  for d in "$SEARCH_HOME/.config/systemd/user" "$SEARCH_HOME/.cloudflared" "$SEARCH_HOME/.ssh/config" \
           "$SEARCH_HOME/bin" "$SEARCH_HOME/.bashrc" "$SEARCH_HOME/.bash_profile" "$SEARCH_HOME/.profile"; do
    [[ -e $d ]] || continue
    hits=$(search_refs "$d") || { gate_fail "supervisors: could not fully search $d"; return; }
    [[ -z $hits ]] || { gate_fail "supervisors: references in $hits"; return; }
  done
  # Restart/launch scripts: top level of home and of the service directory.
  out=$(TMO=10 T grep -lsE -- "$SUP_PATTERN" "$SEARCH_HOME"/*.sh "$SEARCH_HOME"/*.py "$EXPECT_CWD"/*.sh "$EXPECT_CWD"/scripts/*.sh 2>/dev/null; echo "rc=$?")
  [[ $out == *"rc=124"* ]] && { gate_fail "supervisors: script search timed out"; return; }
  hits=$(sed '/^rc=/d' <<<"$out"); [[ -z $hits ]] || { gate_fail "supervisors: launch/restart scripts reference it: $(tr '\n' ' ' <<<"$hits")"; return; }
  out=$(TMO=5 T crontab -l 2>&1; echo "rc=$?")
  if [[ $out == *"rc=0"* ]]; then
    grep -vE '^\s*#' <<<"$out" | grep -qE -- "$SUP_PATTERN" && { gate_fail "supervisors: user crontab references it"; return; }
  elif [[ $out != *"no crontab"* ]]; then gate_fail "supervisors: cannot read user crontab"; return; fi
  if command -v systemctl >/dev/null 2>&1; then
    for scope in "" "--user"; do
      out=$(TMO=10 T systemctl $scope list-units --all --no-pager --plain 2>/dev/null; TMO=10 T systemctl $scope list-timers --all --no-pager 2>/dev/null) || true
      grep -qE -- "$SUP_PATTERN" <<<"$out" && { gate_fail "supervisors: systemd $scope units/timers reference it"; return; }
    done
  fi
  gate_ok "supervisors: no systemd unit/timer, cron, proxy, tunnel config, profile or launch script references it (sudo=$SUDO_OK)"
}

gate_protected_present() {
  local line bad=0
  while read -r line; do
    say "protected: $line"
    [[ $line =~ ^[0-9]+=[0-9]+\| ]] || bad=1
  done <<<"$1"
  (( bad == 0 )) || gate_fail "protected services are not each exactly one readable listener"
}

run_gates() {  # $1 = client samples
  FAILURES=()
  gate_listener
  [[ -n $TARGET_PID ]] || return 1
  gate_parent; gate_clients "$1"; gate_journal_users; gate_process_refs; gate_supervisors
  PROTECTED_BEFORE=$(protected_snapshot); gate_protected_present "$PROTECTED_BEFORE"
  (( ${#FAILURES[@]} == 0 ))
}

token() { echo "RETIRE-8769-PID$TARGET_PID-START$TARGET_START"; }

acquire_locks() {
  mkdir -p "$STATE_ROOT" && chmod 700 "$STATE_ROOT" || die "cannot create $STATE_ROOT"
  exec 8>>"$STATE_ROOT/retire.lock" && flock -n 8 || die "another retirement/rollback is running"
  if [[ -e $DEPLOY_LOCK ]]; then
    exec 7>>"$DEPLOY_LOCK" && flock -n 7 || die "a preview-8768 deployment holds $DEPLOY_LOCK"
  fi
}

pidfd_term() {  # pid starttime -> sends SIGTERM through a pidfd after re-verifying start time
  "$PYTHON" - "$1" "$2" <<'PY'
import os, signal, sys
pid, start = int(sys.argv[1]), sys.argv[2]
fd = os.pidfd_open(pid)            # from here the PID cannot be reused while fd is open
try:
    with open(f"/proc/{pid}/stat") as f:
        now = f.read().rsplit(")", 1)[1].split()[19]
    if now != start:
        print("PID_REUSED"); sys.exit(5)
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
    [[ ${2:-} == --approve && -n ${3:-} ]] || die "usage: $0 retire --approve <token from check>"
    acquire_locks
    say "== RETIRE $(date -u +%FT%TZ)"
    run_gates "$CLIENT_SAMPLES" || { say "nothing was changed"; exit 2; }
    [[ $3 == "$(token)" ]] || die "approval token does not match the current service identity ($(token))"
    S="$STATE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-pid$TARGET_PID"
    mkdir -m 700 "$S" || die "cannot create state directory"
    cp "/proc/$TARGET_PID/cmdline" "$S/argv" && cp "/proc/$TARGET_PID/environ" "$S/env" || die "cannot capture launch state"
    readlink -f "/proc/$TARGET_PID/cwd" >"$S/cwd"
    printf '%s\n' "$TARGET_JOURNAL" >"$S/journal"
    T git -C "$EXPECT_CWD" rev-parse HEAD >"$S/commit"
    printf '%s\n' "$PORT" >"$S/port"
    identity "$TARGET_PID" >"$S/identity"
    stat -c '%n %i %s %Y' "$TARGET_JOURNAL"* >"$S/journal-files" 2>/dev/null
    printf '%s\n' "$PROTECTED_BEFORE" >"$S/protected-before"
    chmod 600 "$S"/*
    say "launch state captured privately in $S (environment never printed)"
    # Immediately before the stop: re-verify everything that could change.
    say "-- final re-check immediately before SIGTERM"
    FAILURES=()
    pid_now=$(listener_pids "$PORT" || true)
    [[ $pid_now == "$TARGET_PID" && $(starttime "$TARGET_PID") == "$TARGET_START" ]] || gate_fail "listener identity changed"
    gate_clients 1; gate_journal_users; gate_process_refs; gate_parent
    [[ $(protected_snapshot) == "$PROTECTED_BEFORE" ]] || gate_fail "protected services changed before the stop"
    (( ${#FAILURES[@]} == 0 )) || { say "final re-check failed; nothing was stopped (state kept in $S)"; exit 2; }
    # Residual race (documented): a client could connect in the milliseconds
    # between this re-check and the signal; the stop would then end that
    # session. The PID itself cannot be reused (pidfd).
    result=$(pidfd_term "$TARGET_PID" "$TARGET_START") || { say "stop not sent: $result"; exit 2; }
    say "$result to PID $TARGET_PID"
    for ((i=0; i<STOP_WAIT; i++)); do
      [[ -e /proc/$TARGET_PID && $(starttime "$TARGET_PID") == "$TARGET_START" ]] || break
      sleep 1
    done
    if [[ -e /proc/$TARGET_PID && $(starttime "$TARGET_PID") == "$TARGET_START" ]]; then
      say "RETIRE_INCOMPLETE: PID $TARGET_PID still running after ${STOP_WAIT}s; NOT force-killed. Decide manually."
      exit 3
    fi
    rc=0
    [[ -z $(listener_pids "$PORT" || echo SS_FAILED) ]] || { say "POST FAIL: port $PORT still has a listener"; rc=4; }
    after=$(protected_snapshot)
    printf '%s\n' "$after" >"$S/protected-after"; chmod 600 "$S/protected-after"
    [[ $after == "$PROTECTED_BEFORE" ]] && say "protected services unchanged" || { say "POST FAIL: protected services changed:"; diff <(echo "$PROTECTED_BEFORE") <(echo "$after"); rc=4; }
    [[ -s $TARGET_JOURNAL ]] && say "journal preserved: $(stat -c '%s bytes' "$TARGET_JOURNAL")" || { say "POST FAIL: journal missing"; rc=4; }
    for p in $HEALTH_PROBE_PORTS; do say "health $p: $(http_json "$p" /healthz | cut -c1-200)"; done
    (( rc == 0 )) && say "RETIRED: PID $TARGET_PID stopped gracefully. Rollback: $0 rollback $S --approve ROLLBACK-8769"
    printf 'result=%s finished=%s\n' "$([[ $rc == 0 ]] && echo RETIRED || echo POST_CHECK_FAILED)" "$(date -u +%FT%TZ)" >"$S/result"
    exit "$rc" ;;

  rollback)
    S=${2:-}
    [[ -n $S && ${3:-} == --approve && ${4:-} == ROLLBACK-8769 ]] || die "usage: $0 rollback <state dir> --approve ROLLBACK-8769"
    acquire_locks
    say "== ROLLBACK $(date -u +%FT%TZ) from $S"
    for f in argv env cwd journal commit port protected-before; do [[ -s $S/$f ]] || die "state file missing: $S/$f"; done
    [[ $(stat -c %a "$S") == 700 ]] || die "state directory permissions are not 700"
    port=$(<"$S/port"); journal=$(<"$S/journal"); cwd=$(<"$S/cwd"); commit=$(<"$S/commit")
    [[ -z $(listener_pids "$port" || echo SS_FAILED) ]] || die "port $port is already in use; nothing started"
    [[ -s $journal ]] || die "journal $journal missing; nothing started"
    if command -v fuser >/dev/null 2>&1 && TMO=10 T fuser "$journal.dashboard.lock" >/dev/null 2>&1; then
      die "another process holds the journal lock; nothing started"
    fi
    [[ $(T git -C "$cwd" rev-parse HEAD) == "$commit" ]] || die "checkout $cwd is no longer at $commit; nothing started"
    PROTECTED_BEFORE=$(protected_snapshot)
    mapfile -d '' -t ENVV <"$S/env"; mapfile -d '' -t ARGV <"$S/argv"
    ( cd "$cwd" && exec setsid nohup env -i "${ENVV[@]}" "${ARGV[@]}" \
        >>"$(dirname "$journal")/restart-$port.log" 2>&1 </dev/null 7>&- 8>&- ) &
    say "relaunched; verifying (up to ${READY_TIMEOUT}s)"
    deadline=$((SECONDS + READY_TIMEOUT)) ok=0 new=""
    while (( SECONDS < deadline )); do
      new=$(listener_pids "$port" || true)
      if [[ $(wc -w <<<"$new") == 1 ]]; then
        h=$(http_json "$port" /healthz); r=$(http_json "$port" /readyz)
        hs=$("$PYTHON" -c 'import json,sys; c,b=sys.argv[1].split(" ",1); b=json.loads(b); print(int(c=="200" and b.get("status")=="HEALTHY" and b.get("signals_only") is True and (b.get("workers") or {}).get("healthy") is True))' "$h")
        rs=$("$PYTHON" -c 'import json,sys; c,b=sys.argv[1].split(" ",1); b=json.loads(b); print(int(c=="200" and b.get("status")=="READY" and b.get("signals_only") is True))' "$r")
        if [[ $hs == 1 && ( $rs == 1 || $REQUIRE_READY == 0 ) ]]; then ok=1; break; fi
      fi
      sleep 3
    done
    rc=0
    (( ok )) || { say "VERIFY FAIL: not healthy/ready within ${READY_TIMEOUT}s (process left running for inspection)"; rc=4; }
    if [[ $(wc -w <<<"$new") == 1 ]]; then
      mapfile -d '' -t NOWV <"/proc/$new/cmdline"
      same=$(( ${#ARGV[@]} == ${#NOWV[@]} ))
      for ((i=0; same && i<${#ARGV[@]}; i++)); do [[ ${ARGV[i]} == "${NOWV[i]}" ]] || same=0; done
      if (( same )); then
        say "verify: argv identical, ${#NOWV[@]} arguments (same program, module, port, journal, signals-only flags)"
      else
        say "VERIFY FAIL: argv differs (saved ${#ARGV[@]} args, running ${#NOWV[@]})"; rc=4
      fi
      [[ $(readlink -f "/proc/$new/cwd") == "$cwd" ]] && say "verify: cwd $cwd" || { say "VERIFY FAIL: cwd differs"; rc=4; }
      [[ $(T git -C "$cwd" rev-parse HEAD) == "$commit" ]] && say "verify: commit ${commit:0:12}" || { say "VERIFY FAIL: commit differs"; rc=4; }
      tr '\0' '\n' <"/proc/$new/cmdline" | grep -qxE -- '--account-readonly|--private-dashboard' && { say "VERIFY FAIL: account flags present"; rc=4; } || say "verify: signals-only (no account flags)"
      say "verify: healthz $(http_json "$port" /healthz | cut -c1-160)"
      say "verify: readyz  $(http_json "$port" /readyz | cut -c1-160)$( [[ $REQUIRE_READY == 0 ]] && echo ' [readiness NOT required: rehearsal override]')"
    else
      say "VERIFY FAIL: expected one listener on $port, got '$new'"; rc=4
    fi
    [[ $(protected_snapshot) == "$PROTECTED_BEFORE" ]] && say "verify: protected services unchanged" || { say "VERIFY FAIL: protected services changed"; rc=4; }
    (( rc == 0 )) && say "ROLLBACK VERIFIED: PID $new"
    exit "$rc" ;;

  *) say "usage: $0 check | retire --approve <token> | rollback <state dir> --approve ROLLBACK-8769"; exit 2 ;;
esac
