# Phase 5 latency evidence for job 01997b30 (READ-ONLY)

Purpose: decide whether the failed readiness of `ae05d37` (both
`QUOTE_OLDER_THAN_5_SECONDS` and `REFERENCE_OLDER_THAN_5_SECONDS`, with
`/healthz` HEALTHY and 1440 complete candles) was sustained or marginal, and
whether it was the new build or the host.

The preview records at most one forecast sample per second, and only while
its own data is fresh, with the quote and reference receipt times. This
command reads those rows from the preserved journal for 30 minutes before,
during and 30 minutes after the job, per minute:

- sample count (about 60 means at least one fresh cycle per second; 0 means
  the preview judged its data stale for the whole minute);
- median, 90th percentile and maximum quote and reference age at recording.

It also prints the readiness progress lines from the job log, the top CPU
consumers (command names only), and disk usage by directory.

**Read-only:** the journal is opened with SQLite `mode=ro` and `query_only`,
using only indexed lookups bounded to the job window; it runs at the lowest
CPU and I/O priority (it lowers its own priority with `renice`/`ionice` when available) so it does not compete with
the running previews. Nothing is started, stopped, written or deleted, and no
environment variables or command arguments are printed.

Source: `deploy/oracle/collect_latency_evidence.sh` (embedded verbatim).

**Windows PowerShell 5.1: UNVERIFIED** (cannot run in the Linux sandbox).

## Command

