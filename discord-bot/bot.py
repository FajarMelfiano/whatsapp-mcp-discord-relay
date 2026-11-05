#!/usr/bin/env python3
"""Two-way WhatsApp <-> Discord relay built on forum topics."""

import asyncio
import datetime
import json
import logging
import os
import re
import sqlite3
import sys
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from relay_core import (
    MAX_DISCORD_TOTAL,
    MAX_WA_TO_DISCORD,
    chat_title,
    full_sender,
    format_relay_message,
    is_idle,
    is_relayable_jid,
    media_allowed,
    format_edit_event,
    format_mentions,
    parse_ts,
    reaction_emoji_ok,
    safe_filename,
    sender_display,
    should_mirror_reaction,
    split_message,
    thread_allowed,
    validate_attachments,
    within_days,
)
from relay_db import RelayDB
from discord_util import DiscordIO
from media_cache import MediaCache

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(BOT_DIR)
CFG_PATH = os.path.join(BASE, "discord_config.json")
MAP_PATH = os.path.join(BASE, "discord_map.json")
RELAY_DB = os.path.join(BASE, "relay.db")
DB = os.path.join(BASE, "whatsapp-bridge", "store", "messages.db")
WDB = os.path.join(BASE, "whatsapp-bridge", "store", "whatsapp.db")
LOG = os.path.join(BOT_DIR, "discord-bot.log")
ENABLED = os.path.join(BASE, "discord-bot.enabled")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG)],
)
log = logging.getLogger("wa-discord")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


cfg = load_json(CFG_PATH, {})
mapping = load_json(MAP_PATH, {"chats": {}})
mapping.setdefault("chats", {})
relay = RelayDB(RELAY_DB)
by_thread = {}
for _jid, _rec in mapping["chats"].items():
    if _rec.get("thread_id"):
        by_thread[int(_rec["thread_id"])] = _jid

BRIDGE = cfg.get("bridge_url", "http://127.0.0.1:8080").rstrip("/")
TOKEN_PATH = os.path.join(BASE, "bridge.token")
try:
    with open(TOKEN_PATH) as _f:
        BRIDGE_TOKEN = _f.read().strip()
except OSError:
    BRIDGE_TOKEN = ""
MEDIA_MAX = int(cfg.get("media_max_mb", 8)) * 1024 * 1024
GUILD_ID = int(cfg.get("guild_id", 0))
GUILD = discord.Object(id=GUILD_ID)
BACKFILL_DAYS = int(cfg.get("backfill_days", 30))
BACKFILL_LIMIT = int(cfg.get("backfill_limit", 50))
MSG_COLS = (
    "rowid, id, chat_jid, sender, push_name, content, timestamp, is_from_me, "
    "media_type, filename, file_length, quoted_id, quoted_sender, quoted_content, "
    "mentioned_jids, deleted, edited"
)
rebuilding_jids = set()
last_error = ""
discord_io = DiscordIO(cfg.get("discord_rate_per_sec", 4))
media_cache = MediaCache(
    os.path.join(BASE, "media_cache"), relay,
    max_bytes=int(cfg.get("media_cache_max_mb", 512)) * 1024 * 1024,
    days=int(cfg.get("media_cache_days", 14)),
)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
session = None
sync_lock = asyncio.Lock()
started = False
_contacts = {"at": 0.0, "mtime": 0.0, "names": {}, "pns": {}}


