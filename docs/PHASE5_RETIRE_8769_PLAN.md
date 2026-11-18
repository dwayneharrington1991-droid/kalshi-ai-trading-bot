# Retirement plan for test service 8769 (DRAFT, awaiting Codex verification)

Author: Claude. Nothing in this plan has been executed. Do not run any step
until (1) Codex confirms 8769 is no longer needed and verifies this plan, and
(2) the owner gives explicit approval. Production 8765 and Phase 5 preview
8768 are never touched; 8767 is a separate decision.

## What 8769 is (evidence already collected, read-only)

| Fact | Value | Source |
|---|---|---|
| PID | 475993 | inventory 44f8101 (partial run) |
| Directory | `/home/opc/kalshi-gap-repair-stage` | process profile b6af8ad |
| Code | commit `7ba1763` "Correct historical trade candle recovery" (2026-11-16 02:42 UTC, branch `codex/opening-gap-repair`) | inventory |
| Started | about 2026-11-16 02:45 UTC, minutes after that commit | process profile (uptime 47.6 h) |
| Launch | shell session (not systemd) | inventory |
| Journal | `logs/repair-test-8769.db`, 4.1 GB | process profile |
| Cost | 22.2% of the single CPU, 571 MiB RSS (host 1.7 GB into swap) | process profile |
| Code superseded | `7ba1763` is an ancestor of the running 8768 build `6c19623` and of every Phase 5 build (`ae05d37`, `8c40a10`, `44f8101`); its change (2 files, `monitor.py` + one test) is already live on 8768 | `git merge-base --is-ancestor` |
| Referenced by repo tooling | no file in the repository refers to port 8769 or its directory | `git grep` |
| Clean stop | `SIGTERM` handler sets the stop event and shuts the HTTP server down; journal guarded by an exclusive `flock` on `repair-test-8769.db.dashboard.lock` | `7ba1763:src/signals_display/server.py` |

Not yet verified (the inventory timed out during connection sampling):
whether any client is connected, and whether any other process reads its
journal. Steps A2 and A3 below close these gaps with single, filtered,
time-limited commands.

## Questions for Codex (please answer in a commit on your branch)

1. Did you launch 8769 to stage `codex/opening-gap-repair`, and is that test
   finished now that `7ba1763` runs on 8768?
2. Does anything still read `repair-test-8769.db` (research scripts,
   comparisons, audits), or should it be kept as evidence only?
3. Do you agree with the steps, gates and rollback below?

## Plan (run on Oracle as opc, one command at a time; each is bounded)

### A. Pre-checks (read-only). Any failed gate = stop, change nothing.

A1. Identity: the listener on 8769 must still be PID 475993 in the expected
directory with the expected journal and commit.

```bash
timeout 5 ss -Hltnp 'sport = :8769'
readlink /proc/475993/cwd
tr '\0' '\n' </proc/475993/cmdline | grep -E -- '^--port$|^8769$|repair-test-8769\.db$'
timeout 5 git -C /home/opc/kalshi-gap-repair-stage rev-parse --short HEAD
```
Gate: PID 475993, cwd `/home/opc/kalshi-gap-repair-stage`, `--port 8769`,
journal `repair-test-8769.db`, HEAD `7ba1763`.

A2. Clients (filtered to this port only; run three times about 20 s apart):
```bash
timeout 5 ss -Htnp state established '( sport = :8769 )' | wc -l
```
Gate: 0 every time. If not 0, show the line(s) without `wc -l` and stop.

A3. Journal users (C tool, bounded):
```bash
cd /home/opc/kalshi-gap-repair-stage/logs && timeout 15 fuser -v repair-test-8769.db* 2>&1
```
Gate: only PID 475993 listed.

A4. Supervision: confirms nothing would restart it or depends on it.
```bash
timeout 5 crontab -l 2>/dev/null | grep -c 8769
timeout 5 systemctl list-units --all --no-pager 2>/dev/null | grep -c 8769
```
Gate: both 0.

A5. Record the protected services (must be identical after the stop):
```bash
timeout 5 ss -Hltnp 'sport = :8765' ; timeout 5 ss -Hltnp 'sport = :8768'
```

### B. Capture rollback identity (private; never printed)

```bash
umask 077
R=/home/opc/.kalshi-deploy/retired-8769-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$R" && chmod 700 "$R"
cp /proc/475993/cmdline "$R/argv"        # NUL-separated
cp /proc/475993/environ "$R/env"         # NUL-separated; may hold secrets: 0600, never printed
readlink /proc/475993/cwd >"$R/cwd"
stat -c '%n %s %y' /home/opc/kalshi-gap-repair-stage/logs/repair-test-8769.db* >"$R/journal-files"
chmod 600 "$R"/*
echo "$R"
```

### C. Graceful stop (only after A and B pass and approval is given)

Guarded against PID reuse: the PID must still own port 8769.
```bash
timeout 5 ss -Hltnp 'sport = :8769' | grep -q 'pid=475993,' && kill -TERM 475993
for i in $(seq 30); do kill -0 475993 2>/dev/null || break; sleep 1; done
kill -0 475993 2>/dev/null && echo STILL_RUNNING || echo STOPPED
timeout 5 ss -Hltnp 'sport = :8769' | wc -l      # expect 0
```
If `STILL_RUNNING` after 30 s: do NOT force-kill. Report and decide manually.

Post-checks:
```bash
timeout 5 ss -Hltnp 'sport = :8765' ; timeout 5 ss -Hltnp 'sport = :8768'   # same PIDs as A5
cat /proc/loadavg; free -m
```

### D. Journal preservation

- Do not delete, move, rename, vacuum or rewrite `repair-test-8769.db`,
  its `-wal`/`-shm` files, its `.dashboard.lock`, or the directory.
- SQLite WAL makes every committed transaction durable; the graceful stop
  closes connections normally. The files stay exactly where they are.
- Freeing its 4.1 GB of disk is a separate, explicitly approved decision.
- Optional (only if Codex wants an offline evidence copy and disk allows):
  `sqlite3 repair-test-8769.db ".backup '/path/elsewhere.db'"` after the stop.
  Not part of this plan by default (the host is at 92% disk).

### E. Rollback (restart 8769 exactly as it was)

Same argv, working directory and environment; no values are printed:
```bash
R=<directory printed in step B>
cd "$(cat "$R/cwd")" || exit 1
mapfile -d '' -t ENVV <"$R/env"; mapfile -d '' -t ARGV <"$R/argv"
nohup env -i "${ENVV[@]}" "${ARGV[@]}" >>logs/restart-8769.log 2>&1 </dev/null &
sleep 20; timeout 5 ss -Hltnp 'sport = :8769'
```
The journal is untouched, so the restarted service resumes from it; the
exclusive journal lock guarantees it cannot start while another process owns
the journal.

## Expected effect

About 22% of the single CPU and about 570 MiB of memory freed, reducing
swap pressure. This directly helps 8768 meet the 5-second freshness limit and
is a precondition for retrying the Phase 5 deployment with the performance
repairs (`b6af8ad`, `8c40a10`) after Codex review.

## Out of scope

8765 (production) and 8768 (Phase 5 preview): untouched. 8767 (commit
`8cc5b0a`, 2026-11-02, branch `codex/kxbtc15m-signals-display`; NOT contained
in the 8768 builds, so it may hold unique features): separate decision after
Codex confirms whether anything still depends on it.
