#!/usr/bin/env bash
# Start the bridge, optional notifier, and Discord relay when configured.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
./start-bridge.sh
./start-notifier.sh
if [ -x "$DIR/discord-bot/.venv/bin/python" ] && [ -f "$DIR/discord_config.json" ]; then
  ./start-discord.sh
else
  echo "Discord relay skipped (install discord-bot/.venv and create discord_config.json)."
fi
