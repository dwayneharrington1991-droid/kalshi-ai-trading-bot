#!/usr/bin/env bash
# READ-ONLY inventory of the dashboard services on ports 8765/8767/8768/8769.
#
#   collect_service_inventory.sh [seconds]     (default 60, allowed 20..300)
#
# For each listener: owner, launch mechanism (systemd unit / cron / shell
# session from /proc/<pid>/cgroup and the parent-process chain), start time,
# working directory and the git commit/branch checked out there, bind
# address, minimal identity (module, --port, --mode, journal NAME, and the
# NAMES of other flags; never values or environment), established client
# connections sampled over the window (who is actually using it), and which
# processes hold each journal open. Also systemd units, crontab lines,
# reverse-proxy/tunnel configuration that mention these ports, and sizes.
#
# Nothing is started, stopped, signalled, attached to, written or deleted, and
# no HTTP request is sent to ANY port (8765/8767 are only observed via /proc
# and socket tables). Runs at the lowest CPU and I/O priority.
set -uo pipefail
umask 077
secs=${1:-60}
[[ $secs =~ ^[0-9]+$ ]] && (( secs >= 20 && secs <= 300 )) || { echo "seconds must be 20..300"; exit 2; }
read -r -a PORTS <<<"${INVENTORY_PORTS:-8765 8767 8768 8769}"
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true
section() { printf '\n===== %s =====\n' "$*"; }
redact() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }
listener_pid() { ss -Hltnp "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ) user=$(id -un) window=${secs}s"

for port in "${PORTS[@]}"; do
  section "PORT $port"
  pid=$(listener_pid "$port")
  if [[ -z $pid ]]; then echo "no listener"; continue; fi
  echo "bind: $(ss -Hltn "sport = :$port" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ')"
  echo "pid $pid  user $(ps -o user= -p "$pid")  started $(ps -o lstart= -p "$pid")  elapsed $(ps -o etime= -p "$pid" | tr -d ' ')"
  echo "cgroup (launch mechanism): $(tr '\n' ' ' <"/proc/$pid/cgroup" 2>/dev/null)"
  chain="" p=$pid
  for _ in 1 2 3 4 5 6 7 8; do
    pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    [[ -z $pp || $pp == 0 ]] && break
    chain+=" <- $pp:$(ps -o comm= -p "$pp" 2>/dev/null)"
    [[ $pp == 1 ]] && break
    p=$pp
  done
  echo "parent chain: $pid:$(ps -o comm= -p "$pid")$chain"
  echo "session id: $(ps -o sid= -p "$pid" | tr -d ' ')  tty: $(ps -o tty= -p "$pid" | tr -d ' ')"
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
  echo "module: $module  mode: $mode  journal file: $(basename "${journal:-none}")"
  echo "flag names: ${flags[*]}"
  cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null)
  echo "cwd: $cwd"
  if [[ -n $cwd ]] && git -C "$cwd" rev-parse --git-dir >/dev/null 2>&1; then
    echo "git: $(git -C "$cwd" rev-parse HEAD 2>/dev/null) branch=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    echo "git last commit: $(git -C "$cwd" log -1 --format='%cI %s' 2>/dev/null | cut -c1-160)"
    echo "git uncommitted files: $(git -C "$cwd" status --porcelain 2>/dev/null | wc -l)"
  else
    echo "git: not a checkout"
  fi
  echo "journal path size: $( [[ -n $journal ]] && du -h --apparent-size "$journal" 2>/dev/null | cut -f1 ) last modified: $( [[ -n $journal ]] && stat -c %y "$journal" 2>/dev/null | cut -c1-19 )"
  echo "cwd size: $( [[ -n $cwd ]] && du -sh "$cwd" 2>/dev/null | cut -f1 )"
  echo "files at cwd top level: $(ls -1 "$cwd" 2>/dev/null | head -n 40 | tr '\n' ' ')"
done

