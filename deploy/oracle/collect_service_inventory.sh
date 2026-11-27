#!/usr/bin/env bash
# READ-ONLY, time-bounded owner/launch/usage check for dashboard ports.
#
#   collect_service_inventory.sh            (ports 8767 8769 by default)
#
# For each port: listener pid, user, start time, launch mechanism (cgroup and
# parent chain), bind address, working directory, checked-out git commit,
# module/mode/journal NAME and flag NAMES (never values or environment),
# client connections sampled 4 times over 20 s, other processes using the
# same journal, and systemd/cron/proxy references.
#
# Bounded: every external command runs under `timeout 5`; no directory-size
# scans, no `git status`, no system-wide file-descriptor scan; the whole
# script stops itself after 80 s. Nothing is started, stopped, signalled,
# written or deleted, and no HTTP request is sent to any port.
set -uo pipefail
umask 077
read -r -a PORTS <<<"${INVENTORY_PORTS:-8767 8769}"
START=$SECONDS
LIMIT=80
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true
t() { timeout -k 1 5 "$@" 2>/dev/null; }
left() { (( SECONDS - START < LIMIT )); }
section() { printf '\n===== %s =====\n' "$*"; }
redact() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ) user=$(id -un) ports=${PORTS[*]}"
LISTEN=$(t ss -Hltnp)

declare -A PID JOURNAL ANCESTORS
for port in "${PORTS[@]}"; do
  left || break
  section "PORT $port"
  line=$(grep -E "[:.]${port}[[:space:]]" <<<"$LISTEN" | head -n1)
  pid=$(grep -oE 'pid=[0-9]+' <<<"$line" | head -n1 | cut -d= -f2)
  if [[ -z $pid ]]; then echo "no listener"; continue; fi
  PID[$port]=$pid
  echo "bind: $(awk '{print $4}' <<<"$line")"
  read -r user lstart <<<"$(t ps -o user=,lstart= -p "$pid")"
  echo "pid $pid  user $user  started $lstart  elapsed $(t ps -o etime= -p "$pid" | tr -d ' ')"
  echo "cgroup: $(head -c 400 "/proc/$pid/cgroup" 2>/dev/null | tr '\n' ' ')"
  chain="$pid:$(cat "/proc/$pid/comm" 2>/dev/null)" p=$pid
  for _ in 1 2 3 4 5 6; do
    pp=$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null)
    [[ -z $pp || $pp == 0 ]] && break
    chain+=" <- $pp:$(cat "/proc/$pp/comm" 2>/dev/null)"
    [[ $pp == 1 ]] && break
    p=$pp
  done
  echo "parent chain: $chain"
  ANCESTORS[$port]=" $(sed -E 's/:[^ ]*//g; s/<-//g' <<<"$chain") "
  mapfile -d '' -t argv <"/proc/$pid/cmdline"
  module= mode= journal= flags=()
  for ((i=0; i<${#argv[@]}; i++)); do
    case ${argv[i]} in
      -m) module=${argv[i+1]:-} ;;
      --mode) mode=${argv[i+1]:-} ;;
      --journal) journal=${argv[i+1]:-} ;;
      --*) flags+=("${argv[i]}") ;;
    esac
  done
  JOURNAL[$port]=$journal
  echo "module: $module  mode: $mode  journal: $(basename "${journal:-none}")"
  echo "flag names: ${flags[*]}"
  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null)
  echo "cwd: $cwd"
  if [[ -n $cwd ]] && t git -C "$cwd" rev-parse --git-dir >/dev/null; then
    echo "git HEAD: $(t git -C "$cwd" rev-parse HEAD)  branch: $(t git -C "$cwd" rev-parse --abbrev-ref HEAD)"
    echo "git last commit: $(t git -C "$cwd" log -1 --format='%cI %s' | cut -c1-140)"
  else
    echo "git: not a checkout (or timed out)"
  fi
  [[ -n $journal ]] && echo "journal: $(t stat -c '%s bytes, last write %y' "$journal" | cut -c1-60)"
  echo "process fds: $(ls /proc/$pid/fd 2>/dev/null | wc -l)  threads: $(awk '/^Threads:/{print $2}' "/proc/$pid/status")"
