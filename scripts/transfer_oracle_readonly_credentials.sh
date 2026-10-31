#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

host=${ORACLE_HOST:-129.146.120.195}
user=${ORACLE_USER:-opc}
remote="$user@$host"
repo=$(cd "$(dirname "$0")/.." && pwd)
stage=/home/opc/.kalshi-credential-stage
[[ -n ${KALSHI_MONITOR_KEY_ID:-} && -n ${KALSHI_MONITOR_PRIVATE_KEY_PEM:-} ]] || {
  echo "TRANSFER_BLOCKED: Codespaces monitoring secrets are not present in this shell" >&2; exit 1;
}
[[ "$KALSHI_MONITOR_KEY_ID" =~ ^[A-Za-z0-9_-]{1,200}$ ]] || {
  echo "TRANSFER_BLOCKED: monitoring key identifier format is unsupported" >&2; exit 1;
}
local_stage=$(mktemp -d)
remote_created=0
ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes)
cleanup() {
  rm -f "$local_stage/key-id" "$local_stage/private-key.pem" \
    "$local_stage/install" "$local_stage/activate" "$local_stage/sudoers" \
    "$local_stage/oracle-ssh-key" "$local_stage/oracle-known-hosts"
  rmdir "$local_stage" 2>/dev/null || true
  if (( remote_created == 1 )); then
    ssh "${ssh_options[@]}" "$remote" \
      "rm -f '$stage/key-id' '$stage/private-key.pem' '$stage/install' '$stage/enable-readonly-account.sh' '$stage/sudoers'; rmdir '$stage' 2>/dev/null || true" \
      >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
if [[ -n ${ORACLE_SSH_PRIVATE_KEY:-} ]]; then
  printf '%s' "$ORACLE_SSH_PRIVATE_KEY" > "$local_stage/oracle-ssh-key"
  chmod 0600 "$local_stage/oracle-ssh-key"
  ssh_options+=(-i "$local_stage/oracle-ssh-key")
fi
if [[ -n ${ORACLE_SSH_HOST_KEY:-} ]]; then
  [[ "$ORACLE_SSH_HOST_KEY" == "$host "* ]] || {
    echo "TRANSFER_BLOCKED: ORACLE_SSH_HOST_KEY does not identify the configured host" >&2; exit 1;
  }
  printf '%s\n' "$ORACLE_SSH_HOST_KEY" > "$local_stage/oracle-known-hosts"
  chmod 0600 "$local_stage/oracle-known-hosts"
  ssh_options+=(-o "UserKnownHostsFile=$local_stage/oracle-known-hosts")
else
  ssh-keygen -F "$host" >/dev/null || {
    echo "TRANSFER_BLOCKED: verified ORACLE_SSH_HOST_KEY secret or existing known_hosts entry is required" >&2
    exit 1
  }
fi
printf '%s' "$KALSHI_MONITOR_KEY_ID" > "$local_stage/key-id"
printf '%s' "$KALSHI_MONITOR_PRIVATE_KEY_PEM" > "$local_stage/private-key.pem"
cp "$repo/deploy/oracle/install_readonly_credentials.sh" "$local_stage/install"
cp "$repo/deploy/oracle/enable_readonly_account.sh" "$local_stage/activate"
cp "$repo/deploy/oracle/sudoers.example" "$local_stage/sudoers"
chmod 0600 "$local_stage"/*

ssh "${ssh_options[@]}" "$remote" \
  "install -d -m 0700 '$stage' && test -z \"\$(find '$stage' -mindepth 1 -maxdepth 1 -print -quit)\""
remote_created=1
scp -q "${ssh_options[@]}" \
  "$local_stage/key-id" "$remote:$stage/key-id"
scp -q "${ssh_options[@]}" \
  "$local_stage/private-key.pem" "$remote:$stage/private-key.pem"
scp -q "${ssh_options[@]}" \
  "$local_stage/install" "$remote:$stage/install"
scp -q "${ssh_options[@]}" \
  "$local_stage/activate" "$remote:$stage/enable-readonly-account.sh"
scp -q "${ssh_options[@]}" \
  "$local_stage/sudoers" "$remote:$stage/sudoers"
ssh -tt "${ssh_options[@]}" "$remote" \
  "chmod 0600 '$stage/'* && bash '$stage/install' '$stage'"
remote_created=0
echo "transfer=COMPLETE credentials_printed=false trading_enabled=false"
