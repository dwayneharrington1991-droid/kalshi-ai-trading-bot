#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
mode=${1:---inspect}
[[ "$mode" == --inspect || "$mode" == --install ]] || {
  echo "usage: $0 [--inspect|--install]" >&2; exit 2;
}
app=/home/opc/kalshi-ai-trading-bot
service=kalshi-signals.service
candidate=$HOME/kalshi-deployment.env.candidate
unit=$(mktemp)
trap 'rm -f "$unit"' EXIT
sudo -n systemctl cat "$service" > "$unit"
working=$(sudo -n systemctl show "$service" --property=WorkingDirectory --value)
[[ -n "$working" ]] || working=$app
python312=$(command -v python3.12 || true)
[[ -n "$python312" ]] || { echo "Python 3.12 is required but unavailable" >&2; exit 1; }

python3 - "$unit" "$working" "$candidate" "$app" "$python312" <<'PY'
import shlex
import sys
from pathlib import Path

unit, working, candidate, app, python312 = map(Path, sys.argv[1:])
pending = ""
commands = []
for raw in unit.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if pending:
        line = pending + line
        pending = ""
    if line.endswith("\\"):
        pending = line[:-1] + " "
        continue
    if line.startswith("ExecStart=") and line.split("=", 1)[1].strip():
        commands.append(line.split("=", 1)[1].strip())
if not commands:
    raise SystemExit("effective ExecStart was not found")
argv = shlex.split(commands[-1])
if "-m" not in argv or "src.signals_display.server" not in argv:
    raise SystemExit("existing service is not the supported signals display server")
if "--mode" not in argv or argv[argv.index("--mode") + 1] != "public":
    raise SystemExit("existing service is not explicitly in public signals-only mode")

def value(flag):
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    raise SystemExit(f"existing service must explicitly configure {flag}; refusing to invent it")

base = working.resolve()
def absolute(flag):
    path = Path(value(flag))
    return (base / path).resolve() if not path.is_absolute() else path.resolve()

journal = absolute("--journal")
account = absolute("--account-store")
model = absolute("--model-artifact")
opening = absolute("--opening-model-artifact")
registry = absolute("--model-registry")
expected_registry = (app / "data/models/registry").resolve()
if registry != expected_registry:
    raise SystemExit(f"model registry mismatch: service={registry} required={expected_registry}")
data_root = (app / "data").resolve()
for name, path in (("journal", journal), ("account store", account)):
    if path.parent != data_root or not path.is_file():
        raise SystemExit(f"{name} is missing or outside {data_root}: {path}")
model_root = (app / "data/models").resolve()
if not model_root.is_dir():
    raise SystemExit(f"model root is missing: {model_root}")

lines = [
    f"APP_ROOT={app}",
    f"DEPLOY_ROOT={app / '.deploy'}",
    f"DATA_ROOT={data_root}",
    "BACKUP_ROOT=/home/opc/kalshi-backups",
    "ALLOW_SAME_DISK_BACKUP=1",
    "SERVICE_NAME=kalshi-signals.service",
    f"PYTHON_BIN={python312}",
    f"JOURNAL_PATH={journal}",
    f"ACCOUNT_STORE_PATH={account}",
    f"MODEL_ARTIFACT={model}",
    f"OPENING_MODEL_ARTIFACT={opening}",
    f"MODEL_REGISTRY={registry}",
    f"MODEL_ROOT={model_root}",
    "REQUIRE_MODEL_ARTIFACTS=0",
]
candidate.write_text("\n".join(lines) + "\n", encoding="utf-8")
candidate.chmod(0o600)
print(f"candidate={candidate}")
print(f"account_readonly_current={int('--account-readonly' in argv)}")
print("signals_only_verified=true")
PY

echo "Candidate generated from the effective service; no configuration installed yet."
cat "$candidate"
if [[ "$mode" == --install ]]; then
  deploy_user=$(id -un)
  deploy_group=$(id -gn)
  [[ "$deploy_user" == opc ]] || {
    echo "configuration installation must be run by opc" >&2; exit 1;
  }
  # root owns the configuration; only root and opc's primary group may read it.
  sudo -n install -d -o root -g "$deploy_group" -m 0750 /etc/kalshi-signals
  if sudo -n test -e /etc/kalshi-signals/deployment.env; then
    sudo -n cmp -s "$candidate" /etc/kalshi-signals/deployment.env || {
      echo "existing deployment.env differs; refusing overwrite" >&2; exit 1;
    }
    sudo -n chown root:"$deploy_group" /etc/kalshi-signals/deployment.env
    sudo -n chmod 0640 /etc/kalshi-signals/deployment.env
  else
    sudo -n install -o root -g "$deploy_group" -m 0640 \
      "$candidate" /etc/kalshi-signals/deployment.env
  fi
  [[ -r /etc/kalshi-signals/deployment.env ]] || {
    echo "installed configuration is not readable by opc" >&2; exit 1;
  }
  mkdir -p /home/opc/kalshi-backups
  echo "configuration_installed=true service_modified=false service_restarted=false"
  echo "WARNING: same-disk backups do not protect against disk failure."
fi
