#!/usr/bin/env bash
set -euo pipefail

APP=/opt/kalshi-signals/app
DATA=/var/lib/kalshi-signals
PYTHON="$APP/.venv/bin/python3"

test -x "$PYTHON" || { echo "production virtual environment is missing" >&2; exit 1; }
mkdir -p "$DATA/models/registry"
commit="$(git -C "$APP" rev-parse HEAD)"

args=(
  -m src.signals_display.server
  --mode public
  --host 127.0.0.1
  --port 8765
  --journal "$DATA/signals_monitor.db"
  --account-store "$DATA/signals_account_monitor.db"
  --model-artifact "$DATA/models/signals_model.json"
  --opening-model-artifact "$DATA/models/signals_opening_model.json"
  --model-registry "$DATA/models/registry"
  --processing-ms 100
  --build-commit "$commit"
)

if [[ -n "${KALSHI_MONITOR_KEY_ID:-}" && -r "${KALSHI_MONITOR_PRIVATE_KEY_PATH:-/nonexistent}" ]]; then
  args+=(--account-readonly --private-dashboard --require-readonly-scope)
else
  echo "Starting in public-data mode; read-only account credentials are not configured." >&2
fi

exec "$PYTHON" "${args[@]}"
