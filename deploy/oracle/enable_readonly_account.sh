#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

config=${KALSHI_DEPLOY_CONFIG:-/etc/kalshi-signals/deployment.env}
[[ $(id -un) == opc ]] || { echo "ACCOUNT_ACTIVATION_BLOCKED: run as opc" >&2; exit 1; }
[[ -r "$config" ]] || { echo "ACCOUNT_ACTIVATION_BLOCKED: unreadable deployment config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${DEPLOY_ROOT:?}" "${SERVICE_NAME:?}" "${PYTHON_BIN:?}"
flags="$DEPLOY_ROOT/account-monitoring.flags"
[[ -r "$flags" ]] || { echo "ACCOUNT_ACTIVATION_BLOCKED: account mode file missing" >&2; exit 1; }

exec 9>"$DEPLOY_ROOT/account-activation.lock"
flock -n 9 || { echo "ACCOUNT_ACTIVATION_BLOCKED: another account activation is running" >&2; exit 1; }
previous=$(cat "$flags")
[[ "$previous" == "ENABLE_ACCOUNT_READONLY=0" || "$previous" == "ENABLE_ACCOUNT_READONLY=1" ]] || {
  echo "ACCOUNT_ACTIVATION_BLOCKED: invalid preserved account mode" >&2; exit 1;
}
restore() {
  code=$?
  trap - ERR
  printf '%s\n' "$previous" > "$flags.next"
  chmod 0600 "$flags.next"
  mv -f "$flags.next" "$flags"
  sudo -n systemctl restart "$SERVICE_NAME" || true
  echo "ACCOUNT_ACTIVATION_BLOCKED: read-only verification failed; prior mode restored" >&2
  exit "$code"
}
trap restore ERR
printf 'ENABLE_ACCOUNT_READONLY=1\n' > "$flags.next"
chmod 0600 "$flags.next"
mv -f "$flags.next" "$flags"
sudo -n systemctl restart "$SERVICE_NAME"

"$PYTHON_BIN" - <<'PY'
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

deadline = time.monotonic() + 120
last = "dashboard has not responded"
opener = build_opener(ProxyHandler({}))
while time.monotonic() < deadline:
    try:
        with opener.open("http://127.0.0.1:8765/api/state?view=main", timeout=5) as response:
            state = json.load(response)
        account = state.get("account") or {}
        permissions = account.get("permissions") or {}
        status = account.get("status")
        if permissions.get("read_only_verified") is True and status == "READ_ONLY_CONNECTED":
            print(json.dumps({
                "account_monitoring": "READ_ONLY_CONNECTED",
                "authentication": permissions.get("authentication"),
                "scope": permissions.get("status"),
                "account_scope": permissions.get("account_scope"),
                "balance_present": account.get("available_balance_dollars") is not None,
                "open_kxbtc15m_positions": sum(
                    h.get("status") == "OPEN" and str(h.get("ticker", "")).startswith("KXBTC15M-")
                    for h in account.get("holdings", [])
                ),
                "signals_only": True,
            }, sort_keys=True))
            raise SystemExit(0)
        last = str(status) + ": " + str(account.get("reason") or permissions.get("blocker"))
        if status in {"ACCOUNT_BLOCKED", "ACCOUNT_NOT_CONFIGURED"}:
            break
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        last = type(exc).__name__
    time.sleep(2)
raise SystemExit("read-only account verification did not succeed: " + last)
PY

trap - ERR
echo "account_activation=VERIFIED_READ_ONLY order_execution=DISABLED"
