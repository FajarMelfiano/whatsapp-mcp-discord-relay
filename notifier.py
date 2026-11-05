#!/usr/bin/env python3
"""Smart WhatsApp DM notifier: VIP alerts, digests, AI summaries/drafts,
action reminders and a morning briefing."""

import datetime
import json
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.request
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
DB = BASE + "/whatsapp-bridge/store/messages.db"
WDB = BASE + "/whatsapp-bridge/store/whatsapp.db"
STATE = BASE + "/notifier_state.json"
CONFIG = BASE + "/notifier_config.json"
DRAFTS = BASE + "/drafts.json"
LOG = BASE + "/notifier.log"
PHONE_MCP = os.environ.get("PHONE_MCP_URL", "http://127.0.0.1:4097/mcp")
PHONE_TOKEN = os.environ.get("PHONE_MCP_TOKEN", "").strip()

EXCLUDED = ("@g.us", "@newsletter", "@broadcast")
MEDIA_LABEL = {"image": "Gambar", "video": "Video", "audio": "Audio",
               "ptt": "Voice note", "document": "Dokumen", "sticker": "Stiker", "gif": "GIF"}

DEFAULTS = {
    "vip": [],
    "quiet_hours": ["23:00", "07:00"],
    "quiet_hours_allow_vip": True,
    "digest_minutes": 45,
    "briefing_time": "08:00",
    "ai": True,
    "ai_model": "gpt-4o-mini",
    "debounce_seconds": 12,
    "auto_reminder": True,
    "max_fresh_minutes": 20,
    "utc_offset_hours": 7,
}

running = True
cfg = dict(DEFAULTS)
cfg_mtime = 0
pending = {}
digest = []
last_digest = 0.0
last_briefing = ""
name_cache = {}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def stop(*_):
    global running
    running = False


@contextmanager
def db(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def load_config(force=False):
    global cfg, cfg_mtime
    try:
        m = os.path.getmtime(CONFIG)
    except OSError:
        m = 0
    if force or m != cfg_mtime:
        try:
            with open(CONFIG) as f:
                data = json.load(f)
            cfg = dict(DEFAULTS)
            cfg.update(data or {})
            cfg_mtime = m
            name_cache.clear()
            log("config loaded")
        except (OSError, ValueError) as e:
            log("config error: %s" % e)


def local_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=cfg["utc_offset_hours"])


def in_quiet_hours():
    try:
        start, end = cfg["quiet_hours"]
        now = local_now().strftime("%H:%M")
        return start <= now or now < end if start > end else start <= now < end
    except (ValueError, TypeError):
        return False


def resolve(chat_jid):
    """Return dict with display name, phone, muted."""
    if chat_jid in name_cache:
        return name_cache[chat_jid]
    info = {"jid": chat_jid, "name": chat_jid.split("@")[0], "phone": "", "muted": False}
    try:
        with db(WDB) as con:
            if chat_jid.endswith("@lid"):
                lid = chat_jid.split("@")[0]
                row = con.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid=?", (lid,)).fetchone()
                if row and row["pn"]:
                    info["phone"] = row["pn"]
            row = con.execute(
                "SELECT push_name, full_name, business_name FROM whatsmeow_contacts WHERE their_jid=? LIMIT 1",
                (chat_jid,),
            ).fetchone()
            if row:
                info["name"] = row["full_name"] or row["push_name"] or row["business_name"] or info["name"]
            row = con.execute(
                "SELECT MAX(muted_until) AS m FROM whatsmeow_chat_settings WHERE chat_jid=?", (chat_jid,)
            ).fetchone()
            if row and row["m"] and row["m"] > time.time():
                info["muted"] = True
    except sqlite3.Error:
        pass
    if (not info["name"] or info["name"] == chat_jid.split("@")[0]
            or info["name"].strip() in (".", "-", "~", "...") or len(info["name"].strip()) < 2):
        info["name"] = info["phone"] or info["name"]
    name_cache[chat_jid] = info
    return info


