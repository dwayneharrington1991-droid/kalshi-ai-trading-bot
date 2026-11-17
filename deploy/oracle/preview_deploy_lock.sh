#!/usr/bin/env bash
# Exclusive deployment lock for preview 8768 ONLY (Phase 5).
#
# `preview_deploy_lock` holds an flock on file descriptor 9 for the whole
# install / verify / rollback sequence. A child started by the lock holder
# inherits fd 9 (the same open file description), so its flock succeeds at
# once; any independent installer or switch fails immediately with
# PREVIEW_DEPLOY_LOCKED. Ports 8765/8767 have no relationship to this lock.

PREVIEW_DEPLOY_LOCK_PATH=${PREVIEW_DEPLOY_LOCK_PATH:-/home/opc/.kalshi-deploy/preview-8768/deploy.lock}

preview_deploy_lock() {
  local dir held want
  dir=$(dirname "$PREVIEW_DEPLOY_LOCK_PATH")
  install -d -m 0700 "$dir" || return 1
  [[ -e $PREVIEW_DEPLOY_LOCK_PATH ]] || ( umask 077; : >>"$PREVIEW_DEPLOY_LOCK_PATH" ) || return 1
  want=$(readlink -f "$PREVIEW_DEPLOY_LOCK_PATH")
  held=$(readlink -f "/proc/$$/fd/9" 2>/dev/null || true)
  if [[ -n $held && $held == "$want" ]]; then
    # Inherited from the lock holder (same open file description).
    flock -n 9 && return 0
  else
    exec 9>>"$PREVIEW_DEPLOY_LOCK_PATH" || return 1
    flock -n 9 && return 0
  fi
  printf 'PREVIEW_DEPLOY_LOCKED: another preview 8768 installer, switch or rollback is running; nothing was changed\n' >&2
  return 1
}
