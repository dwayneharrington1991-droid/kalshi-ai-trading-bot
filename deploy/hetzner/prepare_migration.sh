#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${1:?usage: prepare_migration.sh DESTINATION_DIRECTORY}"
mkdir -p "$DEST"
[[ -z "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit)" ]] || {
  echo "destination must be empty" >&2; exit 1;
}

for name in signals_monitor.db signals_account_monitor.db; do
  source="$ROOT/$name"
  [[ -f "$source" ]] || continue
  sqlite3 "$source" ".timeout 30000" ".backup '$DEST/$name'"
  [[ "$(sqlite3 "$DEST/$name" 'PRAGMA integrity_check;')" == ok ]] || {
    echo "$name snapshot failed integrity check" >&2; exit 1;
  }
done

for item in signals_model.json signals_opening_model.json signals_model_registry; do
  [[ -e "$ROOT/$item" ]] && cp -a "$ROOT/$item" "$DEST/"
done
(cd "$DEST" && find . -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs -r sha256sum > SHA256SUMS)
echo "Migration snapshot prepared. Source files were not modified or deleted."
