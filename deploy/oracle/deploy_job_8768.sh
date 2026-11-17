#!/usr/bin/env bash
# Server-owned preview-8768 deployment jobs (Phase 5).
#
#   deploy_job_8768.sh start   <40-char commit> <request id>
#   deploy_job_8768.sh status  <request id>
#   deploy_job_8768.sh recover <request id>
#   deploy_job_8768.sh rollback <request id>
#
# `start` launches deploy_preview_8768.sh DETACHED (own session via setsid,
# nohup, SIGHUP ignored, stdin from /dev/null, no inherited lock fd), so an
# SSH disconnect, closed terminal or interrupted PowerShell cannot stop it.
# The request id IS the job id: repeating `start` with the same id never
# launches a second installation, it reports the existing job. A `start`
# while another job is running reports BUSY and launches nothing.
# Status, result and log live in a private job directory (0700, files 0600);
# the orchestrator prints no environment or credentials, so nothing secret is
# written there. `recover` re-checks an interrupted job under the exclusive
# lock (also detached) and restores the previous build only if needed.
set -Euo pipefail
umask 077
trap '' HUP
readonly STATE_ROOT=${PREVIEW_STATE_ROOT:-/home/opc/.kalshi-deploy/preview-8768}
readonly JOBS=$STATE_ROOT/jobs
readonly SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly ORCHESTRATOR=$SCRIPT_DIR/deploy_preview_8768.sh

say() { printf 'DEPLOY_JOB: %s\n' "$*"; }
valid_id() { [[ $1 =~ ^[A-Za-z0-9-]{8,64}$ ]]; }

job_pid_alive() {
  local job=$1 pid
  [[ -f $job/pid ]] || return 1
  pid=$(<"$job/pid")
  [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null &&
    tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q 'deploy_preview_8768.sh'
}

active_job() {
  local job
  for job in "$JOBS"/*/; do
    [[ -d $job ]] || continue
    job=${job%/}
    [[ -f $job/result ]] && continue
    if job_pid_alive "$job"; then basename "$job"; return 0; fi
  done
  return 1
}

emit_status() {
  local id=$1 job=$JOBS/$1 state
  if [[ -f $job/result ]]; then state=FINISHED
  elif job_pid_alive "$job"; then state=RUNNING
  elif [[ -f $job/pid ]]; then state=DIED
  else state=STARTING
  fi
  printf 'DEPLOY_JOB_ID=%s\nDEPLOY_JOB_STATE=%s\n' "$id" "$state"
  [[ -f $job/sha ]] && printf 'DEPLOY_JOB_SHA=%s\n' "$(<"$job/sha")"
  [[ -f $job/run_id ]] && printf 'DEPLOY_RUN_ID=%s\n' "$(<"$job/run_id")"
  [[ -f $job/result ]] && grep -E '^(DEPLOY_RESULT|EXIT|FINISHED_AT)=' "$job/result"
  # Recent progress (the orchestrator never prints environment or secrets).
  [[ -f $job/log ]] && tail -n 20 "$job/log" | sed 's/^/LOG: /'
  return 0
}

launch() {
  local job=$1; shift
  rm -f "$job/pid" "$job/result"
  # Own session, no terminal, no SIGHUP, no inherited lock fd.
  setsid nohup env PREVIEW_JOB_DIR="$job" bash "$ORCHESTRATOR" "$@" \
    >>"$job/log" 2>&1 </dev/null 9>&- &
  for _ in $(seq 50); do [[ -f $job/pid ]] && break; sleep 0.1; done
}

cmd=${1:-}
case $cmd in
  start)
    sha=${2:-}; id=${3:-}
    [[ $sha =~ ^[0-9a-f]{40}$ ]] || { say "an exact 40-character commit is required"; printf 'DEPLOY_JOB_STATE=REJECTED\n'; exit 2; }
    valid_id "$id" || { say "invalid request id"; printf 'DEPLOY_JOB_STATE=REJECTED\n'; exit 2; }
    install -d -m 0700 "$STATE_ROOT" "$JOBS"
    if [[ -d $JOBS/$id ]]; then
      say "request $id already exists; reporting it (no second installation)"
      emit_status "$id"; exit 0
    fi
    if other=$(active_job); then
      say "another deployment job is running: $other; nothing was started"
      printf 'DEPLOY_JOB_STATE=BUSY\nDEPLOY_ACTIVE_JOB=%s\n' "$other"; exit 0
    fi
    install -d -m 0700 "$JOBS/$id"
    printf '%s\n' "$sha" >"$JOBS/$id/sha"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$JOBS/$id/created_at"
    launch "$JOBS/$id" deploy "$sha"
    emit_status "$id" ;;
  status)
    id=${2:-}
    valid_id "$id" && [[ -d $JOBS/$id ]] || { say "unknown job"; printf 'DEPLOY_JOB_STATE=UNKNOWN\n'; exit 2; }
    emit_status "$id" ;;
  recover)
    id=${2:-}; job=$JOBS/$id
    valid_id "$id" && [[ -d $job ]] || { say "unknown job"; printf 'DEPLOY_JOB_STATE=UNKNOWN\n'; exit 2; }
    if [[ -f $job/result ]] || job_pid_alive "$job"; then
      say "job is not interrupted; nothing to recover"; emit_status "$id"; exit 0
    fi
    if [[ ! -f $job/run_id ]]; then
      # Interrupted before the snapshot: the installer had not started.
      printf 'DEPLOY_RESULT=INTERRUPTED_BEFORE_CHANGE\nEXIT=2\nFINISHED_AT=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$job/result"
      emit_status "$id"; exit 0
    fi
    say "recovering interrupted job $id under the exclusive lock (detached)"
    launch "$job" recover "$(<"$job/sha")" "$(<"$job/run_id")"
    emit_status "$id" ;;
  rollback)
    # Client-requested rollback of a SUCCESSFUL job (e.g. its own tunnel saw a
    # wrong build). Detached like every job; the orchestrator re-verifies the
    # process identity and holds the exclusive lock.
    id=${2:-}; job=$JOBS/$id
    valid_id "$id" && [[ -d $job ]] || { say "unknown job"; printf 'DEPLOY_JOB_STATE=UNKNOWN\n'; exit 2; }
    if job_pid_alive "$job"; then say "job is still running; rollback not started"; emit_status "$id"; exit 0; fi
    if ! grep -qx 'DEPLOY_RESULT=SUCCESS' "$job/result" 2>/dev/null || [[ ! -f $job/run_id ]]; then
      say "only a successful job can be rolled back by request; nothing started"; emit_status "$id"; exit 0
    fi
    launch "$job" rollback "$(<"$job/sha")" "$(<"$job/run_id")"
    emit_status "$id" ;;
  *) echo "usage: $0 start <commit> <request id> | status <request id> | recover <request id> | rollback <request id>" >&2; exit 2 ;;
esac
