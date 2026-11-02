#!/usr/bin/env bash
set -euo pipefail
umask 027
commit=${1:?40-character commit required}
config=${2:-/etc/kalshi-signals/deployment.env}
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid commit" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
release="$DEPLOY_ROOT/releases/$commit"
[[ -d "$release" && -f "$release/.deployed-commit" ]] || {
  echo "release is unavailable" >&2; exit 1;
}
policy=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["database_change"])' \
  "$release/deploy/oracle/release-policy.json")
[[ "$policy" == none ]] || {
  echo "rollback withheld: release database policy is not 'none'" >&2; exit 1;
}
ln -s "$release" "$DEPLOY_ROOT/current.rollback"
mv -Tf "$DEPLOY_ROOT/current.rollback" "$DEPLOY_ROOT/current"
sudo -n systemctl restart "$SERVICE_NAME"
"$release/.venv/bin/python3" "$release/deploy/oracle/health_check.py" \
  --commit "$commit" --data-root "$DATA_ROOT" \
  --require-models "${REQUIRE_MODEL_ARTIFACTS:-0}"
printf '%s\n' "$commit" > "$DEPLOY_ROOT/deployed-commit"
echo "rollback=HEALTHY commit=$commit"
