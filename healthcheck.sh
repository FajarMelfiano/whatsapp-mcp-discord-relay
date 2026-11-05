#!/usr/bin/env bash
# Exit 0 only when the bridge reports connected=true.
set -euo pipefail
cd "$(dirname "$0")"
url="${BRIDGE_HEALTH_URL:-http://127.0.0.1:8080/api/health}"
body="$(curl -fsS --max-time 5 "$url")"
python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") and d.get("connected") else 1)' <<<"$body"
