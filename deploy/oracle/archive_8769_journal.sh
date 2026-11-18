#!/usr/bin/env bash
# Archive-then-remove the RETIRED 8769 test journal (storage recovery).
#
#   archive_8769_journal.sh prepare
#       Read-only. Proves the journal files are unused and prints a SHA-256
#       manifest ("MANIFEST <sha256> <bytes> <path>") plus the removal token.
#   archive_8769_journal.sh remove --approve REMOVE-8769-<token>
#       Re-proves the files are unused, recomputes the manifest and refuses
#       unless it is IDENTICAL to the one the token was derived from (same
#       files, same bytes). Then removes exactly those files, and reports free
#       space and 8768/8765 health. Run only after the copy was verified.
#
# Files: repair-test-8769.db and, if present, its -wal, -shm and
# .dashboard.lock. Nothing else is ever touched: no active database, backup,
# model or other file. Unused means: no open file handle in any visible
# process (all users with passwordless sudo), no process command line naming
# the files, and no listener on port 8769.
set -uo pipefail
umask 077
DIR=${ARCHIVE_DIR:-/home/opc/kalshi-gap-repair-stage/logs}
NAME=${ARCHIVE_NAME:-repair-test-$((8000 + 769)).db}     # built at runtime: never in any argv
PORT=${ARCHIVE_PORT:-$((8000 + 769))}
FS=${ARCHIVE_FS_PATH:-/home/opc/kalshi-preview-upgrade/logs}
TARGET_BYTES=$((11 * 1024 * 1024 * 1024))
PY=${ARCHIVE_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
DB=$DIR/$NAME
say() { printf '%s\n' "$*"; }
die() { say "BLOCKED: $*"; exit 2; }
SUDO=(); [[ $(id -u) == 0 ]] || { timeout 5 sudo -n true >/dev/null 2>&1 && SUDO=(sudo -n); }

files() { local f; for f in "$DB" "$DB-wal" "$DB-shm" "$DB.dashboard.lock"; do [[ -f $f && ! -L $f ]] && printf '%s\n' "$f"; done; }

prove_unused() {
  local f p link holders=0 refs=0 cmd
  [[ -f $DB && ! -L $DB ]] || die "$DB not found"
  [[ $(head -c 15 "$DB") == "SQLite format 3" ]] || die "$DB is not a SQLite database"
  # 1. open handles: resolve every visible process's file descriptors
  while IFS= read -r link; do
    for f in $(files); do [[ ${link#* } == "$f" ]] && { holders=$((holders + 1)); say "open by pid ${link%% *}: $f"; }; done
  done < <("${SUDO[@]}" bash -c 'for d in /proc/[0-9]*/fd/*; do t=$(readlink "$d" 2>/dev/null) && echo "${d#/proc/} $t"; done' 2>/dev/null | sed -E 's#^([0-9]+)/fd/[0-9]+ #\1 #')
  (( holders == 0 )) || die "the journal is still open ($holders handle(s))"
  # 2. no process command line names the journal (this script's argv never contains it)
  for p in /proc/[0-9]*; do
    [[ ${p#/proc/} == "$$" || ${p#/proc/} == "$BASHPID" ]] && continue
    cmd=$(tr '\0' ' ' 2>/dev/null <"$p/cmdline") || continue
    [[ $cmd == *"$NAME"* ]] && { refs=$((refs + 1)); say "named by pid ${p#/proc/}: $(cut -c1-120 <<<"$cmd")"; }
  done
  (( refs == 0 )) || die "a process still names the journal"
  # 3. nothing listens on the retired port
  [[ -z $(ss -Hltn "sport = :$PORT" 2>/dev/null) ]] || die "port $PORT has a listener"
  say "unused: 0 open handles (sudo=${#SUDO[@]}), 0 processes naming it, port $PORT free"
}

manifest() { local f; for f in $(files); do printf 'MANIFEST %s %s %s\n' "$(nice -n 19 sha256sum "$f" | cut -d' ' -f1)" "$(stat -c %s "$f")" "$f"; done; }
token_of() { sha256sum <<<"$1" | cut -c1-16; }

health() {
  local port
  for port in 8768 8765; do
    "$PY" -P -c '
import json, sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
try:
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=8) as r:
        c, h = r.status, json.load(r)
except HTTPError as e:
    c, h = e.code, {}
print(f"health {sys.argv[1]}: HTTP {c} {h.get(\"status\")} signals_only={h.get(\"signals_only\")} workers_healthy={(h.get(\"workers\") or {}).get(\"healthy\")}")
' "$port" 2>/dev/null || say "health $port: unavailable"
  done
}

case ${1:-} in
  prepare)
    prove_unused
    m=$(manifest)
    printf '%s\n' "$m"
    say "REMOVAL_TOKEN: REMOVE-8769-$(token_of "$m")"
    say "free now: $(df -B1 --output=avail "$FS" | tail -1 | tr -d ' ') bytes" ;;
  remove)
    [[ ${2:-} == --approve && ${3:-} == REMOVE-8769-* && $# -eq 3 ]] || die "usage: $0 remove --approve REMOVE-8769-<token>"
    prove_unused
    m=$(manifest)
    [[ "REMOVE-8769-$(token_of "$m")" == "$3" ]] || die "files changed since the verified copy (manifest differs); nothing removed"
    say "manifest unchanged since the verified copy; removing exactly:"
    printf '%s\n' "$m"
    while read -r _ _ _ f; do rm -f -- "$f" && say "removed $f"; done <<<"$m"
    free=$(df -B1 --output=avail "$FS" | tail -1 | tr -d ' ')
    say "free after: $free bytes ($(awk -v b="$free" 'BEGIN{printf "%.2f", b/1073741824}') GiB)"
    health
    (( free >= TARGET_BYTES )) && { say "STORAGE_OK: >= 11 GiB free"; exit 0; }
    say "STORAGE_SHORT: below 11 GiB"; exit 4 ;;
  *) die "usage: $0 prepare | remove --approve REMOVE-8769-<token>" ;;
esac
