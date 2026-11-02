#!/usr/bin/env bash
set -euo pipefail

APP=/opt/kalshi-signals/app
[[ "$(id -u)" == 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f "$APP/pyproject.toml" ]] || { echo "clone the repository to $APP first" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
cat > /usr/sbin/policy-rc.d <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT
apt-get update
apt-get install -y ca-certificates curl debian-keyring debian-archive-keyring apt-transport-https \
  python3 python3-venv git sqlite3 zstd rsync ufw caddy

if ! command -v tailscale >/dev/null; then
  install -d -m 0755 /usr/share/keyrings
  curl --fail --show-error --location \
    https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl --fail --show-error --location \
    https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
fi
rm -f /usr/sbin/policy-rc.d
trap - EXIT

getent group kalshi-signals >/dev/null || groupadd --system kalshi-signals
id kalshi-signals >/dev/null 2>&1 || useradd --system --gid kalshi-signals \
  --home-dir /var/lib/kalshi-signals --shell /usr/sbin/nologin kalshi-signals
install -d -o kalshi-signals -g kalshi-signals -m 0750 \
  /var/lib/kalshi-signals /var/lib/kalshi-signals/models/registry /var/log/kalshi-signals
install -d -o root -g kalshi-signals -m 0750 /etc/kalshi-signals
touch /etc/kalshi-signals/account.env
chown root:kalshi-signals /etc/kalshi-signals/account.env
chmod 0640 /etc/kalshi-signals/account.env

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/python3" -m pip install --disable-pip-version-check --upgrade pip
"$APP/.venv/bin/python3" -m pip install --disable-pip-version-check "$APP"

install -m 0644 "$APP/deploy/hetzner/kalshi-signals.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/hetzner/kalshi-signals-health.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/hetzner/kalshi-signals-health.timer" /etc/systemd/system/
install -m 0644 "$APP/deploy/hetzner/kalshi-signals-backup.service" /etc/systemd/system/
install -m 0644 "$APP/deploy/hetzner/kalshi-signals-backup.timer" /etc/systemd/system/
install -m 0644 "$APP/deploy/hetzner/Caddyfile" /etc/caddy/Caddyfile
install -d -m 0755 /etc/systemd/system/caddy.service.d
cat > /etc/systemd/system/caddy.service.d/kalshi-signals.conf <<'EOF'
[Service]
EnvironmentFile=-/etc/kalshi-signals/caddy.env
EOF

systemctl daemon-reload
systemctl enable tailscaled.service kalshi-signals-health.timer kalshi-signals-backup.timer
systemctl disable caddy.service >/dev/null 2>&1 || true

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow in on tailscale0 to any port 443 proto tcp
ufw --force enable

echo "Base installation complete. No dashboard or Caddy service was started."
echo "Join Tailscale, configure /etc/kalshi-signals/caddy.env, migrate data, and run connectivity_check.py."
