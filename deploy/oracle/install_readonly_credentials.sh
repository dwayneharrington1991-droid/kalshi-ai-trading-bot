#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

stage=${1:-/home/opc/.kalshi-credential-stage}
[[ $(id -un) == opc ]] || { echo "CREDENTIAL_INSTALL_BLOCKED: run as opc" >&2; exit 1; }
[[ "$stage" == /home/opc/.kalshi-credential-stage ]] || {
  echo "CREDENTIAL_INSTALL_BLOCKED: unexpected staging path" >&2; exit 1;
}
key_id_file="$stage/key-id"
private_key_file="$stage/private-key.pem"
activation="$stage/enable-readonly-account.sh"
sudoers="$stage/sudoers"
cleanup() { rm -f "$key_id_file" "$private_key_file" "$activation" "$sudoers"; rmdir "$stage" 2>/dev/null || true; }
trap cleanup EXIT
for path in "$key_id_file" "$private_key_file" "$activation"; do
  [[ -f "$path" && $(stat -c %U "$path") == opc && $(stat -c %a "$path") == 600 ]] || {
    echo "CREDENTIAL_INSTALL_BLOCKED: staged files must be opc-owned mode 0600" >&2; exit 1;
  }
done
[[ -f "$sudoers" && $(stat -c %U "$sudoers") == opc && $(stat -c %a "$sudoers") == 600 ]] || {
  echo "CREDENTIAL_INSTALL_BLOCKED: staged sudoers policy must be opc-owned mode 0600" >&2; exit 1;
}
key_id=$(cat "$key_id_file")
[[ "$key_id" =~ ^[A-Za-z0-9_-]{1,200}$ ]] || {
  echo "CREDENTIAL_INSTALL_BLOCKED: key identifier format is unsupported" >&2; exit 1;
}

# Parse the private key without printing it or retaining it in a process argument.
/home/opc/kalshi-ai-trading-bot/.deploy/current/.venv/bin/python3 - "$private_key_file" <<'PY'
import sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
key = serialization.load_pem_private_key(Path(sys.argv[1]).read_bytes(), password=None)
if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
    raise SystemExit("unsupported monitoring key type")
if isinstance(key, RSAPrivateKey) and key.key_size < 2048:
    raise SystemExit("monitoring RSA key must be at least 2048 bits")
print("private_key_format=VERIFIED")
PY

candidate="$stage/account-monitor.env"
printf 'KALSHI_MONITOR_KEY_ID=%s\nKALSHI_MONITOR_PRIVATE_KEY_PATH=/etc/kalshi-signals/monitor-private-key.pem\n' \
  "$key_id" > "$candidate"
chmod 0600 "$candidate"
dropin="$stage/20-readonly-account.conf"
cat > "$dropin" <<'EOF'
[Service]
EnvironmentFile=/etc/kalshi-signals/account-monitor.env
EOF
chmod 0600 "$dropin"

sudo visudo -cf "$sudoers" >/dev/null
sudo install -o root -g root -m 0440 "$sudoers" /etc/sudoers.d/kalshi-deploy
sudo -n install -d -o root -g opc -m 0750 /etc/kalshi-signals
sudo -n install -o root -g opc -m 0640 "$private_key_file" \
  /etc/kalshi-signals/monitor-private-key.pem
sudo -n install -o root -g opc -m 0640 "$candidate" \
  /etc/kalshi-signals/account-monitor.env
sudo -n install -d -o root -g root -m 0755 \
  /etc/systemd/system/kalshi-signals.service.d
sudo -n install -o root -g root -m 0644 "$dropin" \
  /etc/systemd/system/kalshi-signals.service.d/20-readonly-account.conf
sudo -n systemctl daemon-reload

# The activator performs authenticated GET-only scope verification, enables
# monitoring only after success, and restores the previous mode on failure.
"$activation"
echo "credential_install=VERIFIED_ROOT_OWNED account_monitoring=READ_ONLY"
