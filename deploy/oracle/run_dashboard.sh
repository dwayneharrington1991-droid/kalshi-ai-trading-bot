#!/usr/bin/env bash
set -euo pipefail

root=/home/opc/kalshi-ai-trading-bot
release="$root/.deploy/current"
commit=$(cat "$release/.deployed-commit")
exec "$release/.venv/bin/python3" -m src.signals_display.server \
  --mode public --host 127.0.0.1 --port 8765 \
  --account-readonly --private-dashboard --require-readonly-scope \
  --journal "$root/signals_monitor.db" \
  --account-store "$root/signals_account_monitor.db" \
  --model-artifact "$root/signals_model.json" \
  --opening-model-artifact "$root/signals_opening_model.json" \
  --model-registry "$root/signals_model_registry" \
  --processing-ms 100 --build-commit "$commit"