```powershell
& {
$ErrorActionPreference = 'Continue'
$JobId = '01997b30-95d3-4642-be05-5a0b48cd691d'
$Sha = 'ae05d37301790388f2cf985413ec5849fde970a1'
$Script = @'
#!/usr/bin/env bash
# READ-ONLY latency, CPU and disk evidence for failed preview-8768 job.
#
#   collect_latency_evidence.sh <job id> <40-char commit>
#
# Measures, from rows the preview itself wrote, how old the contract quote and
# BTC reference were when each per-second forecast sample was recorded, per
# minute, from 30 minutes before the job until 30 minutes after it (the old
# build before, the new build during, the restored old build after). Also
# lists the top CPU consumers (command name only) and where disk is used.
#
# Strictly read-only: the journal is opened with SQLite mode=ro and
# query_only, using only primary-key/indexed lookups (bounded to the job's
# window); no process is signalled, no lock is taken, nothing is written or
# deleted, and no environment variables or command arguments are printed.
set -uo pipefail
umask 077
id=${1:-}; sha=${2:-}
[[ $id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { echo "invalid job id"; exit 2; }
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit"; exit 2; }
STATE_ROOT=${PREVIEW_STATE_ROOT:-/home/opc/.kalshi-deploy/preview-8768}
JOURNAL=${PREVIEW_JOURNAL:-/home/opc/kalshi-preview-upgrade/logs/upgraded-live-8768.db}
PYTHON=${PREVIEW_PYTHON:-/home/opc/kalshi-ai-trading-bot/.venv/bin/python}
JOB=$STATE_ROOT/jobs/$id
# Lowest CPU and I/O priority for this collector only (never other processes).
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c3 -p $$ >/dev/null 2>&1 || true
section() { printf '\n===== %s =====\n' "$*"; }

section "COLLECTED_AT $(date -u +%Y-%m-%dT%H:%M:%SZ)"
created=$(cat "$JOB/created_at" 2>/dev/null)
finished=$(grep -m1 '^FINISHED_AT=' "$JOB/result" 2>/dev/null | cut -d= -f2)
echo "job created_at=${created:-UNKNOWN} finished_at=${finished:-UNKNOWN}"
echo "readiness progress reports in job log: $(grep -c 'PREVIEW_READINESS_PROGRESS' "$JOB/log" 2>/dev/null || echo 0)"
grep -E 'PREVIEW_READINESS_PROGRESS|PREVIEW_READINESS_TIMEOUT|PREVIEW_DEPLOY_BLOCKED|opening-window|stopp|launch|restor' "$JOB/log" 2>/dev/null | head -n 60

section "QUOTE/REFERENCE AGE AT RECORDING, PER MINUTE (from the journal, read-only)"
"$PYTHON" - "$JOURNAL" "${created:-}" "${finished:-}" <<'PY'
import json, sqlite3, statistics, sys
from datetime import datetime, timedelta, timezone

def ts(value):
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

journal, created, finished = sys.argv[1], ts(sys.argv[2]), ts(sys.argv[3])
if not created:
    print("job created_at unknown; cannot bound the window"); raise SystemExit(0)
finished = finished or created + timedelta(minutes=20)
start, end = created - timedelta(minutes=30), finished + timedelta(minutes=30)
db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=5)
db.execute("PRAGMA query_only=ON")
mode = "PUBLIC MARKET DATA"
tickers = [t for (t,) in db.execute(
    "SELECT ticker FROM btc_engine_contracts WHERE closes_at>=? AND closes_at<=? AND mode=?",
    (start.isoformat(), (end + timedelta(minutes=16)).isoformat(), mode))]
minutes = {}
for ticker in tickers:
    for recorded_at, payload in db.execute(
        "SELECT recorded_at,payload FROM btc_engine_predictions WHERE mode=? AND ticker=?",
        (mode, ticker)):
        rec = ts(recorded_at)
        if not rec or not (start <= rec <= end):
            continue
        try:
            body = json.loads(payload)
        except (TypeError, ValueError):
            continue
        q, r = ts(body.get("quote_received_at")), ts(body.get("reference_received_at"))
        key = rec.replace(second=0, microsecond=0)
        bucket = minutes.setdefault(key, {"n": 0, "q": [], "r": []})
        bucket["n"] += 1
        if q: bucket["q"].append((rec - q).total_seconds())
        if r: bucket["r"].append((rec - r).total_seconds())

def fmt(values):
    if not values:
        return "n/a"
    values = sorted(values)
    return f"med {statistics.median(values):6.2f}s p90 {values[int(0.9*(len(values)-1))]:6.2f}s max {values[-1]:6.2f}s"

print("minute (UTC)          phase     samples  quote age at record          reference age at record")
key = start.replace(second=0, microsecond=0)
while key <= end:
    phase = "OLD" if key < created else "JOB" if key <= finished else "AFTER"
    b = minutes.get(key)
    if b:
        print(f"{key:%Y-%m-%d %H:%M}  {phase:<8} {b['n']:7d}  {fmt(b['q']):<28} {fmt(b['r'])}")
    else:
        print(f"{key:%Y-%m-%d %H:%M}  {phase:<8}       0  (no fresh, non-stale sample recorded)")
    key += timedelta(minutes=1)
print("Note: samples are written at most once per second and only from non-stale state,")
print("so 0 samples in a minute means the preview judged its own data stale all minute.")
PY

section "TOP CPU CONSUMERS (command name only)"
ps -eo pid,user,pcpu,pmem,etime,comm --sort=-pcpu | head -n 15
nproc 2>/dev/null | sed 's/^/cpus: /'
cat /proc/loadavg

section "DISK USAGE (sizes only; nothing is deleted)"
df -h /home/opc
du -xsh /home/opc/* 2>/dev/null | sort -rh | head -n 15
echo "--- preview logs directory (largest 15) ---"
ls -laS /home/opc/kalshi-preview-upgrade/logs 2>/dev/null | head -n 16
echo "--- deployment state ---"
du -sh "$STATE_ROOT"/runs "$STATE_ROOT"/jobs 2>/dev/null
ls -1d /home/opc/kalshi-preview-repair-* 2>/dev/null | while read -r d; do du -sh "$d"; done
echo
echo "LATENCY_EVIDENCE_COMPLETE (read-only; nothing was changed)"
'@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($Script -replace "`r`n", "`n")))
$out = Join-Path ([Environment]::GetFolderPath('Desktop')) ('phase5-latency-' + $JobId + '.txt')
ssh -o BatchMode=yes -o ConnectTimeout=20 opc@129.146.120.195 "echo $b64 | base64 -d | bash -s -- $JobId $Sha" | Tee-Object -FilePath $out
Write-Host ''
Write-Host ('Saved to ' + $out + ' - send this file to Claude.')
}
```
