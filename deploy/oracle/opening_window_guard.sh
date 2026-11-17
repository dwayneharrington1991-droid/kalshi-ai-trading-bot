#!/usr/bin/env bash
# Planned-restart protection around every KXBTC15M opening (Phase 5).
#
# Source this file and call `opening_window_wait` immediately before a PLANNED
# stop of a signals process. It waits (bounded) until the wall clock is outside
# the protected window: from 90 s before until 150 s after each :00/:15/:30/:45
# opening. A restart inside that window can permanently lose an opening.
#
# Emergency recovery is never blocked: rollback/restore paths do not call this,
# and PREVIEW_EMERGENCY_OVERRIDE=1 skips the wait with a loud log line.
# OPENING_GUARD_NOW_EPOCH is a test hook for the current time; OPENING_GUARD_SLEEP
# names the sleep command (tests replace it).

OPENING_GUARD_PERIOD=900
OPENING_GUARD_BEFORE=90
OPENING_GUARD_AFTER=150

opening_window_remaining() {
  # Prints seconds until the protected window ends (0 when outside it).
  local now=${1:-${OPENING_GUARD_NOW_EPOCH:-$(date -u +%s)}}
  local pos=$(( now % OPENING_GUARD_PERIOD ))
  if (( pos >= OPENING_GUARD_PERIOD - OPENING_GUARD_BEFORE )); then
    printf '%s\n' $(( OPENING_GUARD_PERIOD - pos + OPENING_GUARD_AFTER + 1 ))
  elif (( pos <= OPENING_GUARD_AFTER )); then
    printf '%s\n' $(( OPENING_GUARD_AFTER - pos + 1 ))
  else
    printf '0\n'
  fi
}

opening_window_wait() {
  local label=${1:-planned restart} remaining waited=0
  if [[ ${PREVIEW_EMERGENCY_OVERRIDE:-0} == 1 ]]; then
    printf 'OPENING_GUARD: EMERGENCY OVERRIDE - %s proceeds inside the opening window by explicit request\n' "$label" >&2
    return 0
  fi
  while :; do
    remaining=$(opening_window_remaining)
    (( remaining == 0 )) && break
    # One window lasts at most 241 s; anything longer means a broken clock.
    (( waited + remaining <= 300 )) || { printf 'OPENING_GUARD: refusing %s; clock check failed\n' "$label" >&2; return 1; }
    printf 'OPENING_GUARD: %s deferred %ss to protect the KXBTC15M opening window\n' "$label" "$remaining" >&2
    ${OPENING_GUARD_SLEEP:-sleep} "$remaining"
    waited=$(( waited + remaining ))
    [[ -n ${OPENING_GUARD_NOW_EPOCH:-} ]] && OPENING_GUARD_NOW_EPOCH=$(( OPENING_GUARD_NOW_EPOCH + remaining ))
  done
  printf 'OPENING_GUARD: %s allowed outside the opening window\n' "$label" >&2
}
