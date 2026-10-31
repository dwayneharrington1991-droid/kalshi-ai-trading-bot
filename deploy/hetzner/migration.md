# Non-destructive journal migration

The migration makes a transactionally consistent SQLite backup and never
removes the Codespaces database.

In Codespaces, from the repository:

```bash
mkdir -p /tmp/kalshi-migration
deploy/hetzner/prepare_migration.sh /tmp/kalshi-migration
```

Copy the resulting directory to the server with `rsync -avP`. Verify the
manifest on both machines:

```bash
cd /tmp/kalshi-migration && sha256sum -c SHA256SUMS
```

On the VPS, stop the signals service and import the bundle:

```bash
sudo systemctl stop kalshi-signals.service
sudo /opt/kalshi-signals/app/deploy/hetzner/import_migration.sh /path/to/kalshi-migration
sudo systemctl start kalshi-signals.service
```

The importer refuses invalid checksums or SQLite integrity failures. If the
VPS already contains a journal, it first makes a timestamped safety copy under
`/srv/kalshi-backups/pre-import`. It does not alter the source bundle.
