# SPEC — WhatsApp ↔ Discord Relay v2

Status: implemented in this repository.
Companion: `SOUL.md` (principles). This file is the contract.

## 0. Scope

One WhatsApp account (linked device) mirrored into a private Discord forum.
Two directions:

- WhatsApp → Discord: text, media, replies, reactions, edits, deletes, read
  receipts, polls, locations, contacts/vCards, stickers and mentions.
- Discord → WhatsApp: text, media, replies, reactions, edits and deletes from
  allowed users only.

Out of scope: WhatsApp channels/newsletters, status broadcasts, voice/video
calls, group administration, E2E key management.

## 1. Architecture

```
Android WhatsApp
      │  (whatsmeow, linked device)
      ▼
Go bridge (whatsapp-bridge)  ── REST ──►  Python relay (discord-bot)
      │                                   │
      ├─ messages.db (SQLite, WAL)         ├─ relay.db (SQLite, WAL)
      └─ whatsapp.db (whatsmeow session)   ├─ discord_map.json
                                           └─ media_cache/
```

- Bridge owns WhatsApp I/O, message storage, media download and event tables.
- Relay owns Discord I/O, thread mapping, display names, media cache and
  backfill/repair jobs.
- The only cross-process contract is HTTP + the read-only SQLite databases.
- One writer per SQLite database; readers use WAL and `busy_timeout`.

## 2. Configuration

`discord_config.json` (safe defaults shown):

```json
{
  "bridge_url": "http://127.0.0.1:8080",
  "guild_id": "…",
  "forum_channel_id": "…",
  "allowed_users": [],
  "owner_id": "…",
  "sync_days": 30,
  "history_days": 7,
  "backfill_days": 30,
  "backfill_limit": 50,
  "thread_policy": "mapped",
  "allowlist": [],
  "archive_after_days": 30,
  "mark_read_on_relay": false,
  "mirror_edits": true,
  "mirror_deletes": true,
  "media_cache_days": 14,
  "media_cache_max_mb": 512,
  "media_max_mb": 8,
  "discord_rate_per_sec": 4,
  "contacts_refresh_seconds": 900
}
```

- `thread_policy`: `mapped` = existing mapped chats + all DMs; `allowlist` =
  existing mapped chats + allowlisted groups + DMs; `all` = previous behavior.
- `bridge.token` (mode 600) is shared by bridge and bot. If absent, the bridge
  binds to loopback only and logs a warning; production must have it.
- Environment overrides: `BRIDGE_BIND` (default `127.0.0.1:8080`),
  `BRIDGE_TOKEN_FILE` (default `../bridge.token` from the bridge directory).

## 3. SQLite contract

### 3.1 messages.db (bridge writes, relay/notifier read)

Connection: `file:store/messages.db?_foreign_keys=on&_journal_mode=WAL&_busy_timeout=10000&_synchronous=NORMAL`,
`SetMaxOpenConns(1)` (single writer).

Core columns:

| table | columns |
|---|---|
| `chats` | `jid PK, name, last_message_time` |
| `messages` | `id, chat_jid, sender, push_name, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length, quoted_id, quoted_sender, quoted_content, mentioned_jids, deleted, edited` |
| `message_events` | `id PK AUTOINCREMENT, chat_jid, message_id, kind, content, sender, timestamp, is_from_me` |
| `reaction_events` | `id PK AUTOINCREMENT, chat_jid, message_id, sender, emoji, timestamp, is_from_me` |
| `receipt_events` | `id PK AUTOINCREMENT, chat_jid, message_id, receipt_type, timestamp` |

Indexes: `(chat_jid, timestamp)`, `(sender)`, `(chat_jid, id)` for events.

`messages` is UPSERT-only on `(id, chat_jid)`: preserve `rowid`; never overwrite
a non-empty live field with an empty history field.

### 3.2 relay.db (bot writes)

`relay_db.py` enables WAL, `synchronous=NORMAL`, `busy_timeout=10000`.

- `msg_map(wa_id, chat_jid, thread_id, discord_id, direction, ts)` — PK
  `(wa_id, chat_jid)`.
- `reaction_mirror(chat_jid, wa_id, sender, emoji)`.
- `receipt_mirror(chat_jid, wa_id)` — one Discord ✅ per read WA message.
- `sender_names(chat_jid, sender, name, source, updated_at)` — resolved display
  cache.
