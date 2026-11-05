#!/usr/bin/env bash
# Start the Discord relay bot. Install discord-bot/requirements.txt first.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/discord-bot/.venv/bin/python"
BOT="$DIR/discord-bot/bot.py"
if [ ! -x "$PY" ]; then
  echo "missing $PY"
  echo "create it with: python3 -m venv discord-bot/.venv && discord-bot/.venv/bin/pip install -r discord-bot/requirements.txt"
  exit 1
fi
if pgrep -f "$BOT" >/dev/null; then
  echo "discord bot already running"
  exit 0
fi
touch "$DIR/discord-bot.enabled"
nohup "$PY" "$BOT" >> "$DIR/discord-bot.out" 2>&1 &
echo "discord bot pid: $!"