def is_vip(info):
    vip = [str(v).strip().lower() for v in cfg.get("vip") or [] if str(v).strip()]
    if not vip:
        return True
    hay = " ".join([info["jid"], info["name"], info["phone"]]).lower()
    return any(v in hay for v in vip)


def is_private(jid):
    return not any(jid.endswith(s) for s in EXCLUDED)


def fresh(ts):
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return now - datetime.timedelta(minutes=cfg["max_fresh_minutes"]) <= dt <= now + datetime.timedelta(minutes=2)


def clean(text, limit=180):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] + "..." if len(text) > limit else text


def media_text(row):
    media = (row["media_type"] or "").strip()
    if media:
        return "[%s] %s" % (MEDIA_LABEL.get(media, media.capitalize()), clean(row["content"], 80))
    return clean(row["content"])


def phone_call(tool, args, timeout=12):
    if not PHONE_TOKEN:
        log("PHONE_MCP_TOKEN is not set; skipping %s" % tool)
        return b""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(PHONE_MCP, data=body, method="POST", headers={
        "Authorization": "Bearer " + PHONE_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def notify(title, text):
    phone_call("notify", {"title": title, "text": text})


def remind(text, at):
    phone_call("remind", {"text": text, "at": at})


def ai_token():
    token = os.environ.get("COPILOT_TOKEN", "").strip()
    if token:
        return token
    # Optional compatibility with an existing OpenCode config. This is only
    # read when COPILOT_TOKEN is not set; no token is stored in this repo.
    try:
        path = os.path.expanduser("~/.config/opencode/opencode.jsonc")
        with open(path) as f:
            m = re.search(r"gho_[A-Za-z0-9]+", f.read())
            return m.group(0) if m else None
    except OSError:
        return None


AI_TOKEN = ai_token()


def ai_json(system, user):
    if not cfg.get("ai") or not AI_TOKEN:
        return None
    payload = json.dumps({
        "model": cfg.get("ai_model", "gpt-4o-mini"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 400,
    }).encode()
    req = urllib.request.Request("https://api.githubcopilot.com/chat/completions", data=payload, headers={
        "Authorization": "Bearer " + AI_TOKEN,
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Version": "vscode/1.107.0",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    content = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    return json.loads(m.group(0)) if m else None


def recent_context(chat_jid, limit=8):
    try:
        with db(DB) as con:
            rows = con.execute(
                "SELECT content, is_from_me, media_type FROM messages WHERE chat_jid=? "
                "ORDER BY rowid DESC LIMIT ?", (chat_jid, limit)).fetchall()
    except sqlite3.Error:
        return ""
    lines = []
    for r in reversed(rows):
        who = "Aku" if r["is_from_me"] else "Dia"
        txt = media_text(r) if r["media_type"] else clean(r["content"], 120)
        if txt:
            lines.append("%s: %s" % (who, txt))
    return "\n".join(lines)


def analyze(name, chat_jid, incoming):
    sys_prompt = (
        "Kamu asisten WhatsApp pribadi. Jawab HANYA JSON valid, tanpa markdown, dengan skema: "
        '{"ringkas":"<=90 karakter, bahasa Indonesia santai, inti pesan",'
        '"draft":"balasan singkat 1 kalimat gaya santai, tanpa tanda kutip",'
        '"butuh_balasan":true/false,"penting":true/false,'
        '"reminder":{"teks":"<=60 karakter","jam":"HH:MM" atau null} atau null}. '
        "Penting hanya jika benar-benar mendesak: deadline dekat, uang, darurat, atau harus segera. "
        "Pertanyaan/obrolan biasa bukan penting. Reminder hanya jika ada janji atau waktu jelas."
    )
    user = "Nama pengirim: %s\nPercakapan terakhir:\n%s\n\nPesan baru:\n%s" % (
        name, recent_context(chat_jid) or "(belum ada)", incoming)
    try:
        out = ai_json(sys_prompt, user)
        if isinstance(out, dict):
            return out
    except Exception as e:
        log("ai error: %s" % e)
    needs = "?" in incoming or bool(re.search(r"\b(bisa|bisakah|gimana|kapan|dimana|apa|tolong|minta|kirim|jadi)\b", incoming, re.I))
    return {"ringkas": incoming, "draft": "", "butuh_balasan": needs, "penting": False, "reminder": None}


def save_draft(chat_jid, name, incoming, draft):
    try:
        with open(DRAFTS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[chat_jid] = {"name": name, "incoming": incoming, "draft": draft,
                      "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    tmp = DRAFTS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DRAFTS)


def load_state():
    try:
        with open(STATE) as f:
            d = json.load(f)
        return int(d.get("rowid", 0)), float(d.get("last_digest", 0)), d.get("last_briefing", "")
    except (OSError, ValueError, TypeError):
        return None, 0.0, ""


def save_state(cursor):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"rowid": cursor, "last_digest": last_digest, "last_briefing": last_briefing}, f)
    os.replace(tmp, STATE)


def init_state():
    global last_digest, last_briefing
    cursor, last_digest, last_briefing = load_state()
    if cursor is None:
        try:
            with db(DB) as con:
                cursor = int(con.execute("SELECT COALESCE(MAX(rowid),0) AS r FROM messages").fetchone()["r"])
        except sqlite3.Error:
            cursor = 0
        save_state(cursor)
        log("started; cursor=%d" % cursor)
    if not last_briefing:
        last_briefing = local_now().strftime("%Y-%m-%d")
        save_state(cursor)
    return cursor


def flush_pending(cursor):
    """Send notifications for chats whose debounce window has passed."""
    now = time.time()
    for jid in list(pending):
        item = pending[jid]
        if item["deadline"] > now:
            continue
        del pending[jid]
        info = resolve(jid)
        if info["muted"]:
            continue
        incoming = " / ".join(item["texts"])
        result = analyze(info["name"], jid, incoming)
        summary = clean(result.get("ringkas") or incoming, 110)
        draft = clean(result.get("draft") or "", 120)
        if draft:
            save_draft(jid, info["name"], incoming, draft)
        needs = bool(result.get("butuh_balasan"))
        urgent = bool(result.get("penting"))
        rem = result.get("reminder")
        rem_line = ""
        if isinstance(rem, dict) and rem.get("teks"):
            rem_line = "⏰ " + clean(rem["teks"], 60)
            jam = rem.get("jam")
            if cfg.get("auto_reminder") and isinstance(jam, str) and re.match(r"^\d{1,2}:\d{2}$", jam):
                try:
                    remind(clean(rem["teks"], 80), jam)
                    rem_line += " (" + jam + ")"
                except Exception as e:
                    log("remind error: %s" % e)
        vip = is_vip(info)
        if in_quiet_hours() and not (vip and cfg.get("quiet_hours_allow_vip")):
            digest.append({"name": info["name"], "text": summary, "ts": time.time()})
            continue
        if vip or urgent:
            lines = [summary]
            if draft:
                lines.append("💬 " + draft)
            if rem_line:
                lines.append(rem_line)
            if needs and not draft:
                lines.append("❗ butuh balasan")
            try:
                notify("WA · " + info["name"], "\n".join(lines))
                log("sent instant to %s (%s)" % (info["name"], jid))
            except Exception as e:
                log("notify failed: %s" % e)
        else:
            digest.append({"name": info["name"], "text": summary, "ts": time.time()})


def send_digest():
    global digest, last_digest
    if not digest:
        last_digest = time.time()
        return
    if in_quiet_hours():
        return
    if time.time() - last_digest < cfg["digest_minutes"] * 60:
        return
    items = digest
    digest = []
    last_digest = time.time()
    names = []
    for it in items:
        if it["name"] not in names:
            names.append(it["name"])
    lines = ["%d chat menunggu:" % len(names)]
    for it in items[:4]:
        lines.append("• %s: %s" % (it["name"], it["text"]))
    if len(items) > 4:
        lines.append("…dan %d pesan lain" % (len(items) - 4))
    try:
        notify("WA · Ringkasan", "\n".join(lines))
        log("sent digest (%d items)" % len(items))
    except Exception as e:
        log("digest notify failed: %s" % e)


def send_briefing():
    global last_briefing
    today = local_now().strftime("%Y-%m-%d")
    if last_briefing == today:
        return
    if local_now().strftime("%H:%M") < cfg["briefing_time"]:
        return
    last_briefing = today
    try:
        with db(DB) as con:
            rows = con.execute(
                "SELECT m.chat_jid, m.content, m.timestamp, m.media_type FROM messages m "
                "JOIN (SELECT chat_jid, MAX(rowid) AS rid FROM messages GROUP BY chat_jid) x "
                "ON x.rid = m.rowid WHERE m.is_from_me = 0 ORDER BY m.timestamp DESC LIMIT 40"
            ).fetchall()
    except sqlite3.Error as e:
        log("briefing db error: %s" % e)
        return
    waiting = []
    for r in rows:
        if not is_private(r["chat_jid"]):
            continue
        info = resolve(r["chat_jid"])
        if info["muted"]:
            continue
        waiting.append("%s: %s" % (info["name"], clean(r["content"] or media_text(r), 60)))
        if len(waiting) >= 6:
            break
    if not waiting:
        return
    try:
        notify("WA · Briefing pagi", "%d chat belum dibalas:\n%s" % (len(waiting), "\n".join("• " + w for w in waiting)))
        log("sent briefing (%d chats)" % len(waiting))
    except Exception as e:
        log("briefing notify failed: %s" % e)


def loop():
    cursor = init_state()
    while running:
        load_config()
        try:
            with db(DB) as con:
                rows = con.execute(
                    "SELECT rowid AS rid, chat_jid, content, timestamp, is_from_me, media_type "
                    "FROM messages WHERE rowid > ? ORDER BY rowid ASC LIMIT 200", (cursor,)).fetchall()
            moved = False
            for row in rows:
                cursor = int(row["rid"])
                moved = True
                if row["is_from_me"] or not is_private(row["chat_jid"]):
                    continue
                if not fresh(row["timestamp"]):
                    continue
                text = media_text(row)
                if not text:
                    continue
                item = pending.get(row["chat_jid"])
                if item is None:
                    pending[row["chat_jid"]] = {"texts": [text], "deadline": time.time() + cfg["debounce_seconds"]}
                else:
                    item["texts"].append(text)
                    item["deadline"] = time.time() + cfg["debounce_seconds"]
            if moved:
                save_state(cursor)
            flush_pending(cursor)
            send_digest()
            send_briefing()
            if moved:
                save_state(cursor)
        except sqlite3.Error as e:
            log("db error: %s" % e)
        except Exception as e:
            log("loop error: %s" % e)
        time.sleep(2)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    load_config(force=True)
    if "--briefing" in sys.argv:
        last_briefing = ""
        send_briefing()
        sys.exit(0)
    if "--dry-run" in sys.argv:
        with db(DB) as con:
            for r in con.execute(
                "SELECT rowid AS rid, chat_jid, content, timestamp, is_from_me, media_type "
                "FROM messages ORDER BY rowid DESC LIMIT 12"):
                if not r["is_from_me"] and is_private(r["chat_jid"]):
                    info = resolve(r["chat_jid"])
                    print(r["rid"], r["timestamp"], info["name"], "vip=%s" % is_vip(info), "|", media_text(r))
        sys.exit(0)
    log("smart notifier starting")
    loop()
    log("smart notifier stopped")
