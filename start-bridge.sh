#!/usr/bin/env bash
# Build (if needed) and start the WhatsApp bridge in the background.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/whatsapp-bridge"
mkdir -p store
if [ ! -x ./whatsapp-bridge ]; then
  echo "building bridge..."
  go build -o whatsapp-bridge .
fi
pkill -x whatsapp-bridge 2>/dev/null || true
sleep 1
nohup ./whatsapp-bridge >> store/bridge.log 2>&1 &
echo "bridge pid: $!"
echo "log: $(pwd)/store/bridge.log"
