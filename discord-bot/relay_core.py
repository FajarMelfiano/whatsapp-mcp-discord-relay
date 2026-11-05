"""Pure helpers for the WhatsApp <-> Discord relay (no Discord imports)."""

import datetime
import json
import os
import re

MAX_WA_TO_DISCORD = 8 * 1024 * 1024
MAX_DISCORD_TOTAL = 8 * 1024 * 1024
MAX_DISCORD_COUNT = 5
MAX_TEXT = 1900

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif"}
VIDEO_EXT = {"mp4", "3gp", "mov", "avi", "mkv", "webm", "m4v"}
AUDIO_EXT = {"ogg", "opus", "mp3", "m4a", "wav", "aac", "amr", "flac"}
DOC_EXT = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv",
    "zip", "rar", "7z", "tar", "gz", "json", "xml", "html", "epub",
}
EXEC_EXT = {
    "exe", "bat", "cmd", "com", "scr", "msi", "apk", "app", "sh", "py",
    "js", "vbs", "ps1", "jar", "deb", "rpm", "bin", "iso", "dll", "so",
    "elf", "appimage", "ipa", "xapk", "apks",
}

INVALID_NAMES = {"", ".", "-", "~", "...", "null", "none"}


def is_relayable_jid(jid):
    if not jid or not isinstance(jid, str):
        return False
    if jid == "status@broadcast":
        return False
    return not (jid.endswith("@newsletter") or jid.endswith("@broadcast"))


def _clean_name(name):
    name = (name or "").strip()
    if name.lower() in INVALID_NAMES or len(name) < 2 or name.isdigit():
        return ""
    return name


def chat_title(jid, chat_name, names, pns):
    num = jid.split("@")[0]
    if jid.endswith("@g.us"):
        name = _clean_name(chat_name) or num
        if name == num and len(num) > 12:
            name = "Grup " + num[-6:]
        return "Grup · " + name, jid

    name = _clean_name(names.get(jid, ""))
    phone = pns.get(num, "") if jid.endswith("@lid") else ""
    if not name and phone and phone.strip().lower() not in INVALID_NAMES:
        name = phone.strip()
    if not name:
        name = _clean_name(chat_name)
    if not name:
        name = "Kontak · " + num[-4:] if len(num) >= 4 else "Kontak"
    send_to = phone if (jid.endswith("@lid") and phone) else jid
    return "DM · " + name, send_to


def sender_display(sender, names, pns, push_name="", chat_jid=""):
    """Resolve a sender for display.

    push_name is the name WhatsApp attaches to the individual message; it is
    the only source for members who are not in the address book. Old history
    rows may carry the group JID as sender, which must never be displayed.
    An unknown LID member (no saved name, no LID->PN phone, no push name)
    becomes a neutral placeholder ("Anggota grup" in group chats, "Kontak"
    elsewhere) instead of a raw numeric LID.
    """
    is_group = (chat_jid or "").endswith("@g.us")
    group_num = (chat_jid or "").split("@")[0] if is_group else ""
    push = _clean_name(push_name)
    if not sender:
        return push or ("Anggota grup" if is_group else "Kontak")
    num = sender.split("@")[0]
    if sender.endswith("@g.us") or (group_num and num == group_num):
        return push or "Anggota grup"

    # LID-shaped sender (explicit @lid, or a bare numeric member of a group
    # chat, which history rows resolve through the @lid candidates below).
    is_lid = sender.endswith("@lid") or ("@" not in sender and is_group)

    if "@" in sender:
        candidates = [sender]
    else:
        candidates = [sender + "@lid", sender + "@s.whatsapp.net"]
    for jid in candidates:
        name = _clean_name(names.get(jid, ""))
        if name:
            return name
    for jid in candidates:
        if jid.endswith("@lid"):
            phone = (pns.get(jid.split("@")[0], "") or "").strip()
            if phone:
                name = _clean_name(names.get(phone + "@s.whatsapp.net", ""))
                if name:
                    return name
    if push:
        return push
    phone = (pns.get(num, "") or "").strip()
    if phone and phone.lower() not in INVALID_NAMES:
        return phone
    # Never show a raw numeric LID as a member/contact name.
    if is_lid:
        return "Anggota grup" if is_group else "Kontak"
    return num


def full_sender(chat_jid, sender):
    if not sender:
        return ""
    if "@" in sender:
        return sender
    if chat_jid.endswith("@lid"):
        return sender + "@lid"
    return sender + "@s.whatsapp.net"


def safe_filename(name, fallback="file.bin"):
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    if not name or name in {".", ".."}:
        return fallback
    return name[:120]


def extension(name):
    base = os.path.basename(name or "")
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[1].lower()


def classify_media(name, media_type=""):
    ext = extension(name)
    mt = (media_type or "").lower()
    if ext in EXEC_EXT:
        return "executable"
    if mt == "image" or ext in IMAGE_EXT:
        return "image"
    if mt == "video" or ext in VIDEO_EXT:
        return "video"
    if mt in ("audio", "ptt") or ext in AUDIO_EXT:
        return "audio"
    if mt == "sticker":
        return "sticker"
    if mt == "document" or ext in DOC_EXT:
        return "document"
    return "file"


