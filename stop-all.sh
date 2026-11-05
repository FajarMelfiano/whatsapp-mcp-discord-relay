#!/usr/bin/env bash
# Stop the watchdog and all relay processes started by this repository.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [ -f keepalive.pid ]; then
  pid="$(cat keepalive.pid 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f keepalive.pid
fi

pkill -f "$DIR/discord-bot/bot.py" 2>/dev/null || true
pkill -f "[n]otifier.py" 2>/dev/null || true
pkill -x whatsapp-bridge 2>/dev/null || true

echo "stopped bridge, notifier, Discord relay, and keepalive (if running)"
