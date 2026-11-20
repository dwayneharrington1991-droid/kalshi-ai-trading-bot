#!/usr/bin/env bash
# Approved retirement of the OLD bots on ports 8765 and 8767 (8768 is kept).
#
#   retire_old_bots.sh run
#       1. Gates: 8768 has exactly one listener and is HEALTHY, signals-only,
#          workers healthy and READY; no guarded 8768 deployment is running.
#       2. For each old port still listening: records the process, its systemd
#          unit (from its cgroup) or manual launcher, argv, working directory,
#          journal and environment into a private rollback bundle
#          (STATE_ROOT/<stamp>/<port>/, 0600, SHA-256 manifest; environment
#          values are never printed) plus the crontab, and checks that 8768
#          does not share its journal (shared code/interpreter directories are
#          reported and are never removed by anything here).
#       3. Measures CPU (per service, 20 s) before the change.
#       4. Stops and disables ONLY that service: `systemctl disable --now` for
#          a systemd unit, otherwise ONE SIGTERM through a pidfd (never a
#          force-kill) after refusing a looping launcher; watches 20 s for a
#          respawn; removes from the crontab only lines naming that port or
#          journal (the original crontab is in the bundle).
#       5. Verifies the port is free, the journal is released, and 8768 is
#          still the same process, HEALTHY and READY; measures CPU again.
#   retire_old_bots.sh rollback <bundle dir>
#       Re-enables the unit or relaunches the saved argv/cwd/environment.
#
# Deletes nothing: old journals, code and data stay in place until a verified
# off-server backup exists. Never touches 8768 beyond reading it.
# Exit: 0 done; 2 blocked before any change; 3 a service did not stop
# (never force-killed); 4 verification after the change failed.
set -Euo pipefail
umask 077
read -r -a OLD <<<"${RETIRE_OLD_PORTS:-8765 8767}"
KEEP=${RETIRE_KEEP_PORT:-8768}
STATE_ROOT=${RETIRE_STATE_ROOT:-/home/opc/.kalshi-deploy/retire-old-bots}
DEPLOY_LOCK=${RETIRE_DEPLOY_LOCK:-/home/opc/.kalshi-deploy/preview-8768/deploy.lock}
PY=${RETIRE_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
STOP_WAIT=${RETIRE_STOP_WAIT:-45}
RESPAWN_WATCH=${RETIRE_RESPAWN_WATCH:-20}
CPU_SAMPLE=${RETIRE_CPU_SAMPLE:-20}
SYSTEMCTL=${RETIRE_SYSTEMCTL:-systemctl}
say() { printf '%s\n' "$*"; }
die() { say "BLOCKED: $*"; exit 2; }
SUDO=(); [[ $(id -u) == 0 ]] || { timeout 5 sudo -n true >/dev/null 2>&1 && SUDO=(sudo -n); }
pids_on() { ( "${SUDO[@]}" ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u; }
start_of() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }
ticks() { awk '{print $14 + $15}' "/proc/$1/stat" 2>/dev/null || echo 0; }
flag_of() { local pid=$1 want=$2 i; local -a a=(); mapfile -d '' -t a <"/proc/$pid/cmdline"
  for ((i=0; i<${#a[@]}-1; i++)); do [[ ${a[i]} == "$want" ]] && { printf '%s' "${a[i+1]}"; return; }; done; }
abs() { [[ -z $1 || $1 == /* ]] && printf '%s' "$1" || printf '%s/%s' "$2" "$1"; }

health() {  # port -> prints a line; exit 0 only if HEALTHY, signals-only, workers healthy and READY
  "$PY" -I - "$1" <<'PY'
import json, sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
o = build_opener(ProxyHandler({}))
def get(path):
    try:
        with o.open(f"http://127.0.0.1:{sys.argv[1]}{path}", timeout=10) as r:
            return r.status, json.load(r)
    except HTTPError as e:
        return e.code, json.load(e)
    except Exception as e:
        return None, {"error": type(e).__name__}
hc, h = get("/healthz"); rc, r = get("/readyz")
ok = (hc == 200 and h.get("status") == "HEALTHY" and h.get("signals_only") is True
      and (h.get("workers") or {}).get("healthy") is True and rc == 200 and r.get("status") == "READY")
print(f"{sys.argv[1]}: health HTTP {hc} {h.get('status')} signals_only={h.get('signals_only')} "
      f"workers_healthy={(h.get('workers') or {}).get('healthy')} ready HTTP {rc} {r.get('status')} {r.get('stale_reasons') or ''}")
sys.exit(0 if ok else 1)
PY
}

wait_healthy() { local i; for i in $(seq 1 12); do health "$KEEP" && return 0; sleep 5; done; return 1; }

cpu_report() {  # label -> per-service CPU % of one core over CPU_SAMPLE seconds
  local label=$1 p; declare -A t0 pid
  for p in "${OLD[@]}" "$KEEP"; do local x; x=$(pids_on "$p" | head -n1); [[ -n $x ]] && { pid[$p]=$x; t0[$p]=$(ticks "$x"); }; done
  local idle0 total0 idle1 total1
  read -r idle0 total0 < <(awk '/^cpu /{print $5+$6, $2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat)
  sleep "$CPU_SAMPLE"
  read -r idle1 total1 < <(awk '/^cpu /{print $5+$6, $2+$3+$4+$5+$6+$7+$8+$9}' /proc/stat)
  local hz; hz=$(getconf CLK_TCK); local line="CPU_$label:"
  for p in "${!pid[@]}"; do line+=" $p=$(( ( $(ticks "${pid[$p]}") - t0[$p] ) * 100 / (CPU_SAMPLE * hz) ))%"; done
  say "$line host_idle=$(( (idle1 - idle0) * 100 / (total1 - total0 > 0 ? total1 - total0 : 1) ))% load=$(cut -d' ' -f1-3 /proc/loadavg) mem_available_mib=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)"
}

pidfd_term() {  # pid start -> 0 if exited after ONE SIGTERM within STOP_WAIT
  "$PY" -I - "$1" "$2" "$STOP_WAIT" <<'PY'
import os, signal, sys, time
pid, start, wait = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
fd = os.pidfd_open(pid)
with open(f"/proc/{pid}/stat") as f:
    if f.read().rsplit(")", 1)[1].split()[19] != start:
        sys.exit("identity changed before stop")
signal.pidfd_send_signal(fd, signal.SIGTERM)
deadline = time.monotonic() + wait
while time.monotonic() < deadline:
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                sys.exit(0)
    except FileNotFoundError:
        sys.exit(0)
    time.sleep(0.5)
sys.exit(3)
PY
}

run() {
  for tool in ss timeout flock awk grep sha256sum; do command -v "$tool" >/dev/null || die "required tool missing: $tool"; done
  [[ -x $PY ]] || die "python not found at $PY"
  local keep_pid; keep_pid=$(pids_on "$KEEP")
  [[ $(wc -w <<<"$keep_pid") == 1 ]] || die "$KEEP must have exactly one listener (has: ${keep_pid:-none})"
  local keep_start; keep_start=$(start_of "$keep_pid")
  wait_healthy || die "$KEEP is not HEALTHY and READY; nothing was changed"
  if [[ -e $DEPLOY_LOCK ]]; then exec 9<"$DEPLOY_LOCK"; flock -n 9 || die "a guarded $KEEP deployment holds its lock; nothing was changed"; fi
  local keep_journal; keep_journal=$(readlink -f "$(abs "$(flag_of "$keep_pid" --journal)" "$(readlink -f "/proc/$keep_pid/cwd")")")
  local keep_cwd keep_exe; keep_cwd=$(readlink -f "/proc/$keep_pid/cwd"); keep_exe=$(tr '\0' '\n' <"/proc/$keep_pid/cmdline" | head -n1)
  local stamp; stamp=$(date -u +%Y%m%dT%H%M%SZ); local root=$STATE_ROOT/$stamp
  mkdir -p "$root" && chmod 700 "$STATE_ROOT" "$root"
  crontab -l >"$root/crontab.before" 2>/dev/null || : >"$root/crontab.before"
  declare -A PID START UNIT SCOPE JOURNAL
  local SELF_UNIT; SELF_UNIT=$(grep -oE '[^/]+\.service' "/proc/$$/cgroup" | grep -vE '^(user@[0-9]+|session-[0-9]+)\.service$' | tail -n1 || true)
  for p in "${OLD[@]}"; do
    local ids; ids=$(pids_on "$p")
    if [[ -z $ids ]]; then say "$p: no listener (already retired)"; continue; fi
    [[ $(wc -w <<<"$ids") == 1 ]] || die "$p has several listeners ($ids); refusing"
    local pid=$ids cwd j unit scope="" b=$root/$p
    cwd=$(readlink -f "/proc/$pid/cwd"); j=$(flag_of "$pid" --journal); j=$(abs "$j" "$cwd"); [[ -n $j ]] && j=$(readlink -f "$j")
    [[ -n $j && $j == "$keep_journal" ]] && die "$p uses the SAME journal as $KEEP; refusing"
    unit=$(grep -oE '[^/]+\.service' "/proc/$pid/cgroup" | grep -vE '^(user@[0-9]+|session-[0-9]+)\.service$' | tail -n1)
    if [[ -n $unit && $unit == "$SELF_UNIT" ]]; then
      # The listener shares THIS script's own service unit (e.g. a CI runner or
      # a cron job inside crond.service). Disabling that unit would stop this
      # script and its caller, never just the old bot: stop the process alone.
      say "$p: shares this script's own unit $unit; the unit is never stopped, only the process"
      unit=""
    fi
    if [[ -n $unit ]]; then grep -q 'user@[0-9]*\.service' "/proc/$pid/cgroup" && scope=--user || scope=system; fi
    mkdir -p "$b"
    cp "/proc/$pid/cmdline" "$b/argv.nul"; printf '%s\n' "$cwd" >"$b/cwd"; printf '%s\n' "$j" >"$b/journal"
    "${SUDO[@]}" cat "/proc/$pid/environ" >"$b/environ.nul" 2>/dev/null || cat "/proc/$pid/environ" >"$b/environ.nul"
    printf '%s %s\n' "${unit:-none}" "${scope:-manual}" >"$b/unit"
    [[ -n $unit ]] && { "${SUDO[@]}" $SYSTEMCTL ${scope/system/} cat "$unit" >"$b/unit-file.txt" 2>/dev/null || true; }
    ls -la "$j" "$j-wal" "$j-shm" >"$b/journal-files.txt" 2>&1
    ( cd "$b" && sha256sum -- * >MANIFEST.sha256 )
    PID[$p]=$pid; START[$p]=$(start_of "$pid"); UNIT[$p]=$unit; SCOPE[$p]=$scope; JOURNAL[$p]=$j
    say "$p: pid $pid unit ${unit:-none (manual launch)} journal $j cwd $cwd"
    [[ $keep_cwd == "$cwd" || $keep_cwd == "$cwd"/* || $keep_exe == "$cwd"/* ]] \
      && say "  SHARED with $KEEP: $cwd (code/interpreter) - kept; never removed"
    say "  rollback bundle: $b (sha256 manifest written)"
  done
  (( ${#PID[@]} )) || { say "RETIRE_RESULT: nothing to retire"; health "$KEEP"; exit 0; }
  cpu_report BEFORE
  local failed=0
  for p in "${!PID[@]}"; do
    local pid=${PID[$p]} unit=${UNIT[$p]}
    [[ $(start_of "$pid") == "${START[$p]}" ]] || die "$p identity changed before stop; nothing more changed"
    if [[ -n $unit ]]; then
      if [[ ${SCOPE[$p]} == --user ]]; then $SYSTEMCTL --user disable --now "$unit" || failed=1
      else (( ${#SUDO[@]} )) || [[ $(id -u) == 0 ]] || die "$p runs as system unit $unit and passwordless sudo is unavailable"
        "${SUDO[@]}" $SYSTEMCTL disable --now "$unit" || failed=1; fi
      say "$p: systemd unit $unit disabled and stopped"
    else
      local ppid; ppid=$(awk '/^PPid:/{print $2}' "/proc/$pid/status")
      if [[ $ppid != 1 ]] && tr '\0' ' ' <"/proc/$ppid/cmdline" 2>/dev/null | grep -qE '(^|[;&| ])(while|until|for)[[:space:]]'; then
        die "$p's launcher (pid $ppid) is a loop that would restart it; nothing more changed"
      fi
      pidfd_term "$pid" "${START[$p]}"; rc=$?
      (( rc == 0 )) || { say "STOP_INCOMPLETE: $p did not exit after one SIGTERM (not force-killed)"; exit 3; }
      say "$p: stopped with one SIGTERM"
    fi
  done
  sleep "$RESPAWN_WATCH"
  for p in "${!PID[@]}"; do
    [[ -z $(pids_on "$p") ]] || { say "VERIFY_FAILED: $p is listening again (respawned)"; failed=1; }
    local j=${JOURNAL[$p]}
    if [[ -n $j ]] && "${SUDO[@]}" find /proc/[0-9]*/fd -maxdepth 1 -lname "$j" 2>/dev/null | grep -q .; then
      say "VERIFY_FAILED: $p journal still open by a process"; failed=1; fi
  done
  local cron_now; cron_now=$(crontab -l 2>/dev/null || true)
  local pattern; pattern=$(printf '%s|' "${!PID[@]}"); pattern="(^|[^0-9])(${pattern%|})([^0-9]|$)"
  for p in "${!PID[@]}"; do [[ -n ${JOURNAL[$p]} ]] && pattern+="|$(basename "${JOURNAL[$p]}" | sed 's/[.]/[.]/g')"; done
  if grep -qE "$pattern" <<<"$cron_now"; then
    grep -vE "$pattern" <<<"$cron_now" | crontab - && say "crontab: removed autostart lines naming the retired bots (original saved in $root/crontab.before)"
  fi
  [[ $(pids_on "$KEEP") == "$keep_pid" && $(start_of "$keep_pid") == "$keep_start" ]] || { say "VERIFY_FAILED: $KEEP process changed"; failed=1; }
  wait_healthy || { say "VERIFY_FAILED: $KEEP not HEALTHY and READY after retirement"; failed=1; }
  cpu_report AFTER
  printf '%s\n' "${!PID[@]}" >"$root/retired_ports"
  (( failed )) && { say "RETIRE_RESULT: VERIFY_FAILED (rollback: $0 rollback $root/<port>)"; exit 4; }
  say "RETIRE_RESULT: RETIRED ${!PID[*]}; $KEEP unchanged, HEALTHY and READY; bundles in $root; nothing deleted"
}

rollback() {
  local b=${1:-}; [[ -f $b/argv.nul && -f $b/unit ]] || die "usage: $0 rollback <bundle dir with argv.nul>"
  ( cd "$b" && sha256sum -c --quiet MANIFEST.sha256 ) || die "bundle manifest does not verify"
  read -r unit scope <"$b/unit"
  if [[ $unit != none ]]; then
    if [[ $scope == --user ]]; then $SYSTEMCTL --user enable --now "$unit"; else "${SUDO[@]}" $SYSTEMCTL enable --now "$unit"; fi
  else
    "$PY" -I - "$b" <<'PY'
import os, subprocess, sys
b = sys.argv[1]
argv = [a for a in open(f"{b}/argv.nul", "rb").read().decode().split("\0") if a]
env = dict(kv.split("=", 1) for kv in open(f"{b}/environ.nul", "rb").read().decode().split("\0") if "=" in kv)
cwd = open(f"{b}/cwd").read().strip()
log = open(f"{b}/relaunch.log", "ab")
subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
PY
  fi
  say "ROLLBACK: relaunch requested from $b"
}

case ${1:-} in
  run) run ;;
  rollback) rollback "${2:-}" ;;
  *) say "usage: $0 run | rollback <bundle dir>"; exit 2 ;;
esac
