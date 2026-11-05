# WhatsApp MCP + Discord Relay

A self-hosted bridge between **your own WhatsApp account**, a **Model Context
Protocol (MCP) server**, and a **private Discord forum**.

It stores WhatsApp messages locally in SQLite, exposes them to MCP clients such
as Claude Desktop, Cursor, or OpenCode, and can mirror every allowed chat into
Discord in both directions.

> **Privacy first:** this project reads your personal WhatsApp data. Run it on a
> machine you control. The bridge listens on loopback only, all mutating API
> calls require a bearer token, and the Discord forum is locked to the bot plus
> explicitly allowed users. Never expose the bridge port to the internet.

---

## Contents

- [What you get](#what-you-get)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
  - [1. Clone and create the bridge token](#1-clone-and-create-the-bridge-token)
  - [2. Build and link the WhatsApp bridge](#2-build-and-link-the-whatsapp-bridge)
  - [3. Install the MCP server](#3-install-the-mcp-server)
  - [4. Connect the MCP client](#4-connect-the-mcp-client)
  - [5. Create the Discord bot](#5-create-the-discord-bot)
  - [6. Configure and start the relay](#6-configure-and-start-the-relay)
  - [7. Verify everything](#7-verify-everything)
- [Configuration reference](#configuration-reference)
- [Discord commands](#discord-commands)
- [Optional DM notifier](#optional-dm-notifier)
- [Running as a service](#running-as-a-service)
- [Security model](#security-model)
- [REST API](#rest-api)
- [MCP tools](#mcp-tools)
- [Data, backups and reset](#data-backups-and-reset)
- [Troubleshooting](#troubleshooting)
- [Development and tests](#development-and-tests)
- [Known limitations](#known-limitations)
- [Project layout](#project-layout)
- [License and credits](#license-and-credits)

---

## What you get

### WhatsApp ↔ Discord relay

- One Discord forum thread per WhatsApp chat:
  - `DM · Name`
  - `Grup · Name`
- Two-way text, replies/quotes, and media.
- WhatsApp → Discord: images, video, audio, documents, stickers, polls,
  locations, contacts/vCards, mentions, edits, deletes, reactions, and read
  receipts.
- Discord → WhatsApp: text, media, replies, reactions, edits, and deletes from
  allowed users.
- Automatic thread creation, history backfill, and repair.
- Display-name resolution for saved contacts, LIDs, and unsaved group members.
- Media cache with SHA-256 deduplication and HEIC/WebP conversion.
- Rate-limited Discord calls with `429`/`5xx` retry handling.
- Private forum permissions, executable blocking, and size limits.

### WhatsApp MCP server

The original MCP integration is included: search chats and messages, inspect
context, send text, send files, send voice notes, and download media through
MCP tools.

### Optional DM notifier

`notifier.py` watches incoming private messages and can send phone
notifications, digests, AI summaries/draft replies, reminders, and a morning
briefing. It is optional; the relay works without it.

---

## Architecture

```text
┌─────────────────────┐
│ WhatsApp on phone   │
│ linked device       │
└──────────┬──────────┘
           │ whatsmeow (multi-device protocol)
           ▼
┌─────────────────────────────┐
│ Go bridge                   │
│ whatsapp-bridge/            │
│                             │
│ - session + message store   │
│ - media download            │
│ - edit/delete/receipt events│
│ - REST API + bearer token   │
└──────────┬──────────────────┘
           │
           │ local SQLite (WAL)          HTTP + token
           │                             │
           ▼                             ▼
┌─────────────────────┐       ┌─────────────────────────┐
│ messages.db         │       │ Python MCP server       │
│ whatsapp.db         │       │ whatsapp-mcp-server/    │
└──────────┬──────────┘       └────────────┬────────────┘
           │ read-only                      │ MCP
           │                                ▼
           │                     ┌─────────────────────┐
           │                     │ Claude / Cursor /   │
           │                     │ OpenCode            │
           │                     └─────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ Python Discord relay        │
│ discord-bot/                │
│                             │
│ - thread mapping            │
│ - backfill / repair         │
│ - media cache               │
│ - reactions / receipts      │
│ - commands                  │
└──────────┬──────────────────┘
           │ Discord API
           ▼
┌─────────────────────────────┐
│ Private Discord forum       │
│ one thread per WA chat      │
└─────────────────────────────┘
```

The bridge is the only writer to `messages.db`. The relay and notifier open it
read-only. `relay.db`, `discord_map.json`, and `media_cache/` belong to the
relay.

---

## Requirements

| Component | Requirement |
|---|---|
| OS | Linux or macOS recommended; Windows works with CGO notes below |
| Go | Go 1.26+ (see `whatsapp-bridge/go.mod`) |
| C toolchain | Required by `go-sqlite3`; install `build-essential` on Debian/Ubuntu or Xcode Command Line Tools on macOS |
| Python | Python 3.11+ recommended |
| MCP client | Claude Desktop, Cursor, OpenCode, or any MCP-compatible client |
| Discord | A server where you can create a private forum channel and invite a bot |
| WhatsApp | WhatsApp account with a linked-device slot available |
| Optional | `ffmpeg` for converting audio to playable WhatsApp voice notes |

### Debian/Ubuntu example

```bash
sudo apt update
sudo apt install -y git curl build-essential python3 python3-venv python3-pip
```

Install Go from <https://go.dev/dl/> if your distribution package is too old.

### macOS example

```bash
xcode-select --install
brew install go python
```

### Windows

`go-sqlite3` needs CGO and a C compiler. With MSYS2:

```powershell
go env -w CGO_ENABLED=1
```

Add `C:\msys64\ucrt64\bin` to `PATH`, then build from an MSYS2 shell or a
terminal where `gcc` is available. Without this, the bridge fails with:

```text
Binary was compiled with 'CGO_ENABLED=0', go-sqlite3 requires cgo to work.
```

---

## Quick start

The commands below assume Linux/macOS and a repository at
`/path/to/whatsapp-mcp`. Replace paths with your own.

### 1. Clone and create the bridge token

```bash
git clone https://github.com/YOUR_USERNAME/whatsapp-mcp-discord-relay.git
cd whatsapp-mcp-discord-relay

# Generate a shared secret. Never commit this file.
python3 -c 'import secrets; print(secrets.token_hex(32))' > bridge.token
chmod 600 bridge.token
```

The bridge, MCP server, and relay all read `bridge.token` from the repository
root by default:

- bridge: `../bridge.token` relative to `whatsapp-bridge/`
- MCP: `../bridge.token` relative to `whatsapp-mcp-server/`
- relay: `bridge.token` relative to the repository root

If you move the file, set `BRIDGE_TOKEN_FILE` for the bridge and MCP server.

### 2. Build and link the WhatsApp bridge

```bash
cd whatsapp-bridge
go mod download
go build -o whatsapp-bridge .
```

#### First login with a pairing code (recommended)

From the repository root:

```bash
./pair.sh 6281234567890
```

Use your own phone number with country code and digits only. The script prints
a pairing code. On your phone:

1. Open WhatsApp.
2. Go to **Settings → Linked devices → Link a device**.
3. Choose **Link with phone number**.
4. Enter the code.

The code is also written to `/tmp/whatsapp_pair_code.txt`.

#### First login with a QR code

Run the bridge in the foreground so the QR code is visible:

```bash
cd whatsapp-bridge
./whatsapp-bridge
```

Scan the QR code from **WhatsApp → Linked devices → Link a device**. After a
successful login, stop the foreground process with `Ctrl+C` and use the start
script from then on.

The session is stored in `whatsapp-bridge/store/whatsapp.db`. Treat that file
like a password: anyone with it may be able to access the linked account.

#### Start the bridge in the background

```bash
cd ..
./start-bridge.sh
```

Check it:

```bash
./healthcheck.sh
curl -s http://127.0.0.1:8080/api/health
```

Expected health response includes:

```json
{"ok": true, "connected": true, "messages": 12345}
```

> If `bridge.token` is missing, the bridge binds to loopback and runs without
> authentication. That is only acceptable for a first local test. Create the
> token before exposing anything or running the relay.

### 3. Install the MCP server

Using `uv` (recommended by the original project):

```bash
cd whatsapp-mcp-server
uv sync
uv run main.py
```

If you do not use `uv`:

```bash
cd whatsapp-mcp-server
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install httpx "mcp[cli]" requests
.venv/bin/python main.py
```

The MCP server reads `messages.db` directly and calls the bridge REST API for
sending and media downloads. The bridge must be running.

### 4. Connect the MCP client

Use absolute paths in the client configuration.

#### Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/absolute/path/to/uv",
      "args": [
        "--directory",
        "/absolute/path/to/whatsapp-mcp/whatsapp-mcp-server",
        "run",
        "main.py"
      ]
    }
  }
}
```

If you installed with a plain venv instead, use the Python binary directly:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/absolute/path/to/whatsapp-mcp/whatsapp-mcp-server/.venv/bin/python",
      "args": ["/absolute/path/to/whatsapp-mcp/whatsapp-mcp-server/main.py"]
    }
  }
}
```

#### Cursor

Save the same structure as `~/.cursor/mcp.json`, then restart Cursor.

#### OpenCode

Add the server to your OpenCode config using the equivalent local MCP command
for your installation. Keep the absolute path to `whatsapp-mcp-server` and
ensure the bridge is running.

After restarting the client, the WhatsApp MCP tools should appear.

### 5. Create the Discord bot

1. Open <https://discord.com/developers/applications>.
2. Create a new application.
3. Go to **Bot → Add Bot** and copy the bot token.
4. Enable **Message Content Intent** under **Privileged Gateway Intents**.
   Without it, the relay cannot read messages you type in Discord.
5. Invite the bot to your server with **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Permissions:
     - View Channels
     - Send Messages
     - Send Messages in Threads
     - Create Public Threads
     - Read Message History
     - Attach Files
     - Add Reactions
     - Embed Links
     - **Manage Threads** (recommended; needed for archive/unarchive)
     - **Manage Messages** (optional; only if you want the bot to delete
       messages it did not author)
6. Create a **forum channel** named `WhatsApp` in your server.
7. Enable Discord **Developer Mode**, then copy:
   - the server (guild) ID
   - the forum channel ID
   - your own Discord user ID (used as owner)

The bot can create the forum automatically only if it has the necessary
channel permissions. Creating it manually is more predictable.

### 6. Configure and start the relay

```bash
cd /path/to/whatsapp-mcp
cp discord_config.example.json discord_config.json
chmod 600 discord_config.json
```

Edit `discord_config.json`:

```json
{
  "token": "YOUR_DISCORD_BOT_TOKEN",
  "guild_id": "YOUR_SERVER_ID",
  "forum_channel_id": "YOUR_FORUM_CHANNEL_ID",
  "owner_id": "YOUR_DISCORD_USER_ID",
  "allowed_users": [],
  "bridge_url": "http://127.0.0.1:8080"
}
```

Keep the remaining values from the example unless you need to change them.

Install the relay dependencies:

```bash
cd discord-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cd ..
```

Start the relay:

```bash
./start-discord.sh
```

Or start the bridge, optional notifier, and relay together:

```bash
./start-all.sh
```

Logs:

```bash
tail -f discord-bot/discord-bot.log
tail -f whatsapp-bridge/store/bridge.log
```

### 7. Verify everything

In Discord, run:

```text
/status
```

You should see the bridge state, relay lag, database size, media cache size,
contacts cache age, and last error.

Also check:

```bash
./healthcheck.sh
curl -s http://127.0.0.1:8080/api/health
```

Expected:

- `/status` shows `Bridge: OK`
- health returns `ok: true` and `connected: true`
- new WhatsApp messages appear in the mapped thread
- typing in a mapped thread sends a WhatsApp message
- `/sync` can create threads for chats in the selected time window

The first sync can take a while because Discord rate limits thread creation.
Use `/sync` with `7 hari` first, then run `30 hari` later if needed.

---

## Configuration reference

### `discord_config.json`

| Key | Default | Meaning |
|---|---|---|
| `token` | — | Discord bot token. Keep this file at mode `600`. |
| `guild_id` | — | Discord server ID. |
| `forum_channel_id` | — | Forum channel ID. If omitted, the bot tries to find/create `WhatsApp`. |
| `owner_id` | — | Discord user ID allowed to run commands when `allowed_users` is empty. |
| `allowed_users` | `[]` | Extra Discord user IDs allowed to use commands and write to WhatsApp. Empty means owner only. |
| `bridge_url` | `http://127.0.0.1:8080` | Bridge REST URL. |
| `sync_days` | `30` | Startup sync window. |
| `history_days` | `7` | Extra context window used when a new message arrives. |
| `backfill_days` | `30` | Thread backfill window. |
| `backfill_limit` | `50` | Maximum messages backfilled per chat. |
| `thread_policy` | `mapped` | `mapped`: existing mapped chats + DMs; `allowlist`: existing mapped chats + allowlisted groups + DMs; `all`: all groups. |
| `allowlist` | `[]` | Group JIDs allowed when using the `allowlist` policy. Managed by `/allow` and `/deny`. |
| `archive_after_days` | `30` | Archive idle mapped threads after this many days. `0` disables. Requires Manage Threads. |
| `mark_read_on_relay` | `false` | Mark WhatsApp messages read after they are relayed. |
| `mirror_edits` | `true` | Mirror WhatsApp edits to Discord. |
| `mirror_deletes` | `true` | Mirror WhatsApp deletes/revokes to Discord. |
| `media_cache_days` | `14` | Delete cached media older than this. |
| `media_cache_max_mb` | `512` | Maximum media cache size. |
| `media_max_mb` | `8` | Maximum size of one WhatsApp media file. |
| `discord_rate_per_sec` | `4` | Global Discord API token bucket. |
| `contacts_refresh_seconds` | `900` | Contact/LID cache refresh interval. |
| `private_category` | `true` | Lock the forum to the bot, owner, and allowed users. |

### Environment variables

| Variable | Used by | Default |
|---|---|---|
| `BRIDGE_BIND` | bridge | `127.0.0.1:8080` |
| `BRIDGE_TOKEN_FILE` | bridge, MCP | `../bridge.token` relative to component |
| `WHATSAPP_PAIR_PHONE` | bridge | unset; enables pairing-code login |
| `WHATSAPP_BRIDGE_URL` | MCP | `http://localhost:8080` |
| `PHONE_MCP_URL` | notifier | `http://127.0.0.1:4097/mcp` |
| `PHONE_MCP_TOKEN` | notifier | unset |
| `COPILOT_TOKEN` | notifier AI | unset |

### `notifier_config.json`

See `notifier_config.example.json`. Important keys:

- `vip`: names, phone numbers, or JIDs that get instant notifications. Empty
  means every private chat is treated as VIP.
- `quiet_hours`: do not notify during this local-time range.
- `digest_minutes`: how long to wait before sending a digest for non-VIP chats.
- `briefing_time`: morning briefing time.
- `ai`: enable AI summaries/drafts.
- `ai_model`: model name for the Copilot-compatible endpoint.
- `debounce_seconds`: wait for a burst of messages before notifying.
- `auto_reminder`: extract reminder text and create phone reminders.
- `max_fresh_minutes`: ignore old messages after a restart.
- `utc_offset_hours`: local timezone offset for quiet hours and briefings.

---

## Discord commands

Commands are available to the owner and `allowed_users`.

| Command | What it does |
|---|---|
| `/status` | Bridge health, relay/event lag, DB/WAL size, media cache, contacts cache, last error. |
| `/sync days:<7\|30\|all>` | Create threads for chats in the window. Includes incoming-only chats. |
| `/open query:<name/phone/JID>` | Create and backfill a thread for one chat. |
| `/backfill days:<7\|30> limit:<1-200>` | Rebuild a chat: delete old bot posts and repost history in order. |
| `/repair query:<name/phone/JID>` | Pull older WhatsApp history (up to 10 pages), then rebuild the thread. |
| `/allow query:<group>` | Allow a group and create its thread. |
| `/deny query:<group>` | Remove a group from the allowlist. The old thread stays. |
| `/private` | Re-apply the private forum permissions. |

The bot also mirrors Discord message edits, deletes, and reactions from
`allowed_users` back to WhatsApp when the message is mapped to a WhatsApp
message.

---

## Optional DM notifier

The notifier is independent of the Discord relay. It watches **private**
incoming messages and can use a phone MCP endpoint for notifications and
reminders.

```bash
cp notifier_config.example.json notifier_config.json
chmod 600 notifier_config.json
```

Set the phone MCP endpoint if you use one:

```bash
export PHONE_MCP_URL="http://127.0.0.1:4097/mcp"
export PHONE_MCP_TOKEN="your-phone-mcp-token"
```

For AI summaries, set a GitHub Copilot-compatible token:

```bash
export COPILOT_TOKEN="gho_..."
```

If `COPILOT_TOKEN` is not set, the notifier may read a `gho_...` token from an
existing `~/.config/opencode/opencode.jsonc`. No token is stored in this
repository.

Useful commands:

```bash
# Preview recent private messages without notifying anyone
python3 notifier.py --dry-run

# Send the morning briefing immediately
python3 notifier.py --briefing

# Start in the background
./start-notifier.sh
```

The notifier only reads `messages.db`; it never writes to WhatsApp. It skips
groups, newsletters, broadcasts, and muted chats.

---

## Running as a service

For a small always-on machine, `keepalive.sh` is enough:

```bash
nohup ./keepalive.sh >> keepalive.log 2>&1 &
```

It checks the bridge health endpoint every 30 seconds and restarts the bridge,
notifier, and relay if they are missing. It only starts the relay when
`discord-bot.enabled` exists, which `start-discord.sh` creates.

For a systemd-managed deployment, create units like these and adjust paths and
the user:

```ini
# /etc/systemd/system/whatsapp-bridge.service
[Unit]
Description=WhatsApp bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/whatsapp-mcp/whatsapp-bridge
ExecStart=/path/to/whatsapp-mcp/whatsapp-bridge/whatsapp-bridge
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/whatsapp-discord-relay.service
[Unit]
Description=WhatsApp Discord relay
After=network-online.target whatsapp-bridge.service
Wants=network-online.target whatsapp-bridge.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/whatsapp-mcp
ExecStart=/path/to/whatsapp-mcp/discord-bot/.venv/bin/python /path/to/whatsapp-mcp/discord-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now whatsapp-bridge whatsapp-discord-relay
sudo journalctl -u whatsapp-discord-relay -f
```

Do not run `keepalive.sh` and systemd for the same processes at the same time.

---

## Security model

- The bridge binds to `127.0.0.1:8080` by default.
- Every `/api/*` endpoint except `GET /api/health` requires
  `Authorization: Bearer <bridge.token>`.
- If no token is configured, the bridge refuses to bind to a non-loopback
  address and logs a warning.
- The relay and forum permissions are locked to the bot, owner, and
  `allowed_users`.
- Executable attachments are rejected.
- Media downloads are capped by `media_max_mb`.
- Logs do not print the token or raw media bytes.
- `discord_config.json`, `bridge.token`, session databases, message databases,
  media cache, and generated maps are ignored by Git.

Never commit:

```text
bridge.token
discord_config.json
notifier_config.json
whatsapp-bridge/store/
relay.db*
discord_map.json
media_cache/
*.log
```

If you fork this repository, keep the `.gitignore` intact.

---

## REST API

All endpoints except health require the bearer token.

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | — | Public health check. |
| `GET` | `/api/status` | — | Authenticated status, WAL size, last message, goroutines. |
| `POST` | `/api/send` | `recipient, message, media_path?, quoted_message_id?, quoted_sender?` | Send text or a file. |
| `POST` | `/api/react` | `chat_jid, message_id, emoji` | Send a reaction. |
| `POST` | `/api/download` | `chat_jid, message_id, max_bytes?` | Download media and return a local path. |
| `POST` | `/api/history` | `chat_jid, before_id?, count?` | Request older history from WhatsApp. |
| `POST` | `/api/edit` | `chat_jid, message_id, message` | Edit an own message. |
| `POST` | `/api/delete` | `chat_jid, message_id` | Revoke an own message. |
| `POST` | `/api/read` | `chat_jid, message_ids[], sender?` | Mark messages read. |

Example:

```bash
TOKEN="$(cat bridge.token)"

curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8080/api/status

curl -sS \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8080/api/send \
  -d '{"recipient":"6281234567890","message":"Hello from the bridge"}'
```

---

## MCP tools

The MCP server exposes:

- `search_contacts`
- `list_messages`
- `list_chats`
- `get_chat`
- `get_direct_chat_by_contact`
- `get_contact_chats`
- `get_last_interaction`
- `get_message_context`
- `send_message`
- `send_file`
- `send_audio_message`
- `download_media`

Media notes:

- `send_file` sends images, video, documents, and raw audio.
- `send_audio_message` sends playable voice notes; install `ffmpeg` to convert
  non-Opus audio automatically.
- `download_media` returns a local file path that you can open or pass to
  another tool.

---

## Data, backups and reset

### Where data lives

| Path | Contents |
|---|---|
| `whatsapp-bridge/store/whatsapp.db` | WhatsApp session and contact/LID metadata. |
| `whatsapp-bridge/store/messages.db` | Chats, messages, media metadata, event tables. |
| `relay.db` | Discord↔WhatsApp mappings, cursors, name cache, receipts. |
| `discord_map.json` | Chat → Discord thread map. |
| `media_cache/` | Cached media files. |
| `discord_config.json` | Bot token and relay settings. |
| `bridge.token` | Shared API secret. |

### Backup

Stop the services first, then copy the whole repository directory or at least:

```bash
cp -a whatsapp-bridge/store /safe/backup/whatsapp-store
cp -a relay.db discord_map.json bridge.token /safe/backup/
```

### Reset the relay only

Stop the relay, then remove:

```bash
rm -f relay.db relay.db-* discord_map.json
```

The next start recreates the relay state and threads as they are needed. This
does **not** delete WhatsApp data.

### Reset the WhatsApp session

Only do this if the session is broken. It requires re-linking the phone:

```bash
./stop-all.sh
rm -f whatsapp-bridge/store/whatsapp.db whatsapp-bridge/store/messages.db
./start-bridge.sh
```

`stop-all.sh` also stops the watchdog so it cannot restart the bridge while you
are resetting the session.

### Rebuild one Discord thread

Use `/repair <chat>` first. It pulls older history and rebuilds the thread. For
a manual rebuild of all mapped chats:

```text
/backfill days:30 limit:50
```

---

## Troubleshooting

### Bridge health is down

```bash
pgrep -af whatsapp-bridge
tail -100 whatsapp-bridge/store/bridge.log
./healthcheck.sh
```

Common causes:

- bridge not built or binary not executable
- another process already bound to port `8080`
- WhatsApp session expired
- DNS/network problem during reconnect

### QR code does not appear

Run the bridge in the foreground:

```bash
cd whatsapp-bridge
./whatsapp-bridge
```

The QR is printed to the terminal. `start-bridge.sh` redirects output to
`store/bridge.log`, so use the foreground command for the first login.

### Pairing code does not work

- Use digits only, including country code.
- Remove `+`, spaces, and dashes.
- Try a new code; codes expire.
- Check `store/bridge.log` for a timeout or “device limit” error.

### `401 Unauthorized`

The bridge and client do not share the same token.

```bash
ls -l bridge.token
cat bridge.token
```

Create the token at the repository root, restart the bridge, and restart the
relay. If you use a custom path, set `BRIDGE_TOKEN_FILE` consistently.

### `database is locked`

- Run only one bridge process.
- Do not run `go run main.go` while the compiled bridge is also running.
- Keep WAL enabled; the bridge and relay set it automatically.
- Check that the filesystem is local, not a flaky network share.

### No messages appear in Discord

1. Run `/status`; the bridge must be `OK`.
2. Check `tail -f discord-bot/discord-bot.log`.
3. Confirm `thread_policy` and `allowlist`.
4. Run `/open <chat>` for one chat to test.
5. Check that the bot has View Channel, Send Messages, and thread permissions.
6. Check that **Message Content Intent** is enabled in the Discord developer
   portal.

### No threads are created automatically

- For groups, the default `mapped` policy does not create new group threads.
  Use `/allow`, `/open`, or set `thread_policy` to `all`.
- For DMs, check that the chat is relayable and has recent messages.
- Check the bot’s channel permissions.

### Media is missing or fails

- WhatsApp media may expire; `/repair` can request older history.
- Check `media_max_mb`; the default is 8 MiB.
- Check disk space and `media_cache` permissions.
- HEIC/WebP conversion needs Pillow and pillow-heif; install requirements in the
  relay venv.
- Audio voice notes need `ffmpeg` for automatic conversion.

### Duplicate messages

- Make sure only one relay process is running:
  `pgrep -af bot.py`
- Do not run two bridges against the same database.
- A manual `/backfill` can create a new ordered history; it intentionally
  replaces old bot posts.

### “Manage Threads” errors

Grant **Manage Threads** to the bot. Without it, archiving and some thread
operations are skipped; the relay continues to work.

### Meta AI messages are not relayed

WhatsApp does not deliver Meta AI text replies to linked devices as normal
message events. Only attachments/history may be available. This is a WhatsApp
limitation, not a relay bug.

### Windows build errors

See the CGO section above. Install MSYS2, make `gcc` available, and set:

```powershell
go env -w CGO_ENABLED=1
```

---

## Development and tests

### Go bridge

```bash
cd whatsapp-bridge
go build ./...
go test ./...
go vet ./...
```

### Python relay

```bash
cd discord-bot
.venv/bin/python -m unittest discover -s tests -v
```

The relay tests do not need Discord, WhatsApp, or network access. They cover
name resolution, mention/poll/location/vCard formatting, media classification,
rate limiter math, allowlist policy, and edit/delete formatting.

### Useful checks

```bash
# Bridge health
./healthcheck.sh

# Authenticated status
curl -sS -H "Authorization: Bearer $(cat bridge.token)" \
  http://127.0.0.1:8080/api/status

# Python syntax check
python3 -m py_compile discord-bot/bot.py discord-bot/relay_db.py \
  discord-bot/relay_core.py discord-bot/media_cache.py

# Process check
pgrep -af 'whatsapp-bridge|bot.py|notifier.py'
```

### Reading the design docs

- `SOUL.md` — product principles and definition of done.
- `SPEC.md` — architecture, database contract, API, F1–F7 acceptance criteria,
  failure handling, and known limitations.

Read `SOUL.md` first if you plan to change relay behavior.

---

## Known limitations

- **Meta AI text replies** are not delivered to linked devices, so they cannot
  be relayed. Attachments/history may still arrive.
- **Historical push names** that WhatsApp never sent cannot be invented. The
  bridge backfills names when the same sender appears live.
- **Discord permissions**: the bot cannot delete messages authored by users
  unless it has Manage Messages. It also cannot archive threads without Manage
  Threads.
- **Multi-part messages**: edits/deletes currently target the first mapped
  Discord chunk of a very long WhatsApp message.
- **Database retention**: message/event tables grow with history; there is no
  automatic pruning policy yet. Monitor `/status` and archive/backup as needed.
- **One WhatsApp account per bridge.** Run separate instances with separate
  `store/` directories for multiple accounts.
- **No end-to-end encryption changes.** The bridge uses the normal WhatsApp
  linked-device protocol; WhatsApp remains the source of truth.

---

## Project layout

```text
.
├── bridge.token                  # generated, never commit
├── discord_config.example.json
├── discord_config.json           # generated, never commit
├── discord_map.json              # generated, never commit
├── discord-bot/
│   ├── bot.py                    # Discord relay and commands
│   ├── relay_core.py             # pure formatting/name/policy helpers
│   ├── relay_db.py               # relay SQLite schema and queries
│   ├── media_cache.py            # SHA-256 media cache and cleanup
│   ├── discord_util.py           # rate limiter and retry helper
│   ├── requirements.txt
│   └── tests/
├── whatsapp-bridge/
│   ├── main.go                   # WhatsApp client, store, REST API
│   ├── main_test.go
│   ├── go.mod
│   └── store/                    # generated session/message DBs
├── whatsapp-mcp-server/
│   ├── main.py                   # MCP server entry point
│   ├── whatsapp.py               # MCP tools
│   ├── audio.py
│   └── pyproject.toml
├── notifier.py                   # optional DM notifier
├── notifier_config.example.json
├── start-all.sh
├── start-bridge.sh
├── start-discord.sh
├── start-notifier.sh
├── stop-all.sh
├── pair.sh
├── healthcheck.sh
├── keepalive.sh
├── SOUL.md
└── SPEC.md
```

Generated runtime files are intentionally ignored by Git. Do not commit them.

---

## License and credits

This project is based on the excellent
[`lharries/whatsapp-mcp`](https://github.com/lharries/whatsapp-mcp) by Luke
Harries and uses [`whatsmeow`](https://github.com/tulir/whatsmeow). The original
project is MIT licensed; see `LICENSE`.

The Discord relay, notifier, bridge extensions, and documentation in this
repository are provided under the same MIT license unless stated otherwise.

If you use this project, keep your tokens, session databases, and message data
private.