def media_allowed(filename, media_type, size, max_bytes=MAX_WA_TO_DISCORD):
    if size is not None and size > max_bytes:
        return False, "ukuran %s MiB melebihi batas %s MiB" % (
            round((size or 0) / 1048576, 1),
            round(max_bytes / 1048576, 1),
        )
    kind = classify_media(filename, media_type)
    if kind == "executable":
        return False, "file executable ora diidinake"
    mt = (media_type or "").lower()
    ext = extension(filename)
    if mt == "image" and ext not in IMAGE_EXT:
        return False, "tipe media image tidak cocok dengan ekstensi"
    if mt == "video" and ext not in VIDEO_EXT:
        return False, "tipe media video tidak cocok dengan ekstensi"
    if mt in ("audio", "ptt") and ext not in AUDIO_EXT:
        return False, "tipe media audio tidak cocok dengan ekstensi"
    return True, ""


def validate_attachments(items, max_count=MAX_DISCORD_COUNT, max_total=MAX_DISCORD_TOTAL):
    if not items:
        return True, "", []
    if len(items) > max_count:
        return False, "maksimal %d lampiran" % max_count, []
    total = sum(int(size or 0) for _, size in items)
    if total > max_total:
        return False, "total lampiran %s MiB melebihi batas %s MiB" % (
            round(total / 1048576, 1),
            round(max_total / 1048576, 1),
        ), []
    for name, size in items:
        kind = classify_media(name, "")
        if kind == "executable":
            return False, "file executable ora diidinake: %s" % safe_filename(name), []
    return True, "", list(items)


def split_message(text, limit=MAX_TEXT):
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def quote_preview(text, limit=180):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def format_relay_message(sender_name, content, outgoing=False, media_type="", quote=None):
    prefix = "**You**" if outgoing else "**%s**" % sender_name
    lines = [prefix]
    if quote:
        lines.append("> " + quote_preview(quote).replace("\n", "\n> "))
    body = (content or "").strip()
    if not body and media_type:
        body = "*[%s]*" % media_type
    if body:
        lines.append(body)
    return "\n".join(lines)


def parse_ts(value):
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        raw = raw.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(raw)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def within_days(ts, now, days):
    if ts is None:
        return False
    if not days or int(days) <= 0:
        return True
    return ts >= now - datetime.timedelta(days=int(days))


def reaction_emoji_ok(emoji):
    emoji = (emoji or "").strip()
    if not emoji:
        return False
    if emoji.startswith(":") and emoji.endswith(":") and len(emoji) > 2:
        return False
    return len(emoji) <= 64


def permission_spec(owner_id, bot_id, allowed_ids=None):
    manage = {
        "view_channel", "send_messages", "send_messages_in_threads",
        "create_public_threads", "read_message_history", "attach_files",
        "add_reactions", "manage_threads", "embed_links",
    }
    allow = sorted(manage)
    deny = sorted(manage)
    users = {}
    for uid in [owner_id, bot_id] + list(allowed_ids or []):
        if uid:
            users[str(uid)] = allow
    return {"default": {"allow": [], "deny": deny}, "users": users}


def format_mentions(text, mentioned_jids, names, pns, chat_jid=""):
    """Resolve WhatsApp mention placeholders to display names.

    Never leaves a raw ``@<digits>`` for a group member, and never returns a
    numeric group JID as a mention label.
    """
    if not mentioned_jids:
        return text or ""
    if isinstance(mentioned_jids, (list, tuple)):
        jids = [str(j) for j in mentioned_jids]
    elif isinstance(mentioned_jids, str):
        raw = mentioned_jids.strip()
        try:
            parsed = json.loads(raw)
            jids = [str(j) for j in parsed] if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            jids = [raw] if "@" in raw else []
    else:
        jids = []
    body = text or ""
    labels = []
    for jid in jids:
        label = sender_display(jid, names, pns, "", chat_jid)
        if not label:
            continue
        labels.append("@" + label.lstrip("@"))
        num = jid.split("@")[0]
        if num:
            body = re.sub(r"@" + re.escape(num) + r"\b", "@" + label.lstrip("@"), body)
    if labels:
        missing = [label for label in labels if label not in body]
        if missing:
            body = (body + "\n" if body else "") + "↳ menyebut: " + ", ".join(missing)
    return body


def thread_allowed(jid, policy="mapped", allowlist=(), is_mapped=False):
    if not is_relayable_jid(jid):
        return False
    if is_mapped:
        return True
    if not jid.endswith("@g.us"):
        return True
    policy = (policy or "mapped").lower()
    if policy == "all":
        return True
    if policy == "allowlist":
        return jid in set(allowlist or ())
    return False


def is_idle(last_ts, now, days):
    if not days or int(days) <= 0:
        return False
    if last_ts is None:
        return False
    return last_ts < now - datetime.timedelta(days=int(days))


def format_edit_event(content, edited=True):
    body = (content or "").strip()
    if edited:
        return "*[diedit]*\n" + body if body else "*[diedit]*"
    return "*[dihapus]*"


def dedupe_key(wa_id, chat_jid):
    return "%s|%s" % (wa_id, chat_jid)


def should_mirror_reaction(is_from_me):
    return not bool(is_from_me)
