#!/usr/bin/env bash
# Link WhatsApp with a phone number (no QR needed).
# Usage: ./pair.sh 6281234567890
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/whatsapp-bridge"
if [ -z "${1:-}" ]; then
  echo "usage: $0 <phone with country code, digits only>"
  exit 1
fi
mkdir -p store
if [ ! -x ./whatsapp-bridge ]; then
  go build -o whatsapp-bridge .
fi
pkill -x whatsapp-bridge 2>/dev/null || true
sleep 1
rm -f /tmp/whatsapp_pair_code.txt
WHATSAPP_PAIR_PHONE="$1" nohup ./whatsapp-bridge > store/bridge.log 2>&1 &
echo "waiting for pairing code..."
for _ in $(seq 1 40); do
  if [ -s /tmp/whatsapp_pair_code.txt ]; then
    echo "PAIRING CODE: $(cat /tmp/whatsapp_pair_code.txt)"
    exit 0
  fi
  sleep 1
done
echo "no code produced; check store/bridge.log"
exit 1
