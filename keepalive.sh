#!/usr/bin/env bash
# Watchdog: keeps the WhatsApp bridge + notifier alive.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
PIDFILE=keepalive.pid
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$(date '+%F %T') keepalive already running ($pid)" >> keepalive.log
    exit 0
  fi
fi
echo "$(date '+%F %T') keepalive started ($$)" >> keepalive.log
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT
while true; do
  if ! pgrep -x whatsapp-bridge >/dev/null || ! ./healthcheck.sh >/dev/null 2>&1; then
    pkill -x whatsapp-bridge 2>/dev/null || true
    sleep 1
    (cd "$DIR/whatsapp-bridge" && nohup ./whatsapp-bridge >> store/bridge.log 2>&1 &)
    echo "$(date '+%F %T') bridge restarted (health check failed or process missing)" >> keepalive.log
  fi
  if ! pgrep -f "[n]otifier.py" >/dev/null; then
    nohup python3 "$DIR/notifier.py" >> "$DIR/notifier.out" 2>&1 &
    echo "$(date '+%F %T') notifier restarted" >> keepalive.log
  fi
  if [ -f "$DIR/discord-bot.enabled" ] && [ -x "$DIR/discord-bot/.venv/bin/python" ] && ! pgrep -f "$DIR/discord-bot/bot.py" >/dev/null; then
    nohup "$DIR/discord-bot/.venv/bin/python" "$DIR/discord-bot/bot.py" >> "$DIR/discord-bot.out" 2>&1 &
    echo "$(date '+%F %T') discord bot restarted" >> keepalive.log
  fi
  sleep 30
done
