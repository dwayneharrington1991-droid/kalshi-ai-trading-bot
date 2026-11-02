#!/usr/bin/env bash
set -euo pipefail

APP=/opt/kalshi-signals/app
BRANCH=codex/kxbtc15m-signals-display
[[ "$(id -u)" == 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -z "$(git -C "$APP" status --porcelain)" ]] || {
  echo "update withheld: repository has local changes" >&2; exit 1;
}
git -C "$APP" fetch origin "$BRANCH"
git -C "$APP" merge --ff-only "origin/$BRANCH"
"$APP/.venv/bin/python3" -m pip install --disable-pip-version-check "$APP"
systemctl daemon-reload
systemctl restart kalshi-signals.service
sleep 3
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8765/health >/dev/null
git -C "$APP" rev-parse HEAD
