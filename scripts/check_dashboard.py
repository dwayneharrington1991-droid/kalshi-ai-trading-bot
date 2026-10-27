#!/usr/bin/env python3
"""Secret-safe diagnostics. Optional authenticated GET-only production check."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-check",
        action="store_true",
        help="Load existing monitor secrets server-side and verify authenticated GET/permissions",
    )
    args = parser.parse_args()
    result = {
        "credentials": {
            name: bool(os.environ.get(name))
            for name in (
                "KALSHI_MONITOR_KEY_ID",
                "KALSHI_MONITOR_PRIVATE_KEY_PEM",
                "KALSHI_MONITOR_PRIVATE_KEY_PATH",
            )
        }
    }
    try:
        with urlopen("http://127.0.0.1:8765/api/state", timeout=10) as response:
            state = json.load(response)
        result["dashboard"] = state.get(
            "connection_diagnostics",
            {
                "account_status": state.get("account", {}).get("status"),
                "note": "Old dashboard version; update and restart",
            },
        )
    except Exception as exc:
        result["dashboard"] = {"status": "UNREACHABLE", "error_type": type(exc).__name__}
    if args.account_check:
        from src.signals_display.account_readonly import ReadOnlyKalshi, AccountReadError

        try:
            client = ReadOnlyKalshi.from_environment()
            permissions = client.permissions()
            result["production_permission_check"] = permissions
            if permissions["read_only_verified"]:
                client.require_readonly = True
                data = client.snapshot()
                result["production_account_check"] = {
                    "status": "AUTHENTICATED_GET_SUCCEEDED",
                    "positions": len(data["positions"]),
                    "fills": len(data["fills"]),
                    "orders": len(data["orders"]),
                    "balance_present": bool(data["balance"]),
                    "observed_at": data["observed_at"],
                }
            else:
                result["production_account_check"] = {
                    "status": "WITHHELD",
                    "reason": permissions.get("blocker"),
                }
        except AccountReadError as exc:
            result["production_account_check"] = {"status": "WITHHELD", "reason": str(exc)}
        except Exception as exc:
            result["production_account_check"] = {
                "status": "WITHHELD",
                "error_type": type(exc).__name__,
            }
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