section "CLIENT CONNECTIONS SAMPLED EVERY 5 s FOR ${secs}s (who actually uses each port)"
end=$((SECONDS + secs))
declare -A seen
while (( SECONDS < end )); do
  for port in "${PORTS[@]}"; do
    while read -r line; do
      [[ -z $line ]] && continue
      peer=$(awk '{print $4}' <<<"$line")
      seen["$port $peer"]=1
    done < <(ss -Htn state established "( sport = :$port )" 2>/dev/null)
  done
  sleep 5
done
for port in "${PORTS[@]}"; do
  peers=()
  for k in "${!seen[@]}"; do [[ $k == "$port "* ]] && peers+=("${k#* }"); done
  echo "port $port: ${#peers[@]} distinct client endpoints seen"
done
echo "--- client-side processes of local connections to these ports (now) ---"
port_re=$(IFS='|'; echo "${PORTS[*]}")
ss -Htnp state established 2>/dev/null \
  | awk -v re=":(${port_re})$" '$4 ~ re {print $4, $5}' | sed -E 's/fd=[0-9]+//g' | sort | uniq -c | head -n 40

section "WHICH PROCESSES HOLD EACH JOURNAL OPEN"
for fd in /proc/[0-9]*/fd/*; do
  target=$(readlink "$fd" 2>/dev/null) || continue
  [[ $target == *.db || $target == *.db-wal || $target == *.lock ]] || continue
  p=${fd#/proc/}; p=${p%%/*}
  printf '%s pid=%s comm=%s\n' "$target" "$p" "$(cat /proc/$p/comm 2>/dev/null)"
done 2>/dev/null | sort -u | head -n 60

section "SYSTEMD UNITS AND TIMERS MENTIONING THE BOT"
systemctl list-units --all --no-pager --plain 2>/dev/null | grep -iE 'kalshi|signal|dashboard|preview|learning' | head -n 30
systemctl --user list-units --all --no-pager --plain 2>/dev/null | grep -iE 'kalshi|signal|dashboard|preview|learning' | head -n 30
systemctl list-timers --all --no-pager 2>/dev/null | grep -iE 'kalshi|signal|dashboard|preview|learning' | head -n 20
for d in /etc/systemd/system "$HOME/.config/systemd/user"; do
  grep -lsE '876[5-9]|kalshi' "$d"/*.service "$d"/*.timer 2>/dev/null | while read -r f; do
    echo "--- $f"; grep -E '^(Description|ExecStart|WorkingDirectory|Restart|User)=' "$f" | redact | cut -c1-200
  done
done

section "CRON LINES MENTIONING THE BOT"
crontab -l 2>/dev/null | grep -vE '^\s*#' | grep -iE 'kalshi|876[5-9]|python|signals' | redact | cut -c1-200
grep -lsE 'kalshi|876[5-9]' /etc/cron.d/* 2>/dev/null

section "REVERSE PROXY / TUNNEL REFERENCES TO THE PORTS"
grep -rsnE '876[5-9]' /etc/nginx /etc/caddy /etc/cloudflared "$HOME/.cloudflared" "$HOME/.ssh/config" 2>/dev/null | redact | cut -c1-200 | head -n 20
ps -eo pid,comm,etime | grep -iE 'cloudflared|ngrok|autossh|caddy|nginx|tailscale' | head

section "OTHER BOT PROCESSES (identity only)"
ps -eww -o pid,etime,comm,args --sort=start_time | grep -E 'python' | grep -v grep \
  | awk '{printf "%s %s %s", $1, $2, $3; prev=""; for (i=4;i<=NF;i++) { if ($i ~ /\.py$/ || $i ~ /^--port$/ || prev=="--port") printf " %s", $i; prev=$i }; print ""}' \
  | head -n 20

section "DISK"
df -h /home/opc
du -sh /home/opc/kalshi-backups/* 2>/dev/null | head

echo
echo "SERVICE_INVENTORY_COMPLETE (read-only; nothing was changed)"
