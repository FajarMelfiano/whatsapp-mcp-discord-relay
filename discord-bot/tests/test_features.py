import asyncio
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay_core as rc
from relay_db import RelayDB
import media_cache as mc
from discord_util import TokenBucket


class TestMentions(unittest.TestCase):
    def test_mention_uses_name(self):
        text = rc.format_mentions(
            "halo @628123", ["628123@s.whatsapp.net"],
            {"628123@s.whatsapp.net": "Budi"}, {}, "1@g.us",
        )
        self.assertIn("@Budi", text)
        self.assertNotIn("@628123", text)

    def test_mention_fallback_phone(self):
        text = rc.format_mentions(
            "cek @99", '["99@lid"]', {}, {"99": "62877"}, "1@g.us",
        )
        self.assertIn("@62877", text)

    def test_mention_never_group_jid(self):
        text = rc.format_mentions(
            "halo", '["120@g.us"]', {}, {}, "120@g.us",
        )
        self.assertNotIn("120@g.us", text)

    def test_mention_ignores_invalid_json(self):
        self.assertEqual(rc.format_mentions("x", "not-json", {}, {}), "x")


class TestThreadPolicy(unittest.TestCase):
    def test_dm_always_allowed(self):
        self.assertTrue(rc.thread_allowed("1@s.whatsapp.net", "mapped"))

    def test_group_mapped_skipped(self):
        self.assertFalse(rc.thread_allowed("1@g.us", "mapped"))

    def test_group_mapped_existing_allowed(self):
        self.assertTrue(rc.thread_allowed("1@g.us", "mapped", is_mapped=True))

    def test_group_allowlist(self):
        self.assertTrue(rc.thread_allowed("1@g.us", "allowlist", ["1@g.us"]))
        self.assertFalse(rc.thread_allowed("2@g.us", "allowlist", ["1@g.us"]))

    def test_group_all(self):
        self.assertTrue(rc.thread_allowed("1@g.us", "all"))

    def test_newsletter_never(self):
        self.assertFalse(rc.thread_allowed("1@newsletter", "all"))


class TestIdle(unittest.TestCase):
    def test_idle_true(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(days=31)
        self.assertTrue(rc.is_idle(old, now, 30))

    def test_idle_false_recent(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertFalse(rc.is_idle(now, now, 30))

    def test_edit_event(self):
        self.assertIn("diedit", rc.format_edit_event("baru"))
        self.assertIn("dihapus", rc.format_edit_event("", False))


class TestTokenBucket(unittest.IsolatedAsyncioTestCase):
    async def test_rate_waits(self):
        bucket = TokenBucket(100)
        bucket.capacity = 1.0
        bucket.tokens = 1.0
        loop = asyncio.get_running_loop()
        start = loop.time()
        await bucket.acquire()
        await bucket.acquire()
        elapsed = loop.time() - start
        self.assertGreaterEqual(elapsed, 0.005)


class TestRelayDBState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = RelayDB(os.path.join(self.tmp.name, "relay.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_receipt_once(self):
        self.assertFalse(self.db.receipt_seen("c", "m"))
        self.db.mark_receipt("c", "m")
        self.assertTrue(self.db.receipt_seen("c", "m"))

    def test_sender_name_cache(self):
        self.assertEqual(self.db.sender_name("c", "s"), "")
        self.db.set_sender_name("c", "s", "Budi")
        self.assertEqual(self.db.sender_name("c", "s"), "Budi")
        self.assertEqual(self.db.sender_name_stats(), 1)

    def test_media_cache(self):
        self.db.media_cache_put("c", "m", "sha", "/tmp/x.png", 12, "now")
        row = self.db.media_cache_get("c", "m")
        self.assertEqual(row["sha256"], "sha")
        self.assertEqual(self.db.media_cache_bytes(), 12)
        self.db.media_cache_delete("c", "m")
        self.assertIsNone(self.db.media_cache_get("c", "m"))


@unittest.skipUnless(mc.PILLOW_OK, "Pillow/pillow-heif not installed")
class TestConversion(unittest.TestCase):
    def test_webp_to_png(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.webp")
            dst = os.path.join(tmp, "a.png")
            Image.new("RGBA", (12, 10), (255, 0, 0, 128)).save(src, "WEBP")
            self.assertTrue(mc.convert_image(src, dst))
            self.assertTrue(os.path.exists(dst))
            self.assertGreater(os.path.getsize(dst), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
