"""SQLite state for the WhatsApp <-> Discord relay."""

import os
import sqlite3


class RelayDB:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self):
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS msg_map (
                wa_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                thread_id INTEGER,
                discord_id INTEGER,
                direction TEXT,
                ts TEXT,
                PRIMARY KEY (wa_id, chat_jid)
            );
            CREATE INDEX IF NOT EXISTS idx_msg_map_discord
                ON msg_map(discord_id);
            CREATE TABLE IF NOT EXISTS reaction_mirror (
                chat_jid TEXT NOT NULL,
                wa_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                emoji TEXT NOT NULL,
                PRIMARY KEY (chat_jid, wa_id, sender)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS receipt_mirror (
                chat_jid TEXT NOT NULL,
                wa_id TEXT NOT NULL,
                PRIMARY KEY (chat_jid, wa_id)
            );
            CREATE TABLE IF NOT EXISTS sender_names (
                chat_jid TEXT NOT NULL,
                sender TEXT NOT NULL,
                name TEXT NOT NULL,
                source TEXT,
                updated_at TEXT,
                PRIMARY KEY (chat_jid, sender)
            );
            CREATE TABLE IF NOT EXISTS media_cache (
                wa_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (wa_id, chat_jid)
            );
            CREATE INDEX IF NOT EXISTS idx_media_cache_sha ON media_cache(sha256);
            """
        )
        cols = [r["name"] for r in self.con.execute("PRAGMA table_info(msg_map)").fetchall()]
        if "author_id" not in cols:
            self.con.execute("ALTER TABLE msg_map ADD COLUMN author_id INTEGER")
        self.con.commit()

    def close(self):
        try:
            self.con.close()
        except sqlite3.Error:
            pass

    def get_meta(self, key, default=None):
        row = self.con.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        self.con.commit()

    def has_message(self, wa_id, chat_jid):
        return self.con.execute(
            "SELECT 1 FROM msg_map WHERE wa_id = ? AND chat_jid = ?",
            (wa_id, chat_jid),
        ).fetchone() is not None

    def map_message(self, wa_id, chat_jid, thread_id, discord_id, direction, ts, author_id=None):
        self.con.execute(
            """INSERT OR REPLACE INTO msg_map
               (wa_id, chat_jid, thread_id, discord_id, direction, ts, author_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (wa_id, chat_jid, thread_id, discord_id, direction, str(ts or ""), author_id),
        )
        self.con.commit()

    def discord_for(self, wa_id, chat_jid):
        return self.con.execute(
            "SELECT * FROM msg_map WHERE wa_id = ? AND chat_jid = ?",
            (wa_id, chat_jid),
        ).fetchone()

    def wa_for_discord(self, discord_id):
        return self.con.execute(
            "SELECT * FROM msg_map WHERE discord_id = ? ORDER BY rowid DESC LIMIT 1",
            (discord_id,),
        ).fetchone()

    def reaction_emoji_of(self, chat_jid, wa_id, sender):
        row = self.con.execute(
            """SELECT emoji FROM reaction_mirror
               WHERE chat_jid = ? AND wa_id = ? AND sender = ?""",
            (chat_jid, wa_id, sender),
        ).fetchone()
        return row["emoji"] if row else ""

    def add_reaction(self, chat_jid, wa_id, sender, emoji):
        self.con.execute(
            """INSERT OR REPLACE INTO reaction_mirror
               (chat_jid, wa_id, sender, emoji) VALUES (?, ?, ?, ?)""",
            (chat_jid, wa_id, sender, emoji),
        )
        self.con.commit()

    def remove_reaction(self, chat_jid, wa_id, sender):
        self.con.execute(
            """DELETE FROM reaction_mirror
               WHERE chat_jid = ? AND wa_id = ? AND sender = ?""",
            (chat_jid, wa_id, sender),
        )
        self.con.commit()

    def remove_message(self, wa_id, chat_jid):
        self.con.execute(
            "DELETE FROM msg_map WHERE wa_id = ? AND chat_jid = ?",
            (wa_id, chat_jid),
        )
        self.con.commit()

    def has_any_for_chat(self, chat_jid):
        return self.con.execute(
            "SELECT 1 FROM msg_map WHERE chat_jid = ? LIMIT 1", (chat_jid,)
        ).fetchone() is not None

    def mappings_for_chat(self, chat_jid):
        return self.con.execute(
            "SELECT * FROM msg_map WHERE chat_jid = ?", (chat_jid,)
        ).fetchall()

    def clear_chat(self, chat_jid):
        self.con.execute("DELETE FROM msg_map WHERE chat_jid = ?", (chat_jid,))
        self.con.execute(
            "DELETE FROM reaction_mirror WHERE chat_jid = ?", (chat_jid,)
        )
        self.con.execute(
            "DELETE FROM receipt_mirror WHERE chat_jid = ?", (chat_jid,)
        )
        self.con.commit()

    def receipt_seen(self, chat_jid, wa_id):
        return self.con.execute(
            "SELECT 1 FROM receipt_mirror WHERE chat_jid = ? AND wa_id = ?",
            (chat_jid, wa_id),
        ).fetchone() is not None

    def mark_receipt(self, chat_jid, wa_id):
        self.con.execute(
            "INSERT OR IGNORE INTO receipt_mirror (chat_jid, wa_id) VALUES (?, ?)",
            (chat_jid, wa_id),
        )
        self.con.commit()

    def sender_name(self, chat_jid, sender):
        row = self.con.execute(
            "SELECT name FROM sender_names WHERE chat_jid = ? AND sender = ?",
            (chat_jid, sender),
        ).fetchone()
        return row["name"] if row else ""

    def set_sender_name(self, chat_jid, sender, name, source="resolver"):
        self.con.execute(
            """INSERT OR REPLACE INTO sender_names
               (chat_jid, sender, name, source, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (chat_jid, sender, name, source),
        )
        self.con.commit()

    def prune_sender_names(self, days=180):
        self.con.execute(
            "DELETE FROM sender_names WHERE updated_at < datetime('now', ?)",
            ("-%d days" % int(days),),
        )
        self.con.commit()

    def sender_name_stats(self):
        row = self.con.execute(
            "SELECT COUNT(*) AS n FROM sender_names"
        ).fetchone()
        return int(row["n"] if row else 0)

    def media_cache_get(self, chat_jid, wa_id):
        return self.con.execute(
            "SELECT * FROM media_cache WHERE chat_jid = ? AND wa_id = ?",
            (chat_jid, wa_id),
        ).fetchone()

    def media_cache_put(self, chat_jid, wa_id, sha256, path, size, updated_at):
        self.con.execute(
            """INSERT OR REPLACE INTO media_cache
               (wa_id, chat_jid, sha256, path, size, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (wa_id, chat_jid, sha256, path, int(size), str(updated_at or "")),
        )
        self.con.commit()

    def media_cache_rows(self):
        return self.con.execute(
            "SELECT * FROM media_cache ORDER BY updated_at ASC"
        ).fetchall()

    def media_cache_delete(self, chat_jid, wa_id):
        self.con.execute(
            "DELETE FROM media_cache WHERE chat_jid = ? AND wa_id = ?",
            (chat_jid, wa_id),
        )
        self.con.commit()

    def media_cache_bytes(self):
        row = self.con.execute(
            "SELECT COALESCE(SUM(size), 0) AS n FROM media_cache"
        ).fetchone()
        return int(row["n"] if row else 0)

    def reaction_count(self, chat_jid, wa_id, emoji):
        row = self.con.execute(
            """SELECT COUNT(*) AS n FROM reaction_mirror
               WHERE chat_jid = ? AND wa_id = ? AND emoji = ?""",
            (chat_jid, wa_id, emoji),
        ).fetchone()
        return row["n"] if row else 0
