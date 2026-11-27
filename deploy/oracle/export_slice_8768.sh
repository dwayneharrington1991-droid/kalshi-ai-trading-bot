#!/usr/bin/env bash
# Export the compact research slice of the 8768 journal for OFF-SERVER evaluation.
#
#   export_slice_8768.sh <commit-sha>
#
# 1. Stops this project's own CPU-starved shadow research run, if one is alive
#    (only processes started by shadow_upgrade_8768.sh under kalshi-research
#    whose worker runs src.signals_display.*; never 8768, 8765 or 8767).
# 2. Reads the 8768 journal READ-ONLY (mode=ro, query_only) and writes
#    research_slice output, the first shadow report and the pinned as-of time
#    into /home/opc/kalshi-research/slice-<sha12>, then packs them as base64
#    text parts (<= 18 MB each) for upload. Idempotent: a finished export is
#    only listed again. Runs at normal CPU priority for a few minutes so it
#    finishes instead of starving; I/O at lowered priority.
set -uo pipefail
SHA=${1:-}
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || { echo "usage: $0 <40-hex commit sha>"; exit 2; }
REPO=${SLICE_REPO:-/home/opc/kalshi-ai-trading-bot}
ROOT=${SLICE_RUN_ROOT:-/home/opc/kalshi-research}
OUT=$ROOT/slice-${SHA:0:12}
umask 077
pid_of() { ( sudo -n ss -Hltnp "sport = :$1" 2>/dev/null || ss -Hltnp "sport = :$1" ) | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2; }

if compgen -G "$OUT/slice.part-*.txt" >/dev/null; then
  echo "SLICE_READY (already exported):"; ls -l "$OUT"/slice.part-*.txt | awk '{print "  " $5 " bytes  " $9}'; exit 0
fi

for f in "$ROOT"/*/pid; do                         # our own starved research runs only
  [[ -f $f ]] || continue
  p=$(cat "$f"); kill -0 "$p" 2>/dev/null || continue
  child=$(pgrep -P "$p" | head -n1)
  if [[ -n $child ]] && tr '\0' ' ' <"/proc/$child/cmdline" | grep -q 'src.signals_display.'; then
    echo "stopping starved research run $(dirname "$f") (pid $p); its work moves off Oracle"
    kill -TERM "$p" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  fi
done

P=$(pid_of 8768); [[ -n $P ]] || { echo "BLOCKED: no listener on 8768"; exit 2; }
mapfile -d '' -t A <"/proc/$P/cmdline"
CWD=$(readlink -f "/proc/$P/cwd"); PY=${A[0]}; [[ $PY == /* ]] || PY=$(readlink -f "/proc/$P/exe")
J=""; for ((i=0; i<${#A[@]}-1; i++)); do [[ ${A[i]} == --journal ]] && J=${A[i+1]}; done
[[ -n $J && $J != /* ]] && J=$CWD/$J
[[ -f $J ]] || { echo "BLOCKED: 8768 journal not found"; exit 2; }
PREVIOUS=$ROOT/shadow-upgrade-e2357e058e67/report.json
[[ -f $PREVIOUS ]] || { echo "BLOCKED: first shadow report $PREVIOUS not found"; exit 2; }
(( $(df -Pk "$ROOT" | awk 'NR==2{print $4}') >= 3145728 )) || { echo "BLOCKED: < 3 GiB free for the export"; exit 3; }
git -C "$REPO" cat-file -e "$SHA^{commit}" 2>/dev/null || { echo "BLOCKED: commit $SHA not fetched"; exit 2; }

rm -rf "$OUT" && mkdir -p "$OUT/code" && git -C "$REPO" archive "$SHA" src | tar -x -C "$OUT/code" || { echo "BLOCKED: code extract failed"; exit 2; }
cp "$PREVIOUS" "$OUT/previous_report.json"
PIN=$ROOT/combined-opening-6cf64dff6fba/asof
[[ -f $PIN ]] && cp "$PIN" "$OUT/pinned_asof.txt"
NOW=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
echo "exporting from $(basename "$J") (read-only), as-of $NOW ..."
( cd "$OUT/code" && timeout 1500 ionice -c2 -n6 "$PY" -m src.signals_display.research_slice \
    --journal "$J" --output "$OUT/slice.db" --asof "$NOW" ) || { echo "SLICE_FAILED: see above"; exit 4; }
PACK=(slice.db slice.db.manifest.json previous_report.json); [[ -f $OUT/pinned_asof.txt ]] && PACK+=(pinned_asof.txt)
( cd "$OUT" && tar -czf bundle.tgz "${PACK[@]}" ) \
  || { echo "SLICE_FAILED: packing"; exit 4; }
SUM=$(sha256sum "$OUT/bundle.tgz" | cut -d' ' -f1)
base64 -w0 "$OUT/bundle.tgz" | split -b 18000000 -d -a 2 - "$OUT/chunk."
chunks=("$OUT"/chunk.*); n=${#chunks[@]}; i=0
for c in "${chunks[@]}"; do
  i=$((i + 1))
  { printf 'KXSLICE %d %d %s\n' "$i" "$n" "$SUM"; cat "$c"; } >"$OUT/slice.part-$(printf '%02d' "$i").txt" && rm -f "$c"
done
rm -f "$OUT/bundle.tgz"
echo "SLICE_READY: $n part(s), bundle sha256 $SUM, slice $(stat -c %s "$OUT/slice.db") bytes"
ls -l "$OUT"/slice.part-*.txt | awk '{print "  " $5 " bytes  " $9}'