done

section "CLIENT CONNECTIONS (4 samples over 20 s)"
declare -A PEERS
for n in 1 2 3 4; do
  left || break
  snap=$(t ss -Htn state established)
  for port in "${PORTS[@]}"; do
    while read -r peer; do
      [[ -n $peer ]] && PEERS["$port $peer"]=1
    done < <(awk -v p=":$port" '$3 ~ p"$" {print $4}' <<<"$snap")
  done
  (( n < 4 )) && sleep 5
done
for port in "${PORTS[@]}"; do
  c=0; for k in "${!PEERS[@]}"; do [[ $k == "$port "* ]] && c=$((c + 1)); done
  echo "port $port: $c distinct client connections seen"
done
echo "--- client processes connected to these ports (now) ---"
t ss -Htnp state established | awk -v re=":($(IFS='|'; echo "${PORTS[*]}"))$" '$4 ~ re {print $4, $5}' \
  | sed -E 's/fd=[0-9]+,?//g' | sort | uniq -c | head -n 20

section "OTHER PROCESSES USING THE SAME JOURNAL (python processes only)"
for port in "${PORTS[@]}"; do
  j=${JOURNAL[$port]:-}; [[ -z $j ]] && continue
  users=()
  for p in $(t pgrep -f python | head -n 40); do
    [[ ${ANCESTORS[$port]:-} == *" $p "* || $p == "$$" ]] && continue   # the server and its launcher
    if { ls -l "/proc/$p/fd" | grep -qF "$(basename "$j")"; } 2>/dev/null; then
      users+=("$p:$(cat /proc/$p/comm 2>/dev/null)")
    elif { tr '\0' ' ' <"/proc/$p/cmdline" | grep -qF "$(basename "$j")"; } 2>/dev/null; then
      users+=("$p:$(cat /proc/$p/comm 2>/dev/null)(argv)")
    fi
  done
  echo "port $port journal $(basename "$j"): ${users[*]:-none}"
done

section "SUPERVISION: SYSTEMD, CRON, PROXIES"
left && t systemctl list-units --all --no-pager --plain | grep -iE 'kalshi|signal|dashboard|preview' | head -n 15
left && t systemctl --user list-units --all --no-pager --plain | grep -iE 'kalshi|signal|dashboard|preview' | head -n 15
for port in "${PORTS[@]}"; do
  left || break
  hits=$(t grep -rlsE "\b${port}\b" /etc/systemd/system "$HOME/.config/systemd/user" /etc/nginx /etc/caddy /etc/cloudflared "$HOME/.cloudflared" | head -n 5 | tr '\n' ' ')
  echo "config files mentioning $port: ${hits:-none}"
  echo "crontab lines mentioning $port: $(t crontab -l | grep -vE '^\s*#' | grep -cE "\b${port}\b")"
done

section "VERDICT INPUTS"
for port in "${PORTS[@]}"; do
  pid=${PID[$port]:-}
  [[ -z $pid ]] && { echo "port $port: not running"; continue; }
  c=0; for k in "${!PEERS[@]}"; do [[ $k == "$port "* ]] && c=$((c + 1)); done
  sup="shell/nohup"
  grep -q '\.service' "/proc/$pid/cgroup" 2>/dev/null && sup="systemd: $(grep -oE '[^/]+\.service' "/proc/$pid/cgroup" | head -n1)"
  echo "port $port: launched by $sup; clients seen: $c"
done
echo
echo "elapsed: $((SECONDS - START)) s"
echo "SERVICE_INVENTORY_COMPLETE (read-only; nothing was changed)"
