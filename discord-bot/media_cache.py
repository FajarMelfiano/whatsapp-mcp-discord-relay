"""Content-addressed media cache with optional image conversion.

The bridge is the only component that talks to WhatsApp. This cache stores a
copy under a SHA-256 key so a message is downloaded at most once per byte
content, and converts webp/heic stickers/photos that Discord cannot render.
"""

import asyncio
import hashlib
import logging
import os
import time

from relay_core import extension, safe_filename

try:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    PILLOW_OK = True
except Exception:  # pragma: no cover - optional dependency
    Image = None
    PILLOW_OK = False

log = logging.getLogger("wa-discord")
CONVERT_EXT = {"webp", "heic", "heif"}


def convert_image(src, dst):
    """Convert the first frame of a webp/heic/heif image to PNG."""
    if not PILLOW_OK:
        return False
    try:
        with Image.open(src) as img:
            img.seek(0)
            frame = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img.copy()
            if frame.mode == "RGBA":
                frame.save(dst, "PNG")
            else:
                frame.save(dst, "PNG")
        return os.path.getsize(dst) > 0
    except Exception as exc:
        log.warning("image conversion failed for %s: %s", src, exc)
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False


class MediaCache:
    def __init__(self, root, relay, max_bytes=512 * 1024 * 1024,
                 days=14, logger=None):
        self.root = root
        self.relay = relay
        self.max_bytes = max(0, int(max_bytes))
        self.days = max(0, int(days))
        self.log = logger or log
        self.in_use = set()
        self._locks = {}
        self._lock_guard = asyncio.Lock()
        os.makedirs(self.root, exist_ok=True)

    async def _key_lock(self, key):
        async with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
        return lock

    def _find_existing(self, chat_jid, wa_id, sha, ext):
        rec = self.relay.media_cache_get(chat_jid, wa_id)
        if rec and rec["path"] and os.path.exists(rec["path"]):
            return rec["path"], rec["sha256"]
        # Same bytes may already be cached for another message.
        row = self.relay.con.execute(
            "SELECT path FROM media_cache WHERE sha256 = ? LIMIT 1", (sha,)
        ).fetchone()
        if row and row["path"] and os.path.exists(row["path"]):
            return row["path"], sha
        for name in (sha + "." + ext, sha + ".png"):
            path = os.path.join(self.root, name)
            if os.path.exists(path):
                return path, sha
        return None, sha

    async def get(self, chat_jid, wa_id, fetcher, max_bytes=None):
        """Return (path, display_name, note) for one WA message, or (None,...)."""
        lock = await self._key_lock("%s|%s" % (chat_jid, wa_id))
        async with lock:
            rec = self.relay.media_cache_get(chat_jid, wa_id)
            if rec and rec["path"] and os.path.exists(rec["path"]):
                # Lease the file before yielding the per-key lock so a
                # concurrent cleanup() cannot delete it between get() and
                # the caller's post_to_thread. Callers release via release().
                self.in_use.add(os.path.abspath(rec["path"]))
                return rec["path"], os.path.basename(rec["path"]), ""

            src, filename = await fetcher()
            if not src or not os.path.exists(src):
                return None, "", "media gagal diunduh"
            ext = extension(filename) or extension(src) or "bin"
            filename = safe_filename(filename or ("media." + ext))
            try:
                with open(src, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                return None, "", "media gagal dibaca: %s" % exc

            limit = int(max_bytes or 0)
            if limit and len(data) > limit:
                return None, "", "ukuran %.1f MiB melebihi batas %.1f MiB" % (
                    len(data) / 1048576, limit / 1048576,
                )

            sha = hashlib.sha256(data).hexdigest()
            existing, _ = self._find_existing(chat_jid, wa_id, sha, ext)
            if existing:
                self.relay.media_cache_put(
                    chat_jid, wa_id, sha, existing, os.path.getsize(existing),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                # Same lease rule as the cache-hit fast path above.
                self.in_use.add(os.path.abspath(existing))
                return existing, os.path.basename(existing), ""

            note = ""
            target_ext = ext
            target = os.path.join(self.root, sha + "." + target_ext)
            if ext in CONVERT_EXT:
                converted = os.path.join(self.root, sha + ".png")
                tmp = converted + ".tmp"
                if await asyncio.to_thread(convert_image, src, tmp):
                    os.replace(tmp, converted)
                    target, target_ext = converted, "png"
                else:
                    note = "[media asli dikirim; konversi gagal]"

            if target == os.path.join(self.root, sha + "." + ext):
                tmp = target + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)

            size = os.path.getsize(target)
            self.relay.media_cache_put(
                chat_jid, wa_id, sha, target, size,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            # Same lease rule: protect the fresh file from cleanup().
            self.in_use.add(os.path.abspath(target))
            return target, (sha + "." + target_ext), note

    def mark_in_use(self, path):
        if path:
            self.in_use.add(os.path.abspath(path))

    def release(self, path):
        if path:
            self.in_use.discard(os.path.abspath(path))

    @staticmethod
    def _unique_paths(rows):
        """Group rows by physical file.

        Multiple msg_map rows may point to one content-addressed file, so
        every per-path operation (age check, unlink, eviction, accounting)
        must happen once per unique path, not once per row.
        """
        groups = {}
        for rec in rows:
            path = rec["path"] or ""
            if not path:
                continue
            key = os.path.abspath(path)
            g = groups.get(key)
            if g is None:
                g = {"path": path, "rows": [], "size": int(rec["size"] or 0)}
                groups[key] = g
            g["rows"].append(rec)
            g["size"] = max(g["size"], int(rec["size"] or 0))
        return groups

    def _path_size(self, key, group):
        """Best-known size of a file: real size, else largest row size."""
        try:
            return os.path.getsize(group["path"])
        except OSError:
            return group["size"]

    def _delete_group(self, group):
        """Unlink the file once and delete every DB row pointing at it."""
        for rec in group["rows"]:
            self.relay.media_cache_delete(rec["chat_jid"], rec["wa_id"])
        try:
            os.unlink(group["path"])
            return 1
        except OSError:
            return 0

    def stats(self):
        groups = self._unique_paths(self.relay.media_cache_rows())
        files = 0
        total = 0
        for key, group in groups.items():
            if not os.path.isfile(group["path"]):
                continue
            files += 1
            total += self._path_size(key, group)
        return {"files": files, "bytes": total}

    def cleanup(self):
        """Delete stale/unreferenced files, then evict oldest to the cap."""
        now = time.time()
        removed = 0
        groups = self._unique_paths(self.relay.media_cache_rows())

        # 1. Per-path age check (shared files are only touched once).
        for key, group in list(groups.items()):
            if key in self.in_use:
                continue
            try:
                mtime = os.path.getmtime(group["path"])
            except OSError:
                # File is gone: drop all rows pointing at it, keep no group.
                for r in group["rows"]:
                    self.relay.media_cache_delete(r["chat_jid"], r["wa_id"])
                groups.pop(key, None)
                continue
            if self.days and now - mtime > self.days * 86400:
                removed += self._delete_group(group)
                groups.pop(key, None)

        # 2. Evict oldest unique paths until total <= max_bytes.
        if self.max_bytes:
            ordered = sorted(
                groups.items(),
                key=lambda kv: (os.path.getmtime(kv[1]["path"]),),
            )
            total = 0
            for key, group in ordered:
                total += self._path_size(key, group)
            for key, group in ordered:
                if total <= self.max_bytes:
                    break
                size = self._path_size(key, group)
                removed += self._delete_group(group)
                groups.pop(key, None)
                total -= size

        # Orphan files (e.g. a crash between write and DB insert).
        known = {os.path.abspath(r["path"]) for r in self.relay.media_cache_rows()}
        for name in os.listdir(self.root):
            path = os.path.abspath(os.path.join(self.root, name))
            if path in self.in_use or path in known or not os.path.isfile(path):
                continue
            try:
                if self.days and now - os.path.getmtime(path) > self.days * 86400:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        return removed