- `meta(key, value)` — cursors, rebuild flags, queue state.

## 4. Bridge REST API

All `/api/*` except `GET /api/health` require
`Authorization: Bearer <bridge.token>`. Bind loopback by default.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/send` | `recipient, message, media_path?, quoted_message_id?, quoted_sender?` | `{success, message_id}` |
| POST | `/api/react` | `chat_jid, message_id, emoji` | `{success, message_id}` |
| POST | `/api/download` | `chat_jid, message_id, max_bytes?` | `{success, path, filename}` |
| POST | `/api/history` | `chat_jid, before_id?, count?` | requests on-demand history |
| POST | `/api/edit` | `chat_jid, message_id, message` | edits an own message |
| POST | `/api/delete` | `chat_jid, message_id` | revokes an own message |
| POST | `/api/read` | `chat_jid, message_ids[]` | marks messages read |
| GET | `/api/health` | — | `{ok, connected, uptime, db_bytes, messages, max_rowid}` |
| GET | `/api/status` | — | health + WAL bytes, chats, last message, goroutines |

`/api/download` rejects files larger than `max_bytes` (default 8 MiB) before
writing; media files are stored under `store/<chat_jid>/`.

## 5. Feature specifications

### F1 — SQLite reliability

- WAL + `busy_timeout` on `messages.db`, `whatsapp.db`, `relay.db`.
- Bridge serializes all writes through one connection; history sync and live
  events cannot race.
- No `database is locked` in normal operation. Acceptance: run a history sync
  while live messages arrive for 5 minutes; zero lock errors.

### F2 — Media pipeline

- Media is downloaded only from `post_to_thread`, i.e. after a chat is mapped
  and the message is actually mirrored.
- Cache key = SHA-256 of downloaded bytes; extension preserved/converted.
- `webp`, `heic`, `heif` images are converted to PNG/JPEG with Pillow +
  pillow-heif when possible; on failure the original is sent with a note.
- Size checked against `media_max_mb` before and after download.
- Background cleanup every 6 h: delete cache files older than
  `media_cache_days`; if total exceeds `media_cache_max_mb`, evict oldest first.
- Executables remain rejected. Stickers are treated as images.
- Acceptance: second post of a cached message does not call `/api/download`;
  conversion produces a Discord-renderable file; cleanup never deletes a file
  currently being uploaded.

### F3 — Discord reliability

- One global async token bucket (`discord_rate_per_sec`) plus per-call retry.
- Every send/reply/edit/delete/reaction goes through `safe_discord`, which:
  - waits for a token;
  - retries `429` using `Retry-After` + jitter;
  - retries transient `5xx` with exponential backoff;
  - never retries `403/404`.
- Rebuild deletes old bot messages with the same limiter; 429s are logged but
  do not abort the rebuild.
- Acceptance: a 100-message rebuild completes without uncaught 429 errors and
  without duplicate posts.

### F4 — Names

- Contact/LID maps are cached with `contacts_refresh_seconds` and the DB file
  mtime; no full contact query every 5 minutes.
- Per-message `push_name` is authoritative for unsaved members.
- When a live message reveals a push name, the bridge backfills all existing
  rows for that sender whose `push_name` is empty (all chats).
- Display order: saved contact → LID→PN contact → per-message push name →
  phone → group placeholder.
- `/repair <chat>` pages `/api/history` backwards (100 at a time, max 10 pages)
  until 30 days or no progress, then rebuilds the thread.
- Acceptance: a new group message from an unsaved member shows their push
  name; a repaired old message never shows the numeric group JID.

### F5 — Security and observability

- Bearer token required for every mutating/read API except health.
- Loopback bind, token file mode 600, no token in logs.
- Discord `/status`: thread count, relay lag, bridge health/latency, DB size,
  media cache size/count, contacts cache age, last error, queue state.
- `/api/health` is suitable for a 30-second watchdog; `healthcheck.sh` exists.
- Acceptance: unauthenticated `/api/send` returns 401; `/status` answers in one
  message; killing the bridge makes health fail within one timeout.

### F6 — WhatsApp features

- **Edits**: WhatsApp edit events update the stored row and append a
  `message_events(kind=edit)`; Discord edits the mapped message in place.
- **Deletes/revokes**: revoke events mark `messages.deleted=1` and append
  `kind=delete`; Discord deletes the mapped message. History rebuild skips
  deleted rows.
- **Polls**: creation is rendered as a text block with question and options.
- **Location**: rendered as `[lokasi] name — address` + Google Maps link.
- **Contact/vCard**: rendered as `[kontak] name — phone` (all numbers for
  multi-contact messages).
- **Mentions**: `mentioned_jids` stored; displayed as `@DisplayName` using the
  same resolver, never as a raw group JID.
- **Stickers**: downloaded and sent as images.
- **Read receipts**: WA read receipts add a ✅ to the mapped Discord message
  once; optional `mark_read_on_relay` marks WA messages read when relayed.
- **Discord → WA edits/deletes**: own mapped messages can be edited/revoked
  through `/api/edit` and `/api/delete`; failures are reported with a reaction.
- Acceptance: edit/delete round-trip in both directions; poll/location/vCard
  render readable text; a mention shows a name.

### F7 — Efficiency

- `thread_policy` prevents creating threads for every group; `allowed` chats
  are explicit.
- Status, newsletters and broadcasts are never mirrored or downloaded.
- Idle threads are archived after `archive_after_days` (best effort; missing
  Manage Threads is logged once, not fatal).
- `/allow`/`/deny` manage the group allowlist; `/open` remains the manual
  escape hatch.
- Media cache and sender-name cache are bounded.
- Acceptance: no new group thread for an unallowlisted group; idle mapped
  threads archive; DB/cache sizes stay bounded.

## 6. Relay flow

1. `relay_watcher` polls `messages WHERE rowid > cursor` (40 at a time).
2. Skip non-relayable JIDs; advance cursor even for skipped rows.
3. If not mapped and `thread_policy` forbids auto-creation, advance and skip.
4. `post_to_thread`: idempotency check → resolve name → quote/reply → media
   cache → rate-limited send → mapping insert.
5. `message_event_watcher` applies edit/delete events to mapped Discord
   messages.
6. `reaction_watcher` and `receipt_watcher` mirror reactions/read receipts.
7. `watcher` creates threads for newly allowed chats only.
8. `cleanup` evicts old media and archives idle threads daily.

## 7. Failure handling

| Failure | Behavior |
|---|---|
| Bridge down | relay logs, retries on next poll; `/status` shows DOWN |
| Discord 429 | wait Retry-After + jitter, retry up to 6 times |
| Discord 403/404 | log once, do not retry, do not advance message mapping |
| SQLite busy | `busy_timeout`; single writer; retry transaction |
| Media too large | placeholder note; no download after size check |
| Unknown sender | push name → phone → `Anggota grup`/`Kontak` |
| Missing push name in history | honest phone/placeholder; `/repair` tries |
| Meta AI text reply not delivered to linked device | cannot relay; logged as known limitation |

## 8. Tests

- `go build ./...` and `go vet ./...` in `whatsapp-bridge`.
- `cd discord-bot && .venv/bin/python -m unittest discover -s tests -v`.
- New pure tests cover: name resolution, mention formatting, poll/location/
  vCard formatting, media classification/conversion decision, rate limiter
  token math, allowlist policy, edit/delete event formatting.
- Manual smoke: `/status`, `/open`, `/repair`, send text/media/reaction, edit
  a WhatsApp message, delete a WhatsApp message, poll, location, vCard.

## 9. Rollout

1. Build bridge and run migrations (WAL, new columns/tables).
2. Create `bridge.token`; restart bridge with `BRIDGE_BIND=127.0.0.1:8080`.
3. Restart bot; verify `/status` and no repeated “database is locked”.
4. Run `/repair` for one affected group; verify names and order.
5. Enable `thread_policy=mapped` (default) and archive idle threads.
6. Keep backups: `whatsapp-bridge/whatsapp-bridge.bak*`, `relay.db.pre-rebuild`.

## 10. Known limitations

- WhatsApp Meta AI text replies are not delivered to linked devices; only
  attachments/history are available. Rich-response extraction is implemented
  for the day they are delivered.
- Historical push names that the server never sent cannot be invented; the
  bridge backfills them when the same sender is seen live.
- Discord cannot edit/delete messages authored by users; only bot messages are
  editable by the bot. Discord→WA edit/delete requires the bot to own the
  mapped Discord message.
- Forum thread archiving requires Manage Threads; without it the task logs and
  skips.
