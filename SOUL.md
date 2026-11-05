# SOUL — WhatsApp ↔ Discord Relay

This file is the product soul: the non-negotiable principles used to decide
whether a change belongs in this project. Read it before `SPEC.md`.

## 1. Satu sumber kebenaran

WhatsApp is the source of truth for messages, media metadata and message
identity. Discord is a mirror. Never mutate WhatsApp history to make Discord
look nicer, and never invent a message that WhatsApp did not send.

## 2. Idempoten, selalu

Every relay operation must be safe to retry. A message is posted once per
`(chat_jid, wa_id)`. History syncs, bridge restarts, Discord reconnects and
429 retries must not duplicate messages or change `rowid` (the poll cursor).
Database upserts preserve the existing row and its live-only fields.

## 3. Privasi lebih dulu

The bridge binds to loopback and requires a bearer token. Forums are locked to
the bot and explicitly allowed users. Executables are rejected. Media is only
downloaded for chats that are actually mirrored. Logs never contain tokens,
secrets or raw media bytes.

## 4. Kompatibel dengan WhatsApp nyata

WhatsApp payloads are messy: LIDs, device-sent wrappers, ephemeral/view-once,
edited messages, bot messages, rich responses, polls, vCards, locations and
missing push names. Parse defensively, prefer the participant carried by the
message, and degrade to a clear placeholder instead of crashing.

## 5. Efisien secara default

Do the smallest amount of work that preserves correctness:
- poll by indexed `rowid`;
- create threads lazily;
- download media only when a mirrored message needs it;
- cache by content hash;
- batch/rate-limit Discord calls;
- archive idle threads;
- skip status, newsletters and broadcasts.

## 6. Observability is a feature

If it cannot be measured, it cannot be operated. Expose health/status, cursor
lag, queue depth, database/cache size, bridge latency and recent errors. A
human should be able to answer “is it working?” from one `/status` command.

## 7. Best-practice code

- Small, named functions with a single responsibility.
- Pure helpers live in `relay_core.py` and are unit-tested.
- No secrets in source; config is validated and has safe defaults.
- Errors are logged with context and do not kill the poller.
- Every schema change is an idempotent migration.
- Every concurrency change has an explicit ordering/idempotency argument.
- Tests run without Discord, WhatsApp or network access.

## 8. No feature without an exit

Every background task needs a stop condition, timeout, backoff and a dry-run or
status surface. No unbounded queue, unbounded retry or unbounded cache.

## 9. Hormati pengguna

Messages are mirrored, not judged. Do not alter wording, hide context or
silently drop a message because its name is unknown. When data is unavailable
(for example an unsaved member's historical push name) say so with an honest
fallback, never with a wrong identity.

## 10. Definition of done

A change is done only when: code builds, unit tests pass, the bridge and bot
restart cleanly, `/status` is green, and the affected Discord thread/API was
smoke-tested.
