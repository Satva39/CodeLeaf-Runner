#!/usr/bin/env bash
set -Eeuo pipefail
PISTON_URL="${PISTON_ADMIN_URL:-http://127.0.0.1:2000}"
curl --fail --silent --show-error "${PISTON_URL}/api/v2/runtimes" | python -m json.tool
