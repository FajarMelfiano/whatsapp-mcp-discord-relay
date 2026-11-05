#!/usr/bin/env bash
# Start the optional WhatsApp DM notifier in the background.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
if pgrep -f "[n]otifier.py" >/dev/null; then
  echo "notifier already running"
  exit 0
fi
nohup python3 "$DIR/notifier.py" >> notifier.out 2>&1 &
echo "notifier pid: $!"
