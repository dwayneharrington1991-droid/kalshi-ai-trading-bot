#!/usr/bin/env bash
set -euo pipefail

echo "BLOCKED: the overnight canary is permanently disabled in this read-only build." >&2
exit 2
