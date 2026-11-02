#!/usr/bin/env bash
set -euo pipefail

state=/run/kalshi-signals-health-failures
payload="$(curl --silent --show-error --max-time 5 http://127.0.0.1:8765/healthz 2>/dev/null || true)"
if python3 - "$payload" <<'PY'
import datetime as dt
import json
import sys

try:
    value = json.loads(sys.argv[1])
    status = value.get("status")
    if status == "HEALTHY":
        raise SystemExit(0)
    if status == "STARTING":
        started = dt.datetime.fromisoformat(value["started_at"].replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        raise SystemExit(0 if age <= 600 else 1)
except (ValueError, TypeError, KeyError, json.JSONDecodeError):
    pass
raise SystemExit(1)
PY
then
  rm -f "$state"
  exit 0
fi

failures=0
[[ -r "$state" ]] && read -r failures < "$state" || true
failures=$((failures + 1))
printf '%s\n' "$failures" > "$state"
logger -t kalshi-signals-health "health check failed ($failures/3); response contents withheld"
if (( failures >= 3 )); then
  rm -f "$state"
  systemctl try-restart kalshi-signals.service
fi
