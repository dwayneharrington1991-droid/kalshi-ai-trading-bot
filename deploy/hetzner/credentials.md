# Server-side read-only account configuration

Account monitoring is optional. Never put either value in Git, the dashboard,
shell history, or Caddy configuration.

1. Store only the key identifier in `/etc/kalshi-signals/account.env`:

   `KALSHI_MONITOR_KEY_ID=your-key-id`

2. Store the PEM in `/etc/kalshi-signals/kalshi-monitor.pem`.
3. Run:

   `sudo chown root:kalshi-signals /etc/kalshi-signals/account.env /etc/kalshi-signals/kalshi-monitor.pem`

   `sudo chmod 640 /etc/kalshi-signals/account.env /etc/kalshi-signals/kalshi-monitor.pem`

The service sets `KALSHI_MONITOR_PRIVATE_KEY_PATH` to that file. The
application verifies broker-reported read-only scope before it enables account
reads. If permission verification fails, account monitoring stays blocked.
Its fixed client permits authenticated GET requests only and has no order,
cancel, amendment, transfer, or position-management method.
