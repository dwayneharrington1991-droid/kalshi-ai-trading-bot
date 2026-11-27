#!/usr/bin/env bash
set -Eeuo pipefail

# Phase 5 read-only data-readiness run on Oracle. Run from an extracted copy of
# the code. It never stops, starts or signals any process, never writes to a
# journal, artifact or registry, and never changes ports 8765/8767/8768. It
#   1. audits each running signals journal (port 8768 first) read-only, and
#   2. probes Kalshi's PUBLIC market data for a bounded time to see whether an
#      executable quote exists before a KXBTC15M opening.
# Output goes only to /home/opc/kalshi-research/phase5-<timestamp>.

readonly CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
readonly PYTHON=/home/opc/kalshi-ai-trading-bot/.venv/bin/python
readonly OUT_DIR=/home/opc/kalshi-research/phase5-$(date -u +%Y%m%dT%H%M%SZ)
readonly PROBE_SECONDS=${PREOPEN_PROBE_SECONDS:-1200}

say() { printf 'READINESS: %s\n' "$*"; }
block() { printf 'READINESS_BLOCKED: %s\n' "$*" >&2; exit 1; }

argv_value() {
  local pid=$1 flag=$2 index
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline"
  for ((index=0; index<${#argv[@]}-1; index++)); do
    if [[ ${argv[index]} == "$flag" ]]; then
      printf '%s\n' "${argv[index+1]}"
      return 0
    fi
  done
  return 0
}

listener_pid() {
  local listing
  listing=$(sudo -n ss -H -ltnp "sport = :$1" 2>/dev/null || ss -H -ltnp "sport = :$1" 2>/dev/null || true)
  sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$listing" | sort -u | head -1
}

journal_of() {
  local pid=$1 path
  path=$(argv_value "$pid" --journal)
  [[ -n $path ]] || return 0
  [[ $path == /* ]] || path="$(readlink -f "/proc/$pid/cwd")/$path"
  [[ -f $path ]] && readlink -f "$path"
  return 0
}

[[ $(id -un) == opc ]] || block "must run as opc"
[[ -x $PYTHON ]] || block "Python interpreter missing: $PYTHON"
pid=$(listener_pid 8768)
[[ -n $pid ]] || block "no process is listening on preview port 8768"
artifact=$(argv_value "$pid" --opening-model-artifact)
install -d -m 0700 "$OUT_DIR"
run_py() { PYTHONPATH="$CODE_ROOT" PYTHONNOUSERSITE=1 nice -n 15 "$PYTHON" -P "$@"; }
asof=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
say "as-of $asof; preview pid=$pid; artifact=${artifact:-NONE}"

declare -A seen=()
for port in 8768 8765 8767; do
  other=$(listener_pid "$port")
  [[ -n $other ]] || continue
  journal=$(journal_of "$other")
  [[ -n $journal && -z ${seen[$journal]:-} ]] || continue
  seen[$journal]=1
  say "data-readiness audit for port $port journal (read only)"
  args=(--journal "$journal" --asof "$asof" --output "$OUT_DIR/data_readiness_port_$port.json")
  # The deployed artifact's input list applies only to the journal it serves.
  if [[ $port == 8768 && -n $artifact && -f $artifact ]]; then
    args+=(--artifact "$artifact")
  fi
  run_py -m src.signals_display.data_readiness "${args[@]}" \
    | sed "s/^/[port $port] /" | tee -a "$OUT_DIR/summary_phase5.txt" \
    || say "audit unavailable for port $port; continuing"
done
(( ${#seen[@]} > 0 )) || block "could not identify any signals journal"

if (( PROBE_SECONDS > 0 )); then
  say "pre-open Kalshi quote probe: public market data only, ${PROBE_SECONDS}s, one request every 10s"
  run_py -m src.signals_display.kalshi_preopen_probe --duration-seconds "$PROBE_SECONDS" \
    --interval-seconds 10 --output "$OUT_DIR/preopen_probe.json" | tee -a "$OUT_DIR/summary_phase5.txt" \
    || say "probe unavailable; continuing"
fi
say "REPORT_DIR=$OUT_DIR"
printf 'READINESS_COMPLETE journal_unchanged=READ_ONLY services_unchanged=YES model_unchanged=YES trading=DISABLED\n'
