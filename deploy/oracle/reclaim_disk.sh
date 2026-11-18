#!/usr/bin/env bash
# Guarded disk-space reclaim for the Phase 5 preview deployment (Oracle).
#
#   reclaim_disk.sh plan                  read-only: what is safe to reclaim
#   reclaim_disk.sh apply --approve RECLAIM
#
# ONLY these data-free categories are ever touched:
#   1. pip download cache (~/.cache/pip): re-downloadable, holds no data
#   2. package-manager cached packages (sudo -n dnf clean packages), if sudo
#   3. systemd journal (operating-system logs) vacuumed to 500 MB, if sudo
#   4. inactive application logs: *.log / *.log.N regular files > 50 MB under
#      /home/opc that NO process has open, compressed with gzip (every line
#      is preserved, nothing is discarded); never inside kalshi-backups and
#      never any database (*.db, *.db-wal, *.db-shm) file
#   5. leftover deployment staging dirs /home/opc/.kalshi-preview-stage.*
#      older than one day
# Never touched: databases, journals, WAL/SHM, models/artifacts, credentials,
# backups, research outputs, release checkouts. Large items are only LISTED.
# `apply` re-measures and refuses unless the result reaches TARGET free.
set -uo pipefail
umask 077
HOME_DIR=${RECLAIM_HOME:-/home/opc}
TARGET_BYTES=${RECLAIM_TARGET_BYTES:-$((11 * 1024 * 1024 * 1024))}   # 10 GiB switch minimum + margin
MIN_LOG_BYTES=$((50 * 1024 * 1024))
JOURNAL_FS=${RECLAIM_FS_PATH:-/home/opc/kalshi-preview-upgrade/logs}
PY=${RECLAIM_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
say() { printf '%s\n' "$*"; }
[[ $HOME_DIR == /* && -d $HOME_DIR && ! -L $HOME_DIR && $HOME_DIR != / ]] || { say "invalid home directory '$HOME_DIR'; nothing done"; exit 2; }
gib() { awk -v b="$1" 'BEGIN{printf "%.2f GiB", b/1073741824}'; }
free_bytes() { df -B1 --output=avail "$JOURNAL_FS" 2>/dev/null | tail -1 | tr -d ' '; }
SUDO_OK=0; timeout 5 sudo -n true >/dev/null 2>&1 && SUDO_OK=1
HAVE_FUSER=0; command -v fuser >/dev/null 2>&1 && HAVE_FUSER=1

pip_bytes() { [[ -d $HOME_DIR/.cache/pip && ! -L $HOME_DIR/.cache/pip ]] && du -sb "$HOME_DIR/.cache/pip" 2>/dev/null | cut -f1 || echo 0; }
dnf_bytes() { (( SUDO_OK )) && sudo -n du -sb /var/cache/dnf 2>/dev/null | cut -f1 || echo 0; }
journald_bytes() {   # bytes above the 500 MB that would be kept
  (( SUDO_OK )) || { echo 0; return; }
  local used; used=$(sudo -n journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMGT]' | head -1 | numfmt --from=iec 2>/dev/null || echo 0)
  awk -v u="${used:-0}" 'BEGIN{k=500*1024*1024; print (u>k)?int(u-k):0}'
}
inactive_logs() {    # prints "bytes<TAB>path" for compressible inactive logs
  (( HAVE_FUSER )) || return 0
  timeout 60 find "$HOME_DIR" -xdev -path "$HOME_DIR/kalshi-backups" -prune -o \
    -type f \( -name '*.log' -o -regex '.*\.log\.[0-9]+' \) -size +"$((MIN_LOG_BYTES / 1024))"k -print 2>/dev/null |
  while IFS= read -r f; do
    case $f in *.db|*.db-wal|*.db-shm|*.gz) continue ;; esac
    [[ -L $f ]] && continue
    fuser -s "$f" 2>/dev/null && continue          # open by a process: leave it
    printf '%s\t%s\n' "$(stat -c %s "$f")" "$f"
  done
}
staging_dirs() { find "$HOME_DIR" -maxdepth 1 -type d -name '.kalshi-preview-stage.*' -mtime +1 2>/dev/null; }

plan() {
  local free p d j logs lb sb s
  free=$(free_bytes); [[ $free =~ ^[0-9]+$ ]] || { say "cannot measure free space"; exit 2; }
  say "free now: $(gib "$free") on the journal filesystem (deployment needs >= 10 GiB; target $(gib "$TARGET_BYTES"))"
  p=$(pip_bytes); d=$(dnf_bytes); j=$(journald_bytes)
  logs=$(inactive_logs); lb=$(awk -F'\t' '{s+=$1} END{print s+0}' <<<"$logs")
  sb=0; while IFS= read -r s; do [[ -n $s ]] && sb=$((sb + $(du -sb "$s" | cut -f1))); done < <(staging_dirs)
  say "1 pip download cache:            $(gib "$p")"
  say "2 dnf cached packages:           $(gib "$d")$( (( SUDO_OK )) || echo ' (no passwordless sudo: skipped)')"
  say "3 systemd journal above 500 MB:  $(gib "$j")$( (( SUDO_OK )) || echo ' (no passwordless sudo: skipped)')"
  say "4 inactive logs > 50 MB (gzip keeps every line; ~90% smaller): $(gib "$lb") before compression$( (( HAVE_FUSER )) || echo ' (fuser missing: skipped)')"
  [[ -n $logs ]] && sort -rn <<<"$logs" | head -n 15 | awk -F'\t' '{printf "    %8.2f GiB  %s\n", $1/1073741824, $2}'
  say "5 old deployment staging dirs:   $(gib "$sb")"
  RECLAIM_ESTIMATE=$(( p + d + j + lb * 9 / 10 + sb ))
  say "estimated reclaim: $(gib "$RECLAIM_ESTIMATE") -> projected free $(gib $(( free + RECLAIM_ESTIMATE )))"
  say "--- NOT touched (listed for your decision only):"
  timeout 30 find "$HOME_DIR" -xdev -maxdepth 4 \( -name '*.db' -o -name '*.db-wal' \) -size +500M -printf '%s\t%p\n' 2>/dev/null |
    sort -rn | head -n 8 | awk -F'\t' '{printf "    %8.2f GiB  %s\n", $1/1073741824, $2}'
  timeout 30 du -sh "$HOME_DIR"/kalshi-backups/* "$HOME_DIR"/kalshi-research 2>/dev/null | sort -rh | head -n 6 | sed 's/^/    /'
  if (( free + RECLAIM_ESTIMATE >= TARGET_BYTES )); then say "PLAN_SUFFICIENT"; else say "PLAN_INSUFFICIENT: safe categories cannot reach $(gib "$TARGET_BYTES"); nothing will be applied"; fi
}

health() {
  local port
  for port in 8768 8765; do
    "$PY" -P - "$port" <<'PY' 2>/dev/null || say "health $port: unavailable"
import json, sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
port = sys.argv[1]
try:
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/healthz", timeout=8) as r:
        code, h = r.status, json.load(r)
except HTTPError as e:
    code, h = e.code, {}
print(f"health {port}: HTTP {code} {h.get('status')} build={str(h.get('build_commit'))[:12]} signals_only={h.get('signals_only')} workers_healthy={(h.get('workers') or {}).get('healthy')}")
PY
  done
}

case ${1:-} in
  plan) plan ;;
  apply)
    [[ ${2:-} == --approve && ${3:-} == RECLAIM && $# -eq 3 ]] || { say "usage: $0 apply --approve RECLAIM"; exit 2; }
    plan
    (( $(free_bytes) + RECLAIM_ESTIMATE >= TARGET_BYTES )) || { say "REFUSED: projected free below target; nothing changed"; exit 2; }
    say "=== applying approved data-free reclaim"
    if [[ -d $HOME_DIR/.cache/pip && ! -L $HOME_DIR/.cache/pip ]]; then rm -rf -- "$HOME_DIR/.cache/pip" && say "1 removed pip download cache"; fi
    if (( SUDO_OK )); then
      timeout 120 sudo -n dnf clean packages >/dev/null 2>&1 && say "2 dnf cached packages cleaned"
      timeout 120 sudo -n journalctl --vacuum-size=500M >/dev/null 2>&1 && say "3 systemd journal vacuumed to 500 MB"
    fi
    while IFS=$'\t' read -r _ f; do
      [[ -n $f && -f $f && ! -L $f ]] || continue
      fuser -s "$f" 2>/dev/null && { say "4 skipped (now open): $f"; continue; }
      if nice -n 19 gzip -9 -- "$f"; then say "4 compressed (content preserved): $f.gz"; else say "4 compression failed, left as is: $f"; fi
    done < <(inactive_logs)
    while IFS= read -r s; do [[ -n $s && -d $s && ! -L $s ]] && rm -rf -- "$s" && say "5 removed staging dir $s"; done < <(staging_dirs)
    after=$(free_bytes)
    say "free after: $(gib "$after")"
    health
    if (( after >= 10 * 1024 * 1024 * 1024 )); then say "RECLAIM_OK: >= 10 GiB free; deployment storage check will pass"; exit 0; fi
    say "RECLAIM_SHORT: still below 10 GiB; nothing else was touched"; exit 4 ;;
  *) say "usage: $0 plan | apply --approve RECLAIM"; exit 2 ;;
esac
