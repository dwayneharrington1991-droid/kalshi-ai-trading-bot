#!/usr/bin/env bash
# READ-ONLY Phase 1 audit of the ACTIVE bot on port 8768 (bounded, low priority).
#
#  A. Deployed identity: pid, start, build, journal, artifact, cwd, python;
#     deployed build compared with this branch (commits and src/ changes).
#  B. Model: artifact SHA-256, model id, threshold, components, input count.
#  C. Live health: workers, readiness reasons, quote/reference ages, latency
#     percentiles, /api/state response time.
#  D. Verified accuracy from the 8768 journal (read-only connection).
#  E. The previous guarded deployment jobs: result and exact failing gate.
#  F. Dependencies of 8768 on 8765: open files, sockets, shared paths, and the
#     NAMES (never values) of environment variables that mention 8765's port
#     or journal.
#  G. 8765 storage inventory for a future retirement plan (listing only).
# Never signals, restarts, writes, checkpoints or deletes anything; never
# prints environment values; credential-like text is redacted.
set -uo pipefail
REPO=${AUDIT_REPO:-/home/opc/kalshi-ai-trading-bot}
JOBS=${AUDIT_JOBS:-/home/opc/.kalshi-deploy/preview-8768/jobs}
renice -n 19 -p $$ >/dev/null 2>&1 || true
ionice -c2 -n7 -p $$ >/dev/null 2>&1 || true
R() { sed -E 's/((secret|token|password|passwd|api[_-]?key|private[_-]?key|authorization)[^=:]*[=:]).*/\1 [REDACTED]/I'; }
section() { printf '\n===== %s =====\n' "$*"; }
SUDO=(); timeout 5 sudo -n true >/dev/null 2>&1 && SUDO=(sudo -n)
pid_of() { ( "${SUDO[@]}" ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }
flag() { local i; for ((i=0; i<${#A[@]}-1; i++)); do [[ ${A[i]} == "$1" ]] && { printf '%s' "${A[i+1]}"; return; }; done; }
abs() { [[ -z $1 || $1 == /* ]] && printf '%s' "$1" || printf '%s/%s' "$2" "$1"; }

P=$(pid_of 8768); [[ -n $P ]] || { echo "no listener on 8768"; exit 2; }
mapfile -d '' -t A <"/proc/$P/cmdline"
CWD=$(readlink -f "/proc/$P/cwd"); EXE=$(readlink -f "/proc/$P/exe")
BUILD=$(flag --build-commit); J=$(abs "$(flag --journal)" "$CWD"); ART=$(abs "$(flag --opening-model-artifact)" "$CWD")
PY=${A[0]}; [[ $PY == /* ]] || PY=$EXE

section "A. DEPLOYED 8768 IDENTITY"
echo "pid $P  started $(ps -o lstart= -p "$P")  cpu $(ps -o pcpu= -p "$P")%  rss $(ps -o rss= -p "$P") KiB"
echo "build: ${BUILD:-unknown}"; echo "cwd: $CWD"; echo "python: $EXE"
echo "journal: $J"; echo "artifact: $ART"
echo "flags: $(for a in "${A[@]}"; do [[ $a == --* ]] && printf '%s ' "$a"; done)"
if [[ -n $BUILD ]] && git -C "$REPO" cat-file -e "$BUILD^{commit}" 2>/dev/null; then
  B=refs/remotes/origin/claude/independent-review-2026-11-18
  echo "branch head: $(git -C "$REPO" rev-parse --short "$B" 2>/dev/null)"
  echo "commits on branch after the deployed build: $(git -C "$REPO" rev-list --count "$BUILD..$B" 2>/dev/null)"
  echo "src/ change since deployed build: $(git -C "$REPO" diff --shortstat "$BUILD" "$B" -- src 2>/dev/null)"
  git -C "$REPO" diff --stat "$BUILD" "$B" -- src 2>/dev/null | tail -n 12
else
  echo "deployed build not found in $REPO (fetch the branch first)"
fi

section "B. MODEL (read-only)"
timeout 30 "$PY" -I - "$ART" <<'PY' || echo "artifact unreadable"
import hashlib, json, sys
data = open(sys.argv[1], "rb").read(); a = json.loads(data); m = a.get("model") or {}
parts = m.get("components") or [m]
names = {n for p in parts for n in ((p.get("preprocessor") or {}).get("names") or [])}
print("sha256", hashlib.sha256(data).hexdigest()); print("model_id", a.get("model_id"))
print("threshold", (a.get("decision_policy") or {}).get("threshold"), "| components", len(parts), "| inputs", len(names))
print("created", a.get("created_at"), "| kind", m.get("kind") or m.get("type"))
PY

section "C. LIVE HEALTH, FRESHNESS, LATENCY"
timeout 90 "$PY" -I - <<'PY' || echo "live probe failed"
import json, time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
o = build_opener(ProxyHandler({}))
def get(path):
    t = time.monotonic()
    try:
        with o.open("http://127.0.0.1:8768" + path, timeout=15) as r:
            c, b = r.status, json.load(r)
    except HTTPError as e:
        c, b = e.code, json.load(e)
    return c, b, (time.monotonic() - t) * 1000
c, h, ms = get("/healthz")
print(f"healthz HTTP {c} {h.get('status')} build {str(h.get('build_commit'))[:12]} signals_only {h.get('signals_only')} ({ms:.0f} ms)")
for n, w in ((h.get("workers") or {}).get("workers") or {}).items():
    print(f"  worker {n}: {w.get('status')} alive={w.get('thread_alive')} cycles={w.get('cycles')} "
          f"since_completion={w.get('seconds_since_completion')} last_error={w.get('last_error_type')}")
ready = {}
for i in range(6):
    c, r, ms = get("/readyz")
    key = f"{c} {r.get('status')} {sorted(r.get('stale_reasons') or [])}"
    ready[key] = ready.get(key, 0) + 1
    time.sleep(2)
print("readyz over 6 samples (2 s apart):", ready)
times = []
for i in range(3):
    c, s, ms = get("/api/state?view=main"); times.append(round(ms))
mh, rt = s.get("market_health") or {}, s.get("runtime") or {}
print(f"/api/state HTTP {c} response ms {times}")
print(f"contract {mh.get('contract')} quote_age {mh.get('quote_age_seconds')} reference_age {mh.get('reference_age_seconds')}")
print("reasons", mh.get("contract_quote_reasons"), mh.get("reference_reasons"))
print("latency_percentiles", json.dumps(rt.get("latency_percentiles"), default=str)[:900])
di = s.get("decision_intelligence") or {}
print("execution_capability", di.get("execution_capability"))
PY

section "D. VERIFIED ACCURACY (journal opened read-only)"
( cd "$CWD" && timeout -k 5 240 "$PY" -P - "$J" ) <<'PY' || echo "scoreboard unavailable"
import json, sys, time
sys.path.insert(0, ".")
from src.signals_display.monitor import Journal
j = Journal.__new__(Journal); j.path = sys.argv[1]
t = time.monotonic(); b = j.verified_opening_scoreboard()
print(f"scoreboard ({time.monotonic() - t:.1f} s):", json.dumps(b, default=str)[:1500])
with j.read() as db:
    rows = db.execute("SELECT b.payload FROM btc_engine_opening b WHERE b.mode='PUBLIC MARKET DATA'").fetchall()
counts = {}
for (p,) in rows:
    try:
        d = json.loads(p) if p else {}
    except Exception:
        d = {}
    k = f"{d.get('direction')} / {(d.get('confidence') or {}).get('status')}"
    counts[k] = counts.get(k, 0) + 1
print("openings by direction / confidence:", json.dumps(dict(sorted(counts.items(), key=lambda kv: -kv[1]))))
PY

section "E. PREVIOUS GUARDED DEPLOYMENT JOBS (newest 3)"
for job in $(ls -1dt "$JOBS"/*/ 2>/dev/null | head -n 3); do
  echo "--- job $(basename "$job")  $(stat -c %y "$job" | cut -c1-19)"
  for f in state result sha; do [[ -f $job/$f ]] && echo "  $f: $(head -c 200 "$job/$f" | tr '\n' ' ')"; done
  [[ -f $job/soak.json ]] && echo "  soak.json: $("$PY" -I -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("result"),"|",d.get("reason"),"| samples",d.get("samples"))' "$job/soak.json" 2>/dev/null)"
  grep -hE 'GATE FAIL|FAILED|ROLLED_BACK|DEPLOY_RESULT|free storage|soak passed' "$job/log" 2>/dev/null | tail -n 6 | cut -c1-300 | R | sed 's/^/  /'
done

section "F. DOES 8768 DEPEND ON 8765?"
P65=$(pid_of 8765)
if [[ -n $P65 ]]; then
  mapfile -d '' -t A <"/proc/$P65/cmdline"
  CWD65=$(readlink -f "/proc/$P65/cwd"); J65=$(abs "$(flag --journal)" "$CWD65")
  echo "8765: pid $P65 cwd $CWD65 journal $J65"
else
  CWD65=$REPO; J65=$REPO/signals_monitor.db; echo "8765: no listener (assuming $J65)"
fi
n=0; while IFS= read -r t; do [[ $t == "$J65"* ]] && { n=$((n+1)); echo "  8768 HAS OPEN: $t"; }; done < <(for d in /proc/"$P"/fd/*; do readlink "$d" 2>/dev/null; done)
echo "8768 open handles on the 8765 journal: $n"
echo "8768 TCP connections to :8765: $(ss -Htnp 2>/dev/null | grep "pid=$P," | grep -c ':8765 ')"
echo "8768 cwd inside 8765's checkout: $([[ $CWD == "$CWD65" || $CWD == "$CWD65"/* ]] && echo YES || echo no)"
echo "8768 python inside 8765's checkout: $([[ $EXE == "$CWD65"/* || $PY == "$CWD65"/* ]] && echo YES || echo no) ($EXE)"
echo "8768 artifact inside 8765's checkout: $([[ $ART == "$CWD65"/* ]] && echo YES || echo no)"
echo "8768 journal is the 8765 journal: $([[ $(readlink -f "$J") == $(readlink -f "$J65") ]] && echo YES || echo no)"
echo "8768 environment variable NAMES mentioning 8765 or its journal: $(tr '\0' '\n' <"/proc/$P/environ" 2>/dev/null | grep -E '8765|signals_monitor' | cut -d= -f1 | tr '\n' ' ')"
echo "8768 open files under 8765's checkout (shared code/data):"
for d in /proc/"$P"/fd/*; do t=$(readlink "$d" 2>/dev/null); [[ $t == "$CWD65"/* ]] && echo "  $t"; done | sort -u | head -n 15
echo "8768 mapped modules under 8765's checkout: $(grep -c " $CWD65/" "/proc/$P/maps" 2>/dev/null)"

section "G. 8765 STORAGE INVENTORY (listing only; nothing removed)"
ls -la "$J65" "$J65-wal" "$J65-shm" 2>&1 | sed 's/^/  /'
timeout 60 find /home/opc -xdev -maxdepth 4 -name "$(basename "$J65")*" -size +100M -printf '  %s bytes  %TY-%Tm-%Td  %p\n' 2>/dev/null | sort -rn | head -n 12
timeout 60 du -sh /home/opc/kalshi-backups/* 2>/dev/null | sort -rh | head -n 8 | sed 's/^/  backup dir: /'
df -h /home/opc | tail -n 1
echo
echo "AUDIT_8768_COMPLETE (read-only; nothing was changed)"
