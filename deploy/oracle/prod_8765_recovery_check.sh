#!/usr/bin/env bash
# READ-ONLY pre-recovery check for production 8765 (bounded, low priority).
#
#  1. Where stderr goes: the peer of the process's stderr socket (journald,
#     sshd, ...) and the process's systemd unit (from /proc/<pid>/cgroup).
#  2. Recover the original collector traceback from journald if it exists
#     (system and user journals, the unit, and the PID, 2026-11-11..14);
#     kernel storage errors (I/O, filesystem, no space) in the same window.
#  3. Integrity of the journal WITHOUT writing: SQLite opened mode=ro with
#     query_only; header/page geometry; table-scoped quick_check of the
#     tables the collector writes (SQLite >= 3.33), otherwise a full read of
#     those tables and their index; WAL/SHM sizes.
#  4. Storage now: bytes and inodes free.
# Never signals, restarts, checkpoints, vacuums, writes or deletes anything,
# and changes no logging configuration. Credential-like text is redacted.
set -uo pipefail
PORT=${CHECK_PORT:-8765}
PY=${CHECK_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c2 -n7 -p $$ >/dev/null 2>&1 || true
R() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }
section() { printf '\n===== %s =====\n' "$*"; }
SUDO=(); timeout 5 sudo -n true >/dev/null 2>&1 && SUDO=(sudo -n)

P=$( ("${SUDO[@]}" ss -Hltnp "sport = :$PORT" 2>/dev/null || ss -Hltnp "sport = :$PORT") | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2)
[[ -n $P ]] || { echo "no listener on $PORT"; exit 2; }
mapfile -d '' -t A <"/proc/$P/cmdline"
J=""; for ((i=0; i<${#A[@]}-1; i++)); do [[ ${A[i]} == --journal ]] && J=${A[i+1]}; done
[[ -n $J && $J != /* ]] && J=$(readlink -f "/proc/$P/cwd")/$J

section "1. LAUNCH MECHANISM AND STDERR DESTINATION (pid $P)"
echo "started: $(ps -o lstart= -p "$P")  parent: $(awk '/^PPid:/{print $2}' "/proc/$P/status") ($(cat "/proc/$(awk '/^PPid:/{print $2}' "/proc/$P/status")/comm" 2>/dev/null))"
echo "cgroup: $(tr '\n' ' ' <"/proc/$P/cgroup" | head -c 300)"
UNIT=$(grep -oE '[^/]+\.service' "/proc/$P/cgroup" | head -n1)
echo "systemd unit: ${UNIT:-none (not started by systemd)}"
for fd in 1 2; do
  t=$(readlink "/proc/$P/fd/$fd"); echo "fd$fd -> $t"
  ino=$(grep -oE '[0-9]+' <<<"$t")
  [[ $t == socket:* && -n $ino ]] && echo "  socket peer: $("${SUDO[@]}" ss -xp 2>/dev/null | grep -E "(^|[^0-9])$ino([^0-9]|$)" | grep -oE 'users:\(\("[^"]+"' | sort -u | tr '\n' ' ')"
done
[[ -n $UNIT ]] && timeout 10 systemctl status "$UNIT" --no-pager 2>&1 | head -n 12 | R

section "2. ORIGINAL TRACEBACK FROM JOURNALD (2026-11-11 .. 2026-11-14)"
found=0
for scope in "" "--user"; do
  for sel in "_PID=$P" ${UNIT:+"_SYSTEMD_UNIT=$UNIT"}; do
    out=$(timeout 40 "${SUDO[@]}" journalctl $scope --no-pager -o short-iso --since 2026-11-11 --until 2026-11-14 "$sel" 2>/dev/null | grep -B4 -A22 -E 'Exception in thread|OperationalError' | head -n 80)
    if [[ -n $out ]]; then found=1; echo "--- journalctl $scope $sel"; R <<<"$out"; fi
  done
done
(( found )) || echo "no traceback in journald for this PID/unit in that window (output likely went to a closed SSH session)"
echo "--- kernel storage errors in the same window"
timeout 40 "${SUDO[@]}" journalctl -k --no-pager -o short-iso --since 2026-11-11 --until 2026-11-14 2>/dev/null \
  | grep -iE 'I/O error|EXT4-fs error|XFS .*error|No space left|blk_update_request|Buffer I/O' | head -n 20 || true
echo "(empty above = no kernel storage errors recorded or journal not readable)"

section "3. JOURNAL INTEGRITY (read-only, query_only)"
ls -la "$J" "$J-wal" "$J-shm" 2>&1
timeout -k 5 900 "$PY" -P - "$J" <<'PY'
import sqlite3, sys, time
path = sys.argv[1]
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
db.execute("PRAGMA query_only=ON")
print("sqlite library", sqlite3.sqlite_version)
ps, pc = db.execute("PRAGMA page_size").fetchone()[0], db.execute("PRAGMA page_count").fetchone()[0]
import os
print(f"page_size {ps} page_count {pc} -> {ps * pc} bytes; file {os.path.getsize(path)} bytes; journal_mode {db.execute('PRAGMA journal_mode').fetchone()[0]}")
tables = [t for t in ("btc_candles", "feed_observations", "predictions") if
          db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
scoped = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2]) >= (3, 33)
for t in tables:
    t0 = time.monotonic()
    if scoped and t == "btc_candles":
        res = [r[0] for r in db.execute(f"PRAGMA quick_check({t})").fetchall()]
        print(f"quick_check({t}): {res[:5]} ({time.monotonic() - t0:.1f} s)")
    elif t == "btc_candles":
        n = db.execute("SELECT COUNT(*), SUM(LENGTH(payload)) FROM btc_candles").fetchone()
        m = db.execute("SELECT COUNT(*) FROM btc_candles INDEXED BY btc_candles_start WHERE start IS NOT NULL").fetchone()
        print(f"full read of btc_candles: rows {n[0]} bytes {n[1]}; via index rows {m[0]} ({time.monotonic() - t0:.1f} s)")
    else:
        last = db.execute(f"SELECT MAX(rowid) FROM {t}").fetchone()[0]
        print(f"{t}: newest rowid {last} readable")
print("newest candle start:", db.execute("SELECT MAX(start) FROM btc_candles").fetchone()[0])
print("INTEGRITY_PROBE_DONE (no write transaction was opened)")
PY

section "4. STORAGE NOW"
df -h "$J" | tail -n 1
df -i "$J" | tail -n 1
echo
echo "RECOVERY_CHECK_COMPLETE (read-only; nothing was changed)"