def db(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
    except sqlite3.Error:
        pass
    return con


def load_contacts():
    names, pns = {}, {}
    try:
        con = db(WDB)
        try:
            for r in con.execute(
                "SELECT their_jid, push_name, full_name, business_name FROM whatsmeow_contacts"
            ):
                n = r["full_name"] or r["push_name"] or r["business_name"]
                if n and r["their_jid"] not in names:
                    names[r["their_jid"]] = n
            for r in con.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                pns[r["lid"]] = r["pn"]
        finally:
            con.close()
    except sqlite3.Error as e:
        log.error("contact db error: %s", e)
    for lid, phone in pns.items():
        lid_name = names.get(lid + "@lid")
        if lid_name and phone:
            names.setdefault(phone + "@s.whatsapp.net", lid_name)
    return names, pns


def get_contacts():
    now = time.time()
    try:
        mtime = os.stat(WDB).st_mtime
    except OSError:
        mtime = 0.0
    refresh = int(cfg.get("contacts_refresh_seconds", 900))
    if (now - _contacts["at"] > refresh
            or mtime != _contacts["mtime"]):
        names, pns = load_contacts()
        _contacts.update(at=now, mtime=mtime, names=names, pns=pns)
    return _contacts["names"], _contacts["pns"]


def max_rowid():
    try:
        con = db(DB)
        try:
            return int(con.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def max_reaction_id():
    try:
        con = db(DB)
        try:
            return int(con.execute("SELECT COALESCE(MAX(id), 0) FROM reaction_events").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def fetch_chats(days, include_incoming=False):
    names, pns = get_contacts()
    cut = None
    if days is not None:
        cut = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S+00:00")
    if cut:
        q = (
            "SELECT c.jid, c.name, c.last_message_time FROM chats c "
            "JOIN (SELECT chat_jid, MAX(timestamp) AS ts FROM messages "
            + ("" if include_incoming else "WHERE is_from_me=1 ")
            + "GROUP BY chat_jid) m ON m.chat_jid=c.jid "
            "WHERE c.jid NOT LIKE '%@newsletter' AND c.jid NOT LIKE '%@broadcast' "
            "AND c.jid != 'status@broadcast' AND c.last_message_time >= ? AND m.ts >= ? "
            "ORDER BY c.last_message_time DESC"
        )
        args = (cut, cut)
    else:
        q = (
            "SELECT c.jid, c.name, c.last_message_time FROM chats c "
            "JOIN (SELECT chat_jid FROM messages "
            + ("" if include_incoming else "WHERE is_from_me=1 ")
            + "GROUP BY chat_jid) m "
            "ON m.chat_jid=c.jid "
            "WHERE c.jid NOT LIKE '%@newsletter' AND c.jid NOT LIKE '%@broadcast' "
            "AND c.jid != 'status@broadcast' ORDER BY c.last_message_time DESC"
        )
        args = ()
    out = []
    try:
        con = db(DB)
        try:
            rows = con.execute(q, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.error("chat db error: %s", e)
        return out
    for r in rows:
        title, send_to = chat_title(r["jid"], r["name"], names, pns)
        out.append({"jid": r["jid"], "name": title, "send_to": send_to})
    return out


def fetch_oldest_id(jid):
    try:
        con = db(DB)
        try:
            row = con.execute(
                "SELECT id FROM messages WHERE chat_jid = ? "
                "ORDER BY timestamp ASC, rowid ASC LIMIT 1", (jid,)
            ).fetchone()
            return row["id"] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def fetch_oldest_ts(jid):
    try:
        con = db(DB)
        try:
            row = con.execute(
                "SELECT timestamp FROM messages WHERE chat_jid = ? "
                "ORDER BY timestamp ASC, rowid ASC LIMIT 1", (jid,)
            ).fetchone()
            return parse_ts(row["timestamp"]) if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def fetch_chat_last(jid):
    try:
        con = db(DB)
        try:
            row = con.execute(
                "SELECT last_message_time FROM chats WHERE jid = ?", (jid,)
            ).fetchone()
            return parse_ts(row["last_message_time"]) if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def fetch_chat_name(jid):
    try:
        con = db(DB)
        try:
            row = con.execute("SELECT name FROM chats WHERE jid = ?", (jid,)).fetchone()
            return row["name"] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def fetch_sender(jid, wa_id):
    try:
        con = db(DB)
        try:
            row = con.execute(
                "SELECT sender FROM messages WHERE id = ? AND chat_jid = ?",
                (wa_id, jid),
            ).fetchone()
            return row["sender"] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def fetch_new_messages(cursor, limit=40):
    con = db(DB)
    try:
        return con.execute(
            "SELECT %s FROM messages WHERE rowid > ? ORDER BY rowid ASC LIMIT ?"
            % MSG_COLS,
            (cursor, limit),
        ).fetchall()
    finally:
        con.close()


def fetch_history(chat_jid, days, limit):
    cut = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S+00:00")
    limit = max(1, int(limit))
    con = db(DB)
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND timestamp >= ? "
            "AND deleted = 0",
            (chat_jid, cut),
        ).fetchone()[0]
        rows = list(con.execute(
            "SELECT %s FROM messages WHERE chat_jid = ? AND timestamp >= ? "
            "AND deleted = 0 ORDER BY timestamp DESC, rowid DESC LIMIT ?" % MSG_COLS,
            (chat_jid, cut, limit),
        ).fetchall())
        rows.reverse()
        seen = {r["id"] for r in rows}
        queue = [r["quoted_id"] for r in rows if r["quoted_id"]]
        extra = 0
        while queue and extra < 100:
            qid = queue.pop()
            if not qid or qid in seen:
                continue
            q = con.execute(
                "SELECT %s FROM messages WHERE chat_jid = ? AND id = ? "
                "AND deleted = 0 LIMIT 1" % MSG_COLS,
                (chat_jid, qid),
            ).fetchone()
            if q:
                rows.append(q)
                seen.add(qid)
                extra += 1
                if q["quoted_id"]:
                    queue.append(q["quoted_id"])
    finally:
        con.close()
    rows.sort(key=lambda r: (
        parse_ts(r["timestamp"])
        or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
        r["rowid"],
    ))
    return rows, total


def fetch_reaction_events(cursor, limit=100):
    try:
        con = db(DB)
        try:
            return con.execute(
                "SELECT id, chat_jid, message_id, sender, emoji, is_from_me, timestamp "
                "FROM reaction_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def bridge_headers():
    return {"Authorization": "Bearer " + BRIDGE_TOKEN} if BRIDGE_TOKEN else {}


async def bridge_get(path, timeout=10):
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    try:
        async with session.get(
            BRIDGE + path, headers=bridge_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            data = await r.json(content_type=None)
            return r.status == 200, data, r.status
    except Exception as e:
        global last_error
        last_error = "bridge %s: %s" % (path, e)
        log.error("bridge %s error: %s", path, e)
        return False, {}, 0


def max_event_id():
    try:
        con = db(DB)
        try:
            return int(con.execute(
                "SELECT COALESCE(MAX(id), 0) FROM message_events"
            ).fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def fetch_message_events(cursor, limit=100):
    try:
        con = db(DB)
        try:
            return con.execute(
                "SELECT id, chat_jid, message_id, kind, content, sender, timestamp, "
                "is_from_me FROM message_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.error("message event fetch failed: %s", e)
        return []


def max_receipt_id():
    try:
        con = db(DB)
        try:
            return int(con.execute(
                "SELECT COALESCE(MAX(id), 0) FROM receipt_events"
            ).fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def fetch_receipt_events(cursor, limit=100):
    try:
        con = db(DB)
        try:
            return con.execute(
                "SELECT id, chat_jid, message_id, receipt_type, timestamp "
                "FROM receipt_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.error("receipt event fetch failed: %s", e)
        return []


async def bridge_post(path, payload, timeout=90):
    global session, last_error
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    try:
        async with session.post(
            BRIDGE + path, json=payload, headers=bridge_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                last_error = "bridge %s HTTP %s: %s" % (
                    path, r.status, data.get("message", ""))
            return r.status == 200 and bool(data.get("success")), data
    except Exception as e:
        last_error = "bridge %s: %s" % (path, e)
        log.error("bridge %s error: %s", path, e)
        return False, {}


async def send_wa(recipient, message, media_path=None, quoted_id=None, quoted_sender=None):
    payload = {"recipient": recipient, "message": message}
    if media_path:
        payload["media_path"] = media_path
    if quoted_id:
        payload["quoted_message_id"] = quoted_id
        if quoted_sender:
            payload["quoted_sender"] = quoted_sender
    ok, data = await bridge_post("/api/send", payload)
    return ok, (data.get("message_id") or "" if ok else "")


async def react_wa(chat_jid, wa_id, emoji):
    ok, data = await bridge_post(
        "/api/react", {"chat_jid": chat_jid, "message_id": wa_id, "emoji": emoji}
    )
    if not ok:
        log.error("reaction failed for %s/%s: %s", chat_jid, wa_id, data)
    return ok


async def download_wa(chat_jid, wa_id, max_bytes=None):
    ok, data = await bridge_post(
        "/api/download",
        {"message_id": wa_id, "chat_jid": chat_jid,
         "max_bytes": int(max_bytes or MEDIA_MAX)},
        timeout=180,
    )
    if not ok:
        return None, None
    return data.get("path"), data.get("filename")


async def edit_wa(chat_jid, wa_id, message):
    ok, data = await bridge_post(
        "/api/edit",
        {"chat_jid": chat_jid, "message_id": wa_id, "message": message},
    )
    return ok, data


async def delete_wa(chat_jid, wa_id):
    ok, data = await bridge_post(
        "/api/delete", {"chat_jid": chat_jid, "message_id": wa_id}
    )
    return ok, data


async def read_wa(chat_jid, wa_ids, sender=""):
    if not wa_ids:
        return True, {}
    payload = {"chat_jid": chat_jid, "message_ids": list(wa_ids)}
    if sender:
        payload["sender"] = full_sender(chat_jid, sender)
    return await bridge_post("/api/read", payload)


async def ensure_container(guild):
    cid = cfg.get("forum_channel_id")
    if cid:
        ch = guild.get_channel(int(cid))
        if ch is None:
            try:
                ch = await client.fetch_channel(int(cid))
            except (discord.NotFound, discord.Forbidden):
                ch = None
        if isinstance(ch, (discord.ForumChannel, discord.TextChannel)):
            return ch
    for ch in guild.forums:
        if ch.name.lower() == "whatsapp":
            cfg["forum_channel_id"] = str(ch.id)
            save_json(CFG_PATH, cfg)
            return ch
    for ch in guild.text_channels:
        if ch.name.lower() == "whatsapp":
            cfg["forum_channel_id"] = str(ch.id)
            save_json(CFG_PATH, cfg)
            return ch
    try:
        ch = await guild.create_forum(
            name="WhatsApp", topic="Sinkronisasi chat WhatsApp", reason="WhatsApp bridge"
        )
        log.info("created forum channel %s", ch.id)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("forum creation failed (%s), falling back to text channel", e)
        ch = await guild.create_text_channel(
            "whatsapp", topic="Sinkronisasi chat WhatsApp", reason="WhatsApp bridge"
        )
    cfg["forum_channel_id"] = str(ch.id)
    save_json(CFG_PATH, cfg)
    return ch


def build_overwrites(guild):
    perms = {
        "view_channel", "send_messages", "send_messages_in_threads",
        "create_public_threads", "read_message_history", "attach_files",
        "add_reactions", "embed_links",
    }
    if guild.me.guild_permissions.manage_threads:
        perms.add("manage_threads")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            **{perm: False for perm in perms}
        )
    }
    users = [client.user.id] + [int(x) for x in (cfg.get("allowed_users") or [])]
    owner_id = cfg.get("owner_id")
    if owner_id:
        users.append(int(owner_id))
    for uid in users:
        overwrites[discord.Object(id=uid)] = discord.PermissionOverwrite(
            **{perm: True for perm in perms}
        )
    return overwrites


async def ensure_private(guild, container):
    if not cfg.get("private_category", True):
        return
    try:
        overwrites = build_overwrites(guild)
        await container.edit(overwrites=overwrites, reason="WhatsApp privacy")
        log.info("forum locked to bot + allowed users (%s)", container.id)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("privacy setup failed: %s", e)


async def create_topic(container, chat):
    title = re.sub(r"\s+", " ", chat["name"]).strip()[:100]
    intro = (
        "Terhubung ke WhatsApp: **%s**\n"
        "Ketik pesan di thread ini untuk mengirim ke WhatsApp." % title
    )
    if isinstance(container, discord.ForumChannel):
        res = await container.create_thread(name=title, content=intro, reason="WA sync")
        th = getattr(res, "thread", res)
    else:
        th = await container.create_thread(
            name=title,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
            reason="WA sync",
        )
        await discord_io.send(th, intro)
    mapping["chats"][chat["jid"]] = {
        "thread_id": th.id,
        "name": title,
        "send_to": chat["send_to"],
    }
    by_thread[th.id] = chat["jid"]
    save_json(MAP_PATH, mapping)
    log.info("thread %s -> %s (%s)", th.id, title, chat["jid"])
    return th


async def get_thread(guild, thread_id):
    th = guild.get_thread(int(thread_id))
    if th:
        return th
    try:
        return await client.fetch_channel(int(thread_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def ensure_thread_for(guild, jid, names, pns):
    rec = mapping["chats"].get(jid)
    if rec:
        th = await get_thread(guild, rec["thread_id"])
        if th:
            return th
    async with sync_lock:
        rec = mapping["chats"].get(jid)
        if rec:
            th = await get_thread(guild, rec["thread_id"])
            if th:
                return th
        chat_name = await asyncio.to_thread(fetch_chat_name, jid)
        title, send_to = chat_title(jid, chat_name, names, pns)
        container = await ensure_container(guild)
        th = await create_topic(
            container, {"jid": jid, "name": title, "send_to": send_to}
        )
        await backfill_thread(th, jid, BACKFILL_DAYS, BACKFILL_LIMIT)
        return th


async def reconcile(container):
    existing = set()
    try:
        for th in container.threads:
            existing.add(th.id)
        async for th in container.archived_threads(limit=None):
            existing.add(th.id)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("cannot list threads: %s", e)
        return
    removed = 0
    for jid, rec in list(mapping["chats"].items()):
        if int(rec.get("thread_id", 0)) not in existing:
            by_thread.pop(int(rec.get("thread_id", 0)), None)
            del mapping["chats"][jid]
            removed += 1
    if removed:
        save_json(MAP_PATH, mapping)
        log.info("removed %d stale thread mappings", removed)


async def sync_chats(guild, days, include_incoming=False):
    async with sync_lock:
        container = await ensure_container(guild)
        chats = await asyncio.to_thread(fetch_chats, days, include_incoming)
        created = 0
        for chat in chats:
            if chat["jid"] in mapping["chats"]:
                continue
            if not thread_allowed(
                chat["jid"], cfg.get("thread_policy", "mapped"),
                cfg.get("allowlist", []), is_mapped=False,
            ):
                continue
            try:
                th = await create_topic(container, chat)
                created += 1
                if relay.get_meta("rebuild_v2_done"):
                    await backfill_thread(
                        th, chat["jid"], BACKFILL_DAYS, BACKFILL_LIMIT
                    )
            except Exception as e:
                log.error("thread create failed for %s: %s", chat["jid"], e)
                if isinstance(e, discord.HTTPException) and e.status == 429:
                    await asyncio.sleep(float(getattr(e, "retry_after", 5)))
            await asyncio.sleep(2)
        return created, len(chats)


def search_chats(query):
    names, pns = get_contacts()
    q = query.strip().lower()
    out = []
    try:
        con = db(DB)
        try:
            rows = con.execute(
                "SELECT jid, name FROM chats ORDER BY last_message_time DESC"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return out
    for r in rows:
        if not is_relayable_jid(r["jid"]):
            continue
        title, send_to = chat_title(r["jid"], r["name"], names, pns)
        if q in (title + " " + r["jid"] + " " + send_to).lower():
            out.append({"jid": r["jid"], "name": title, "send_to": send_to})
            if len(out) >= 10:
                break
    return out


def user_allowed(user_id):
    users = [int(x) for x in (cfg.get("allowed_users") or [])]
    if users:
        return int(user_id) in users
    owner = cfg.get("owner_id")
    return owner is not None and int(user_id) == int(owner)


def allowed(interaction):
    return interaction.guild_id == GUILD_ID and user_allowed(interaction.user.id)


def resolve_sender(chat_jid, row, names, pns):
    sender = row["sender"] or ""
    push = row["push_name"] or ""
    if not push:
        cached = relay.sender_name(chat_jid, sender)
        if cached:
            return cached
    name = sender_display(sender, names, pns, push, chat_jid)
    if name:
        relay.set_sender_name(chat_jid, sender, name, "push" if push else "resolver")
    return name


async def post_to_thread(thread, row, names, pns, days=None):
    wa_id = row["id"]
    chat_jid = row["chat_jid"]
    if relay.has_message(wa_id, chat_jid):
        return False
    if row["deleted"]:
        return False
    ts = parse_ts(row["timestamp"]) or datetime.datetime.now(datetime.timezone.utc)
    if days is not None and not within_days(ts, datetime.datetime.now(datetime.timezone.utc), days):
        return False
    if getattr(thread, "archived", False):
        try:
            await discord_io.edit(thread, archived=False)
        except discord.HTTPException:
            pass
    outgoing = bool(row["is_from_me"])
    if not outgoing and not chat_jid.endswith("@g.us"):
        label = (mapping["chats"].get(chat_jid) or {}).get("name", "")
        if row["push_name"]:
            sender_name = resolve_sender(chat_jid, row, names, pns)
        else:
            sender_name = label.split(" · ", 1)[-1] if " · " in label else resolve_sender(
                chat_jid, row, names, pns
            )
    else:
        sender_name = resolve_sender(chat_jid, row, names, pns)
    quote_text = row["quoted_content"]
    target = None
    if row["quoted_id"]:
        mapped = relay.discord_for(row["quoted_id"], chat_jid)
        if (mapped and mapped["discord_id"]
                and int(mapped["thread_id"] or 0) == thread.id):
            target = thread.get_partial_message(int(mapped["discord_id"]))
            quote_text = None
    content = format_mentions(
        row["content"], row["mentioned_jids"], names, pns, chat_jid
    )
    text = format_relay_message(
        sender_name,
        content,
        outgoing=outgoing,
        media_type=row["media_type"] or "",
        quote=quote_text,
    )
    text += "\n-# <t:%d:f>" % int(ts.timestamp())
    chunks = split_message(text)
    if not chunks:
        return False

    file = None
    note = ""
    cache_path = None
    if row["media_type"]:
        ok, reason = media_allowed(
            row["filename"] or "", row["media_type"], row["file_length"] or 0,
            max_bytes=MEDIA_MAX,
        )
        if not ok:
            note = "*[media tidak dikirim: %s]*" % reason
        else:
            async def fetch_media():
                return await download_wa(chat_jid, wa_id, MEDIA_MAX)

            cache_path, cached_name, conv_note = await media_cache.get(
                chat_jid, wa_id, fetch_media, MEDIA_MAX
            )
            if conv_note:
                note = conv_note
            if cache_path and os.path.exists(cache_path):
                media_cache.mark_in_use(cache_path)
                actual = os.path.getsize(cache_path)
                if actual > MEDIA_MAX:
                    note = "*[media tidak dikirim: ukuran %.1f MiB]*" % (
                        actual / 1048576)
                else:
                    file = discord.File(
                        cache_path, filename=safe_filename(cached_name or row["filename"])
                    )
            else:
                note = note or "*[media gagal diunduh]*"
    if note:
        chunks[-1] = chunks[-1] + "\n" + note

    first = None
    try:
        for i, chunk in enumerate(chunks):
            kwargs = {
                "content": chunk,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if i == 0 and file:
                kwargs["file"] = file
            sent = None
            if i == 0 and target is not None:
                try:
                    sent = await discord_io.reply(target, **kwargs)
                except (discord.NotFound, discord.HTTPException):
                    fallback = format_relay_message(
                        sender_name,
                        content,
                        outgoing=outgoing,
                        media_type=row["media_type"] or "",
                        quote=row["quoted_content"],
                    ) + "\n-# <t:%d:f>" % int(ts.timestamp())
                    fb = split_message(fallback)
                    if fb:
                        chunks = fb
                        kwargs["content"] = fb[0]
                    sent = await discord_io.send(thread, **kwargs)
            else:
                sent = await discord_io.send(thread, **kwargs)
            if first is None:
                first = sent
    finally:
        if cache_path:
            media_cache.release(cache_path)

    if first is not None:
        relay.map_message(
            wa_id, chat_jid, thread.id, first.id,
            "out" if outgoing else "in", ts.isoformat(),
            author_id=client.user.id,
        )
        if (not outgoing and cfg.get("mark_read_on_relay", False)):
            try:
                await read_wa(chat_jid, [wa_id], row["sender"])
            except Exception as e:
                log.warning("mark read failed for %s/%s: %s", chat_jid, wa_id, e)
    return True

def history_summary(days, total, limit):
    return (
        "**History %d hari:** %d pesan. Menampilkan %d terakhir + pesan yang dikutip."
        % (int(days), int(total), int(limit))
    )


async def preserved_user_mappings(thread, jid):
    preserved = []
    try:
        recs = relay.mappings_for_chat(jid)
    except sqlite3.Error:
        return None
    for r in recs:
        if r["direction"] != "out" or not r["discord_id"]:
            continue
        if int(r["thread_id"] or 0) != thread.id:
            continue
        try:
            msg = await thread.fetch_message(int(r["discord_id"]))
        except (discord.NotFound, discord.Forbidden):
            continue
        except discord.HTTPException:
            continue
        if msg.author.id != client.user.id:
            preserved.append(dict(r))
    return preserved


def restore_mappings(preserved, jid):
    for p in preserved:
        relay.map_message(
            p["wa_id"], jid, p["thread_id"], p["discord_id"],
            p["direction"], p["ts"], author_id=p["author_id"],
        )


async def backfill_thread(thread, jid, days=BACKFILL_DAYS, limit=BACKFILL_LIMIT):
    names, pns = get_contacts()
    if not is_relayable_jid(jid):
        return 0
    try:
        rows, total = await asyncio.to_thread(fetch_history, jid, days, limit)
    except sqlite3.Error as e:
        log.error("backfill query failed for %s: %s", jid, e)
        return 0
    if not rows:
        return 0
    if total > limit:
        try:
            await discord_io.send(thread, history_summary(days, total, limit))
        except discord.HTTPException:
            pass
    posted = 0
    for row in rows:
        try:
            if await post_to_thread(thread, row, names, pns, days=None):
                posted += 1
        except Exception as e:
            log.error("backfill post failed for %s: %s", jid, e)
        await asyncio.sleep(0.35)
    if posted:
        log.info("backfilled %d messages for %s", posted, jid)
    return posted


async def rebuild_thread(thread, jid, days=BACKFILL_DAYS, limit=BACKFILL_LIMIT):
    names, pns = get_contacts()
    try:
        rows, total = await asyncio.to_thread(fetch_history, jid, days, limit)
    except sqlite3.Error as e:
        log.error("rebuild query failed for %s: %s", jid, e)
        return 0
    if not rows:
        return 0
    rebuilding_jids.add(jid)
    posted = 0
    try:
        preserved = await preserved_user_mappings(thread, jid)
        if preserved is None:
            return -1
        intro_kept = False
        try:
            async for msg in thread.history(limit=None, oldest_first=True):
                if msg.author.id != client.user.id:
                    continue
                content = msg.content or ""
                if not intro_kept and content.startswith("Terhubung ke WhatsApp:"):
                    intro_kept = True
                    continue
                try:
                    await discord_io.delete(msg)
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    log.warning("cannot delete %s in %s: %s", msg.id, thread.id, e)
                    return -1
                await asyncio.sleep(0.15)
        except discord.HTTPException as e:
            log.warning("cannot read history for %s: %s", thread.id, e)
            return -1
        relay.clear_chat(jid)
        restore_mappings(preserved, jid)
        if not intro_kept:
            title = (mapping["chats"].get(jid) or {}).get("name", jid)
            try:
                await discord_io.send(thread, 
                    "Terhubung ke WhatsApp: **%s**\n"
                    "Ketik pesan di thread ini untuk mengirim ke WhatsApp." % title
                )
            except discord.HTTPException:
                pass
        if total > limit:
            try:
                await discord_io.send(thread, history_summary(days, total, limit))
            except discord.HTTPException:
                pass
        for row in rows:
            try:
                if await post_to_thread(thread, row, names, pns, days=None):
                    posted += 1
            except Exception as e:
                log.error("rebuild post failed for %s/%s: %s", jid, row["id"], e)
            await asyncio.sleep(0.35)
        log.info("rebuilt %d messages for %s (total %d in window)", posted, jid, total)
    finally:
        rebuilding_jids.discard(jid)
    return posted


async def rebuild_all(guild, days=BACKFILL_DAYS, limit=BACKFILL_LIMIT, manual=False):
    jobs = []
    for jid, rec in list(mapping["chats"].items()):
        if not manual and relay.get_meta("rebuild_v2:" + jid):
            continue
        jobs.append((jid, rec))
    sem = asyncio.Semaphore(3)
    counter = [0]
    failures = [0]

    async def one(jid, rec):
        async with sem:
            thread = await get_thread(guild, rec["thread_id"])
            if thread is None:
                return
            try:
                rows, _ = await asyncio.to_thread(fetch_history, jid, days, limit)
            except sqlite3.Error as e:
                log.error("rebuild history failed for %s: %s", jid, e)
                failures[0] += 1
                return
            if not rows:
                relay.set_meta("rebuild_v2:" + jid, "1")
                return
            try:
                result = await rebuild_thread(thread, jid, days, limit)
            except Exception as e:
                log.error("rebuild failed for %s: %s", jid, e)
                failures[0] += 1
                return
            if result == -1:
                log.error("rebuild aborted for %s (safe state, not marked done)", jid)
                failures[0] += 1
                return
            relay.set_meta("rebuild_v2:" + jid, "1")
            counter[0] += 1

    await asyncio.gather(*(one(jid, rec) for jid, rec in jobs))
    if failures[0]:
        log.error("rebuild finished with %d failed chat(s)", failures[0])
    else:
        relay.set_meta("rebuild_v2_done", "1")
    log.info("rebuild done: %d chats (%d days, limit %d)", counter[0], days, limit)


async def repair_history(thread, jid, pages=10):
    """Page /api/history backwards, then rebuild the thread once."""
    before = await asyncio.to_thread(fetch_oldest_id, jid)
    for page in range(max(1, int(pages))):
        oldest_ts = await asyncio.to_thread(fetch_oldest_ts, jid)
        if oldest_ts is None or not within_days(
            oldest_ts, datetime.datetime.now(datetime.timezone.utc), 30
        ):
            break
        ok, data = await bridge_post(
            "/api/history",
            {"chat_jid": jid, "before_id": before, "count": 100},
            timeout=120,
        )
        if not ok:
            log.warning("history page %d failed for %s: %s", page + 1, jid, data)
            break
        await asyncio.sleep(2.0)
        oldest = await asyncio.to_thread(fetch_oldest_id, jid)
        if not oldest or oldest == before:
            break
        before = oldest
    return await rebuild_thread(thread, jid, BACKFILL_DAYS, BACKFILL_LIMIT)


@tasks.loop(seconds=3)
async def relay_watcher():
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    cursor = int(relay.get_meta("wa_cursor", 0))
    try:
        rows = await asyncio.to_thread(fetch_new_messages, cursor, 40)
    except sqlite3.Error as e:
        log.warning("message poll failed: %s", e)
        return
    if not rows:
        return
    names, pns = get_contacts()
    rows.sort(key=lambda r: (parse_ts(r["timestamp"]) or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc), r["rowid"]))
    for row in rows:
        jid = row["chat_jid"]
        if jid in rebuilding_jids:
            break
        allowed_chat = thread_allowed(
            jid,
            cfg.get("thread_policy", "mapped"),
            cfg.get("allowlist", []),
            is_mapped=jid in mapping["chats"],
        )
        if is_relayable_jid(jid) and allowed_chat:
            try:
                thread = await ensure_thread_for(guild, jid, names, pns)
                if thread:
                    await post_to_thread(
                        thread, row, names, pns,
                        days=int(cfg.get("history_days", 7)),
                    )
            except Exception as e:
                global last_error
                last_error = "relay %s: %s" % (jid, e)
                log.error("relay failed for %s/%s: %s", jid, row["id"], e)
                break
        relay.set_meta("wa_cursor", row["rowid"])


@tasks.loop(seconds=3)
async def message_event_watcher():
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    cursor = int(relay.get_meta("message_event_cursor", 0))
    events = await asyncio.to_thread(fetch_message_events, cursor, 100)
    for event in events:
        cursor = event["id"]
        mapped = relay.discord_for(event["message_id"], event["chat_jid"])
        if not mapped or not mapped["discord_id"]:
            relay.set_meta("message_event_cursor", cursor)
            continue
        thread = await get_thread(guild, mapped["thread_id"])
        if thread is None:
            relay.set_meta("message_event_cursor", cursor)
            continue
        try:
            full = await discord_io.call(
                lambda: thread.fetch_message(int(mapped["discord_id"]))
            )
            if full.author.id != client.user.id:
                # A Discord->WA user message; WhatsApp echo/edit must not
                # rewrite the user's own Discord message.
                relay.set_meta("message_event_cursor", cursor)
                continue
            if event["kind"] == "delete" and cfg.get("mirror_deletes", True):
                await discord_io.delete(full)
                relay.remove_message(event["message_id"], event["chat_jid"])
            elif event["kind"] == "edit" and cfg.get("mirror_edits", True):
                body = format_edit_event(event["content"], True)
                await discord_io.edit(
                    full, content=body,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.NotFound:
            relay.remove_message(event["message_id"], event["chat_jid"])
            relay.set_meta("message_event_cursor", cursor)
            continue
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("message event %s failed: %s", event["kind"], e)
            break
        relay.set_meta("message_event_cursor", cursor)


@tasks.loop(seconds=5)
async def receipt_watcher():
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    cursor = int(relay.get_meta("receipt_cursor", 0))
    events = await asyncio.to_thread(fetch_receipt_events, cursor, 100)
    for event in events:
        cursor = event["id"]
        if str(event["receipt_type"]) != "read":
            relay.set_meta("receipt_cursor", cursor)
            continue
        if relay.receipt_seen(event["chat_jid"], event["message_id"]):
            relay.set_meta("receipt_cursor", cursor)
            continue
        mapped = relay.discord_for(event["message_id"], event["chat_jid"])
        if mapped and mapped["discord_id"]:
            thread = await get_thread(guild, mapped["thread_id"])
            if thread is not None:
                msg = thread.get_partial_message(int(mapped["discord_id"]))
                try:
                    await discord_io.add_reaction(msg, "✅")
                    relay.mark_receipt(event["chat_jid"], event["message_id"])
                except discord.NotFound:
                    relay.set_meta("receipt_cursor", cursor)
                    continue
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("receipt mirror failed: %s", e)
                    break
        relay.set_meta("receipt_cursor", cursor)


@tasks.loop(seconds=3)
async def reaction_watcher():
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    cursor = int(relay.get_meta("reaction_cursor", 0))
    events = await asyncio.to_thread(fetch_reaction_events, cursor, 100)
    for event in events:
        cursor = event["id"]
        if not should_mirror_reaction(event["is_from_me"]):
            relay.set_meta("reaction_cursor", cursor)
            continue
        emoji = event["emoji"] or ""
        if emoji and not reaction_emoji_ok(emoji):
            relay.set_meta("reaction_cursor", cursor)
            continue
        mapped = relay.discord_for(event["message_id"], event["chat_jid"])
        if not mapped or not mapped["discord_id"]:
            relay.set_meta("reaction_cursor", cursor)
            continue
        thread = await get_thread(guild, mapped["thread_id"])
        if thread is None:
            relay.set_meta("reaction_cursor", cursor)
            continue
        msg = thread.get_partial_message(int(mapped["discord_id"]))
        sender = event["sender"] or ""
        old = relay.reaction_emoji_of(event["chat_jid"], event["message_id"], sender)
        try:
            if not emoji:
                if old:
                    relay.remove_reaction(event["chat_jid"], event["message_id"], sender)
                    if relay.reaction_count(
                        event["chat_jid"], event["message_id"], old
                    ) == 0:
                        await discord_io.remove_reaction(msg, old, client.user)
            elif old != emoji:
                if old:
                    relay.remove_reaction(event["chat_jid"], event["message_id"], sender)
                    if relay.reaction_count(
                        event["chat_jid"], event["message_id"], old
                    ) == 0:
                        await discord_io.remove_reaction(msg, old, client.user)
                relay.add_reaction(
                    event["chat_jid"], event["message_id"], sender, emoji
                )
                if relay.reaction_count(
                    event["chat_jid"], event["message_id"], emoji
                ) == 1:
                    await discord_io.add_reaction(msg, emoji)
        except discord.NotFound:
            relay.set_meta("reaction_cursor", cursor)
            continue
        except discord.HTTPException as e:
            log.warning("mirror reaction failed: %s", e)
            break
        relay.set_meta("reaction_cursor", cursor)


async def handle_discord_reaction(payload, remove=False):
    if payload.guild_id != GUILD_ID:
        return
    if client.user and payload.user_id == client.user.id:
        return
    if payload.channel_id not in by_thread:
        return
    if not user_allowed(payload.user_id):
        return
    mapped = relay.wa_for_discord(payload.message_id)
    if not mapped:
        return
    if remove:
        emoji = ""
    elif payload.emoji.is_custom_emoji():
        emoji = payload.emoji.name
    else:
        emoji = str(payload.emoji)
    await react_wa(mapped["chat_jid"], mapped["wa_id"], emoji)


async def startup_rebuild(guild):
    if relay.get_meta("rebuild_v2_done"):
        return
    await rebuild_all(guild, BACKFILL_DAYS, BACKFILL_LIMIT)


@tree.command(name="sync", description="Buat thread untuk chat WhatsApp", guild=GUILD)
@app_commands.describe(days="Rentang chat yang disinkronkan")
@app_commands.choices(days=[
    app_commands.Choice(name="7 hari", value="7"),
    app_commands.Choice(name="30 hari", value="30"),
    app_commands.Choice(name="semua", value="all"),
])
async def sync_cmd(interaction: discord.Interaction, days: app_commands.Choice[str]):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    created, total = await sync_chats(
        interaction.guild, None if days.value == "all" else int(days.value),
        include_incoming=True,
    )
    await interaction.followup.send(
        "Selesai: %d thread baru dari %d chat." % (created, total), ephemeral=True
    )


@tree.command(name="open", description="Buat thread untuk satu chat tertentu", guild=GUILD)
@app_commands.describe(query="Nama, nomor, atau JID chat")
async def open_cmd(interaction: discord.Interaction, query: str):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    matches = await asyncio.to_thread(search_chats, query)
    if not matches:
        await interaction.followup.send("Chat tidak ditemukan.", ephemeral=True)
        return
    if len(matches) > 1:
        lines = ["%d. %s" % (i + 1, m["name"]) for i, m in enumerate(matches[:10])]
        await interaction.followup.send(
            "Beberapa chat cocok:\n" + "\n".join(lines) + "\nPakai query lebih spesifik.",
            ephemeral=True,
        )
        return
    chat = matches[0]
    if chat["jid"] in mapping["chats"]:
        await interaction.followup.send("Thread sudah ada.", ephemeral=True)
        return
    container = await ensure_container(interaction.guild)
    try:
        thread = await create_topic(container, chat)
        await backfill_thread(
            thread, chat["jid"], BACKFILL_DAYS, BACKFILL_LIMIT
        )
        await interaction.followup.send("Thread dibuat: **%s**" % chat["name"], ephemeral=True)
    except discord.HTTPException as e:
        await interaction.followup.send("Gagal: %s" % e, ephemeral=True)


@tree.command(name="backfill", description="Rebuild history chat (hapus bot lama, post ulang urut)", guild=GUILD)
@app_commands.describe(days="Rentang history", limit="Maks pesan per chat")
@app_commands.choices(days=[
    app_commands.Choice(name="7 hari", value="7"),
    app_commands.Choice(name="30 hari", value="30"),
])
async def backfill_cmd(
    interaction: discord.Interaction,
    days: app_commands.Choice[str],
    limit: int = 20,
):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    limit = max(1, min(limit, 200))
    asyncio.create_task(run_rebuild(guild, int(days.value), limit))
    await interaction.followup.send(
        "Rebuild %s hari (maks %d/chat) dimulai di background. "
        "Pesan bot lama dihapus, history dipost ulang urut." % (days.value, limit),
        ephemeral=True,
    )


async def run_rebuild(guild, days, limit):
    await rebuild_all(guild, days, limit, manual=True)


@tree.command(name="private", description="Kunci forum hanya untuk owner + bot", guild=GUILD)
async def private_cmd(interaction: discord.Interaction):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    container = await ensure_container(interaction.guild)
    await ensure_private(interaction.guild, container)
    await interaction.followup.send("Permission forum diperbarui.", ephemeral=True)


@tree.command(name="status", description="Status bridge Discord-WhatsApp", guild=GUILD)
async def status_cmd(interaction: discord.Interaction):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    t0 = time.monotonic()
    ok, health, status = await bridge_get("/api/health", timeout=4)
    _, stats, _ = await bridge_get("/api/status", timeout=4)
    health = dict(health or {})
    health.update(stats or {})
    latency = (time.monotonic() - t0) * 1000
    lag = max_rowid() - int(relay.get_meta("wa_cursor", 0))
    receipt_lag = max_receipt_id() - int(relay.get_meta("receipt_cursor", 0))
    cache = media_cache.stats()
    contacts_age = int(time.time() - _contacts["at"]) if _contacts["at"] else -1
    lines = [
        "Thread: **%d** · Forum: <#%s>" % (
            len(mapping["chats"]), cfg.get("forum_channel_id")),
        "Bridge: **%s** (HTTP %s, %.0f ms)" % (
            "OK" if ok else "DOWN", status, latency),
        "Relay lag: **%d** · event lag: **%d**" % (
            lag, max_event_id() - int(relay.get_meta("message_event_cursor", 0))),
        "Rebuilding: **%d** · pending receipts: **%d**" % (
            len(rebuilding_jids), max(receipt_lag, 0)),
        "DB: %.1f MiB · WAL: %.1f MiB" % (
            (health.get("db_bytes", 0) or 0) / 1048576,
            (health.get("wal_bytes", 0) or 0) / 1048576),
        "Media cache: **%d** files / %.1f MiB" % (
            cache["files"], cache["bytes"] / 1048576),
        "Contacts cache: %ss · names: %d" % (
            "never" if contacts_age < 0 else contacts_age,
            relay.sender_name_stats()),
        "Discord latency: %.0f ms · last error: %s" % (
            client.latency * 1000, last_error or "-"),
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="allow", description="Izinkan grup masuk ke Discord", guild=GUILD)
@app_commands.describe(query="Nama grup atau JID")
async def allow_cmd(interaction: discord.Interaction, query: str):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    matches = [m for m in await asyncio.to_thread(search_chats, query)
               if m["jid"].endswith("@g.us")]
    if not matches:
        await interaction.followup.send("Grup tidak ditemukan.", ephemeral=True)
        return
    chat = matches[0]
    allow = list(cfg.get("allowlist") or [])
    if chat["jid"] not in allow:
        allow.append(chat["jid"])
    cfg["allowlist"] = allow
    save_json(CFG_PATH, cfg)
    if chat["jid"] not in mapping["chats"]:
        container = await ensure_container(interaction.guild)
        thread = await create_topic(container, chat)
        await backfill_thread(thread, chat["jid"], BACKFILL_DAYS, BACKFILL_LIMIT)
    await interaction.followup.send(
        "Diizinkan: **%s**" % chat["name"], ephemeral=True)


@tree.command(name="deny", description="Cabut izin grup", guild=GUILD)
@app_commands.describe(query="Nama grup atau JID")
async def deny_cmd(interaction: discord.Interaction, query: str):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    matches = [m for m in await asyncio.to_thread(search_chats, query)
               if m["jid"].endswith("@g.us")]
    if not matches:
        await interaction.followup.send("Grup tidak ditemukan.", ephemeral=True)
        return
    jid = matches[0]["jid"]
    cfg["allowlist"] = [j for j in (cfg.get("allowlist") or []) if j != jid]
    save_json(CFG_PATH, cfg)
    await interaction.followup.send(
        "Izin dicabut: **%s** (thread lama tetap ada)." % matches[0]["name"],
        ephemeral=True)


@tree.command(name="repair", description="Tarik history WhatsApp lalu rebuild thread", guild=GUILD)
@app_commands.describe(query="Nama, nomor, atau JID chat")
async def repair_cmd(interaction: discord.Interaction, query: str):
    if not allowed(interaction):
        await interaction.response.send_message("Tidak diizinkan.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    matches = await asyncio.to_thread(search_chats, query)
    if not matches:
        await interaction.followup.send("Chat tidak ditemukan.", ephemeral=True)
        return
    chat = matches[0]
    rec = mapping["chats"].get(chat["jid"])
    if not rec:
        await interaction.followup.send("Thread belum ada; pakai /open dulu.", ephemeral=True)
        return
    thread = await get_thread(interaction.guild, rec["thread_id"])
    if thread is None:
        await interaction.followup.send("Thread tidak ditemukan.", ephemeral=True)
        return
    await interaction.followup.send(
        "Repair **%s** dimulai (maks 10 halaman history)." % chat["name"],
        ephemeral=True,
    )
    try:
        posted = await repair_history(thread, chat["jid"])
        if posted == -1:
            await interaction.followup.send(
                "Repair dibatalkan karena thread/DB tidak bisa dibaca; "
                "tidak ada pesan yang dihapus.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Repair selesai: %d pesan dipost ulang." % posted, ephemeral=True)
    except Exception as e:
        await interaction.followup.send("Repair gagal: %s" % e, ephemeral=True)


@tasks.loop(hours=6)
async def cleanup():
    try:
        removed = media_cache.cleanup()
        if removed:
            log.info("media cache cleanup removed %s files", removed)
        relay.prune_sender_names(180)
    except Exception as e:
        log.warning("media cleanup failed: %s", e)
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    days = int(cfg.get("archive_after_days", 30) or 0)
    if days <= 0:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    for jid, rec in list(mapping["chats"].items()):
        last = await asyncio.to_thread(fetch_chat_last, jid)
        if last is None or not is_idle(last, now, days):
            continue
        thread = await get_thread(guild, rec["thread_id"])
        if thread is None or getattr(thread, "archived", False):
            continue
        try:
            await discord_io.edit(thread, archived=True)
            log.info("archived idle thread %s (%s)", thread.id, jid)
        except (discord.Forbidden, discord.HTTPException) as e:
            if not relay.get_meta("archive_warned"):
                relay.set_meta("archive_warned", "1")
                log.warning("cannot archive threads (missing Manage Threads): %s", e)


@tasks.loop(seconds=120)
async def watcher():
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        created, _ = await sync_chats(guild, 2)
        if created:
            log.info("watcher created %d new threads", created)
    except Exception as e:
        log.error("watcher error: %s", e)


@client.event
async def on_ready():
    global started
    log.info("logged in as %s (guild %s)", client.user, GUILD_ID)
    try:
        open(ENABLED, "w").write(datetime.datetime.now(datetime.timezone.utc).isoformat())
    except OSError:
        pass
    if started:
        return
    started = True
    guild = client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)
    if not cfg.get("owner_id") and guild.owner_id:
        cfg["owner_id"] = str(guild.owner_id)
        save_json(CFG_PATH, cfg)
    try:
        container = await ensure_container(guild)
        await reconcile(container)
        await ensure_private(guild, container)
    except Exception as e:
        log.error("setup error: %s", e)
    if relay.get_meta("wa_cursor") is None:
        relay.set_meta("wa_cursor", max_rowid())
    if relay.get_meta("reaction_cursor") is None:
        relay.set_meta("reaction_cursor", max_reaction_id())
    if relay.get_meta("message_event_cursor") is None:
        relay.set_meta("message_event_cursor", max_event_id())
    if relay.get_meta("receipt_cursor") is None:
        relay.set_meta("receipt_cursor", max_receipt_id())
    try:
        await tree.sync(guild=GUILD)
        log.info("slash commands synced")
    except Exception as e:
        log.error("command sync failed: %s", e)
    watcher.start()
    relay_watcher.start()
    reaction_watcher.start()
    message_event_watcher.start()
    receipt_watcher.start()
    cleanup.start()
    try:
        created, total = await sync_chats(guild, int(cfg.get("sync_days", 30)))
        log.info("initial sync: %d new / %d chats", created, total)
    except Exception as e:
        log.error("initial sync error: %s", e)
    asyncio.create_task(startup_rebuild(guild))


@client.event
async def on_message(msg):
    if msg.author.bot or msg.guild is None or msg.guild.id != GUILD_ID:
        return
    jid = by_thread.get(msg.channel.id)
    if not jid:
        return
    if not user_allowed(msg.author.id):
        return
    rec = mapping["chats"].get(jid)
    if not rec:
        return
    text = (msg.content or "").strip()
    if not text and not msg.attachments:
        return

    if msg.attachments:
        ok, reason, _ = validate_attachments(
            [(a.filename, a.size) for a in msg.attachments],
            max_total=MAX_DISCORD_TOTAL,
        )
        if not ok:
            try:
                await discord_io.add_reaction(msg, "❌")
                await discord_io.reply(msg, reason, mention_author=False)
            except discord.HTTPException:
                pass
            return

    quoted_id = None
    quoted_sender = None
    if msg.reference and msg.reference.message_id:
        ref = relay.wa_for_discord(msg.reference.message_id)
        if ref:
            quoted_id = ref["wa_id"]
            sender = await asyncio.to_thread(
                fetch_sender, ref["chat_jid"], ref["wa_id"]
            )
            quoted_sender = full_sender(ref["chat_jid"], sender)

    ok = True
    if msg.attachments:
        for i, att in enumerate(msg.attachments):
            name = safe_filename(att.filename or "file.bin")
            path = os.path.join("/tmp", "wa_%d_%s" % (att.id, name))
            try:
                data = await att.read()
                with open(path, "wb") as f:
                    f.write(data)
                sent, mid = await send_wa(
                    rec["send_to"],
                    text if i == 0 else "",
                    path,
                    quoted_id if i == 0 else None,
                    quoted_sender if i == 0 else None,
                )
                ok = sent and ok
                if mid:
                    relay.map_message(
                        mid, jid, rec["thread_id"], msg.id, "out",
                        datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        author_id=msg.author.id,
                    )
            except Exception as e:
                log.error("attachment failed: %s", e)
                ok = False
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    else:
        sent, mid = await send_wa(
            rec["send_to"], text, None, quoted_id, quoted_sender
        )
        ok = sent
        if mid:
            relay.map_message(
                mid, jid, rec["thread_id"], msg.id, "out",
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                author_id=msg.author.id,
            )
    try:
        await discord_io.add_reaction(msg, "✅" if ok else "❌")
    except discord.HTTPException:
        pass


@client.event
async def on_raw_message_edit(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id not in by_thread:
        return
    mapped = relay.wa_for_discord(payload.message_id)
    if not mapped or mapped["direction"] != "out":
        return
    channel = client.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(payload.channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    try:
        msg = payload.cached_message or await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    if msg.author.id == client.user.id or not user_allowed(msg.author.id):
        return
    content = (payload.data.get("content") or msg.content or "").strip()
    if not content:
        return
    ok, _ = await edit_wa(mapped["chat_jid"], mapped["wa_id"], content)
    try:
        await discord_io.add_reaction(msg, "✅" if ok else "❌")
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


@client.event
async def on_raw_message_delete(payload):
    if payload.guild_id != GUILD_ID or payload.channel_id not in by_thread:
        return
    mapped = relay.wa_for_discord(payload.message_id)
    if not mapped or mapped["direction"] != "out":
        return
    cached = payload.cached_message
    author_id = cached.author.id if cached is not None else (
        int(mapped["author_id"]) if mapped["author_id"] else None
    )
    if not author_id or author_id == client.user.id or not user_allowed(author_id):
        return
    ok, _ = await delete_wa(mapped["chat_jid"], mapped["wa_id"])
    if ok:
        relay.remove_message(mapped["wa_id"], mapped["chat_jid"])


@client.event
async def on_raw_reaction_add(payload):
    await handle_discord_reaction(payload, remove=False)


@client.event
async def on_raw_reaction_remove(payload):
    await handle_discord_reaction(payload, remove=True)


if __name__ == "__main__":
    token = cfg.get("token", "")
    if not token:
        log.error("discord_config.json has no token")
        sys.exit(1)
    try:
        client.run(token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        log.error("Message Content Intent belum diaktifkan di Developer Portal")
        try:
            os.remove(ENABLED)
        except OSError:
            pass
        sys.exit(2)
