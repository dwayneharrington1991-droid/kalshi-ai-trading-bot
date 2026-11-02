# 24/7 private signals server

This deployment runs only `src.signals_display.server`. It does not import or
start the repository's order execution programs. The application listens on
`127.0.0.1:8765`; Caddy exposes it only on the VPS Tailscale address.

## Recommended server

- Hetzner Cloud US, Ubuntu 24.04 LTS
- 4 shared vCPU, 8 GB RAM, 160 GB or larger local disk
- a separately mounted 100 GB or larger backup volume at
  `/srv/kalshi-backups`
- Tailscale account with HTTPS certificates enabled

Allow room for the existing 8 GB journal, SQLite WAL files, model artifacts,
updates, and growth. The repository currently records second-level evidence,
so check disk growth after the first week. Increase the data disk before it
reaches 70% utilization.

Provider prices change. At the time this configuration was prepared, budget
approximately USD $20–35 per month for the VPS, backup storage, IPv4 if used,
and provider backups. Confirm the displayed checkout price before purchase.

## Installation outline

1. Create the VPS only after reviewing the provider's current price. Add your
   SSH public key. Do not paste the Kalshi private key into cloud-init.
2. Attach and mount the separate backup volume at `/srv/kalshi-backups`.
3. Clone this branch to `/opt/kalshi-signals/app`.
4. As root, run `deploy/hetzner/install.sh`.
5. Join the VPS to your Tailscale network and enable HTTPS certificates for
   its `*.ts.net` name in the Tailscale admin console.
   Run `sudo tailscale set --operator=caddy` so Caddy can request that
   certificate; root retains administrative access.
6. Put the Tailscale IPv4 address and DNS name in
   `/etc/kalshi-signals/caddy.env`, then run
   `sudo systemctl enable --now caddy.service`.
7. Optionally configure the read-only account key as described in
   `credentials.md`. Public market monitoring works without account secrets.
8. Copy a verified migration bundle using `migration.md`. The source journal
   remains in Codespaces.
9. Run `sudo /opt/kalshi-signals/app/deploy/hetzner/connectivity_check.py`.
10. Start the service only after the diagnostic passes:
    `sudo systemctl enable --now kalshi-signals.service`.

The actual VPS network has not been tested by repository tests. A successful
offline test does not establish that Kalshi or exchange feeds accept its IP.
