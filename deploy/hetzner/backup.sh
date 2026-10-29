#!/usr/bin/env bash
set -euo pipefail
umask 077

DATA=/var/lib/kalshi-signals
ROOT=/srv/kalshi-backups
mountpoint -q "$ROOT" || {
  echo "$ROOT is not a separate mounted backup destination" >&2
  exit 1
}
day="$(date -u +%Y%m%dT%H%M%SZ)"
target="$ROOT/daily/$day"
tmp="$ROOT/.incomplete-$day"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp" "$ROOT/daily" "$ROOT/weekly"

for name in signals_monitor.db signals_account_monitor.db; do
  source="$DATA/$name"
  [[ -f "$source" ]] || continue
  sqlite3 "$source" ".timeout 30000" ".backup '$tmp/$name'"
  result="$(sqlite3 "$tmp/$name" 'PRAGMA integrity_check;')"
  [[ "$result" == ok ]] || { echo "$name integrity check failed" >&2; exit 1; }
  zstd -q -T1 -3 --rm "$tmp/$name"
done

if [[ -d "$DATA/models" ]]; then
  tar --create --zstd --file "$tmp/models.tar.zst" --directory "$DATA" models
fi
(cd "$tmp" && sha256sum ./* > SHA256SUMS)
mv "$tmp" "$target"
trap - EXIT

# Keep seven daily generations. Sunday backups also remain for 35 days.
find "$ROOT/daily" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf -- {} +
if [[ "$(date -u +%u)" == 7 ]]; then
  cp -al "$target" "$ROOT/weekly/$day"
fi
find "$ROOT/weekly" -mindepth 1 -maxdepth 1 -type d -mtime +35 -exec rm -rf -- {} +
