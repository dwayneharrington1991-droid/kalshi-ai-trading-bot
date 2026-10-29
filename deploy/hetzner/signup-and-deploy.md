# Simple signup and deployment checklist

Nothing in the repository purchases a server. Prices below are estimates;
Hetzner's checkout screen is authoritative.

## Plan to select

1. Go to Hetzner Cloud and create a project named `kalshi-signals`.
2. Select a US location close to you.
3. Select Ubuntu 24.04.
4. Select **CPX32: 4 vCPU, 8 GB RAM, 160 GB NVMe**, or the current equivalent
   with at least those resources.
5. Add your SSH public key. Disable password login after confirming SSH works.
6. Do not enter Kalshi credentials in Hetzner's web form or cloud-init.
7. Add a separate Hetzner Storage Box (at least 100 GB) for backups, or an
   equivalent independently authenticated backup destination.
8. Review the checkout total before clicking Create.

Expected monthly budget before tax:

| Item | Estimated monthly cost |
|---|---:|
| 4 vCPU / 8 GB / 160 GB cloud server | $15–22 |
| Provider server backup option | $3–5 |
| Separate backup storage | $4–7 |
| Optional public IPv4 | $0.50–2 |
| **Expected total** | **$23–36** |

The official pricing page was unreachable from the development environment,
so these are planning estimates rather than a verified quote. Stop if the
checkout total is materially higher and review the selected resources.

## After the server exists

1. SSH into the VPS.
2. Install Git and clone branch `codex/kxbtc15m-signals-display` into
   `/opt/kalshi-signals/app`.
3. Run `sudo /opt/kalshi-signals/app/deploy/hetzner/install.sh`.
4. Run `sudo tailscale up` and follow its browser link.
5. In Tailscale, enable HTTPS certificates for the tailnet.
6. Run `sudo tailscale set --operator=caddy`.
7. Copy `caddy.env.example` to `/etc/kalshi-signals/caddy.env`, replacing the
   example address and name with `tailscale ip -4` and `tailscale status` data.
8. Run `sudo systemctl enable --now caddy.service`.
9. Configure the backup mount at `/srv/kalshi-backups`; verify that
   `mountpoint /srv/kalshi-backups` succeeds.
10. Follow `migration.md` to copy a verified snapshot. The Codespaces copy is
    retained.
11. Run `sudo -u kalshi-signals /opt/kalshi-signals/app/.venv/bin/python3
    /opt/kalshi-signals/app/deploy/hetzner/connectivity_check.py`.
12. Only if required public connections pass, run
    `sudo systemctl enable --now kalshi-signals.service`.
13. Open `https://your-server.your-tailnet.ts.net` on a device signed into the
    same Tailscale network.

Do not expose port 8765 or 443 on the public interface. Port 8765 remains on
loopback and the firewall permits HTTPS only through `tailscale0`.
