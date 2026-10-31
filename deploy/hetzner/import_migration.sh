#!/usr/bin/env bash
set -euo pipefail
umask 077

BUNDLE="${1:?usage: import_migration.sh MIGRATION_DIRECTORY}"
DATA=/var/lib/kalshi-signals
BACKUP=/srv/kalshi-backups/pre-import/"$(date -u +%Y%m%dT%H%M%SZ)"
[[ "$(id -u)" == 0 ]] || { echo "run as root" >&2; exit 1; }
systemctl is-active --quiet kalshi-signals.service && {
  echo "stop kalshi-signals.service before importing" >&2; exit 1;
}
(cd "$BUNDLE" && sha256sum -c SHA256SUMS)
for name in signals_monitor.db signals_account_monitor.db; do
  [[ -f "$BUNDLE/$name" ]] || continue
  [[ "$(sqlite3 "$BUNDLE/$name" 'PRAGMA integrity_check;')" == ok ]] || exit 1
done

mkdir -p "$DATA" "$BACKUP"
for name in signals_monitor.db signals_account_monitor.db; do
  [[ -f "$DATA/$name" ]] && cp --reflink=auto --preserve=all "$DATA/$name" "$BACKUP/"
  [[ -f "$BUNDLE/$name" ]] && install -o kalshi-signals -g kalshi-signals -m 0640 "$BUNDLE/$name" "$DATA/$name.new"
  [[ -f "$DATA/$name.new" ]] && mv "$DATA/$name.new" "$DATA/$name"
done
mkdir -p "$DATA/models/registry"
[[ -f "$BUNDLE/signals_model.json" ]] && install -o kalshi-signals -g kalshi-signals -m 0640 "$BUNDLE/signals_model.json" "$DATA/models/signals_model.json"
[[ -f "$BUNDLE/signals_opening_model.json" ]] && install -o kalshi-signals -g kalshi-signals -m 0640 "$BUNDLE/signals_opening_model.json" "$DATA/models/signals_opening_model.json"
[[ -d "$BUNDLE/signals_model_registry" ]] && cp -a "$BUNDLE/signals_model_registry/." "$DATA/models/registry/"
chown -R kalshi-signals:kalshi-signals "$DATA"
echo "Verified migration imported; source bundle and pre-import safety copies preserved."
