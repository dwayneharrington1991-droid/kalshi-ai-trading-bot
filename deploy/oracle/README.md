# Oracle production deployment

This directory prepares a reviewed, signals-only deployment. It does **not**
deploy or restart the current Oracle service by itself. The live service stays
at `/home/opc/kalshi-ai-trading-bot`, listens on loopback port 8765, and keeps
its SQLite journals and model artifacts outside versioned release directories.

## What the workflow guarantees

1. A pull request to `main` passes tests, and an approved commit reaches `main`.
2. GitHub Actions checks out that exact commit and runs the complete test suite.
3. The production job pauses at the protected GitHub Environment named
   `oracle-production`. The configured required reviewer must approve it.
4. SSH uses a dedicated private key and a pinned Oracle host key. Strict host
   verification is mandatory.
5. The server verifies the release checksum, creates a versioned virtual
   environment, and makes consistent SQLite backups through SQLite's backup API.
6. Only then does it atomically change the `current` release link and restart
   `kalshi-signals.service`.
7. The health check verifies the exact commit, live worker loops, required
   persistent files, and loopback-only port binding. A failed code-only release
   automatically restores the preceding healthy code release. Database-changing
   releases deliberately require a reviewed, manual rollback plan.

No workflow command places orders, modifies orders, transfers money, or edits
Kalshi credentials. Account credentials remain in the server-side service
environment and are never uploaded to GitHub.

## One-time first deployment setup

Do these steps in an ordinary SSH session before enabling automatic deploys.
They are intentionally manual because they inspect and preserve the existing
service.

1. In GitHub, open **Settings → Environments → oracle-production** and confirm
   `dwayneharrington1991-droid` is a required reviewer and only `main` may deploy.
2. Create a dedicated SSH key pair for deployments. Put only the public key in
   `/home/opc/.ssh/authorized_keys` on Oracle. Store the private key in the
   `oracle-production` environment secret `ORACLE_SSH_PRIVATE_KEY`.
3. Run `ssh-keyscan -t ed25519 129.146.120.195` from a trusted network and
   compare the fingerprint with `sudo ssh-keygen -lf
   /etc/ssh/ssh_host_ed25519_key.pub` on Oracle. After they match, store the
   complete `129.146.120.195 ssh-ed25519 ...` line as the environment secret
   `ORACLE_SSH_HOST_KEY`. Keep `ORACLE_HOST=129.146.120.195` and
   `ORACLE_USER=opc` as environment variables.
4. Copy `deployment.env.example` to
   `/etc/kalshi-signals/deployment.env`. Confirm `BACKUP_ROOT` is a separate
   mounted backup volume with enough free space for the journals plus 20%.
5. Copy `sudoers.example` to `/etc/sudoers.d/kalshi-deploy`, set mode 0440, and
   validate it with `sudo visudo -cf /etc/sudoers.d/kalshi-deploy`.
6. Run the workflow manually with **operation: preflight**. This is read-only:
   it reports the current commit, service definition, working tree state,
   persistent file sizes, owners, modes, and backup capacity. Save its output.
7. Compare the current `systemctl show` output with
   `kalshi-signals.override.conf.example`. Back up the existing unit and install
   the override only after confirming every journal/model path. Run
   `sudo systemctl daemon-reload`; do not restart yet.
8. Reconcile the preserved `monitor.py` diff and create a versioned release of
   the current working service as the first rollback baseline. Do not point the
   service at the repair release during this step. After the paths, backup
   volume, override, and baseline have been reviewed, create the explicit
   gate with `install -m 600 /dev/null
   /home/opc/kalshi-ai-trading-bot/.deploy/production-approved`.

The deploy script refuses to run without that last marker. The preflight never
creates it and never changes the service.

## Branch protection and routine deployment

Protect `main` so changes arrive through pull requests and require the CI check.
A merge to `main` runs the same complete test job but does not deploy. Production
requires a manual workflow run from `main`; that run tests the selected commit
again and then pauses for `oracle-production` approval. A pull-request or push
run never receives the production environment or SSH key.

For a deliberate redeploy, choose **Actions → Deploy Oracle production → Run
workflow**, select `deploy`, verify the displayed commit, and approve the
oracle-production environment. For a read-only inspection choose `preflight`.

## Persistent data and backups

These files remain under `/home/opc/kalshi-ai-trading-bot` and are never put in
a release archive:

- `data/signals_monitor.db`
- `data/signals_account_monitor.db`
- `data/models/`
- all service environment and credential files

Before every restart, `sqlite_backup.py` uses `Connection.backup()` so a live
WAL database is copied consistently. Each backup receives `PRAGMA quick_check`,
a SHA-256 manifest, and the release policy. The deployment stops when backup
space is less than all databases/models plus 25% and 1 GiB. Never copy a live `.db`
file with `cp`.

## Rollback

In GitHub Actions choose `rollback`, enter the full 40-character commit of a
known versioned release, then approve the production environment. Rollback
changes the code symlink and restarts the service; it never restores or deletes
the journals. It is permitted only when that release declares
`"database_change": "none"`. Any incompatible database release must include a
separately reviewed restore/migration plan and will be blocked automatically.

## Health and failure handling

The post-deploy check requires:

- systemd reports the service active;
- `/healthz` returns `HEALTHY` and the exact tested commit;
- collector and prediction workers have completed cycles;
- port 8765 is bound only to `127.0.0.1`;
- the persistent journal exists, plus model files when configured as required.

`market_stale` is reported but is not treated as proof of a bad deployment,
because an upstream exchange outage must not roll back otherwise healthy code.
The dashboard continues to label stale inputs and withhold signals.
