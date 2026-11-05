"""Hermetic tests for media_cache: no network, no Discord, no WhatsApp."""

import os
import tempfile
import time
import unittest
from unittest import mock

import relay_db
import media_cache as mc
from media_cache import MediaCache


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.cache_dir = os.path.join(self.base, "cache")
        self.db = relay_db.RelayDB(os.path.join(self.base, "state.db"))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def mk(self, **kw):
        return MediaCache(self.cache_dir, self.db, **kw)

    def write_src(self, name, data):
        path = os.path.join(self.base, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def backdate(self, path, days):
        t = time.time() - days * 86400
        os.utime(path, (t, t))


class TestCacheHit(_Base):
    async def test_hit_avoids_second_fetch(self):
        cache = self.mk()
        src = self.write_src("a.png", b"hello world")
        calls = []

        async def fetch():
            calls.append(1)
            return src, "a.png"

        p1, n1, note1 = await cache.get("c1", "m1", fetch)
        p2, n2, note2 = await cache.get("c1", "m1", fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(p1, p2)
        self.assertEqual(note1, "")
        self.assertEqual(note2, "")
        self.assertTrue(os.path.exists(p1))
        # Nothing must stay leased after the test.
        cache.release(p1)
        self.assertEqual(cache.in_use, set())

    async def test_get_leases_path_before_returning(self):
        cache = self.mk()
        src = self.write_src("a.png", b"hello world")
        path, _, _ = await cache.get("c1", "m1", lambda: _async(src, "a.png"))
        self.assertIn(os.path.abspath(path), cache.in_use)
        # A caller that never releases keeps the lease -> cleanup skips it.
        removed = cache.cleanup()
        self.assertTrue(os.path.exists(path))
        cache.release(path)


async def _async(src, name):
    return src, name


class TestSharedSha(_Base):
    async def test_same_sha_across_wa_ids_reuses_file(self):
        cache = self.mk()
        data = b"identical bytes"
        src = self.write_src("a.png", data)

        async def fetch():
            return src, "a.png"

        p1, n1, _ = await cache.get("c1", "m1", fetch)
        p2, n2, _ = await cache.get("c2", "m2", fetch)
        self.assertEqual(p1, p2)  # one physical file
        self.assertEqual(len(os.listdir(self.cache_dir)), 1)
        self.assertEqual(len(self.db.media_cache_rows()), 2)  # both rows alias it
        self.assertEqual(cache.stats(), {"files": 1, "bytes": len(data)})
        self.assertTrue(os.path.exists(p1))
        cache.release(p1)
        cache.release(p2)

    async def test_shared_sha_cleanup_deletes_all_rows_once(self):
        cache = self.mk(max_bytes=4)  # force eviction of the 12-byte file
        src = self.write_src("a.png", b"twelve bytes")

        async def fetch():
            return src, "a.png"

        p1, _, _ = await cache.get("c1", "m1", fetch)
        await cache.get("c2", "m2", fetch)
        rows = self.db.media_cache_rows()
        self.assertEqual(len(rows), 2)
        unlink_calls = []

        real_unlink = os.unlink

        def counted_unlink(path, **kw):
            unlink_calls.append(path)
            return real_unlink(path, **kw)

        with mock.patch("media_cache.os.unlink", side_effect=counted_unlink):
            removed = cache.cleanup()

        self.assertFalse(os.path.exists(p1))
        self.assertEqual(self.db.media_cache_rows(), [])
        self.assertEqual(unlink_calls, [os.path.abspath(p1)])  # unlinked once
        self.assertGreaterEqual(removed, 1)


class TestCleanup(_Base):
    async def test_cleanup_removes_stale_row_and_file(self):
        cache = self.mk(days=1)
        src = self.write_src("a.png", b"stale data")
        path, _, _ = await cache.get("c1", "m1", lambda: _async(src, "a.png"))
        cache.release(path)
        self.backdate(path, 2)

        removed = cache.cleanup()
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(self.db.media_cache_get("c1", "m1"))

    async def test_cleanup_skips_in_use_path(self):
        cache = self.mk(days=1)
        src = self.write_src("a.png", b"fresh")
        path, _, _ = await cache.get("c1", "m1", lambda: _async(src, "a.png"))
        src2 = self.write_src("b.png", b"other")
        path2, _, _ = await cache.get("c2", "m2", lambda: _async(src2, "b.png"))
        # get() leases internally; simulate callers releasing.
        cache.release(path)
        cache.release(path2)
        self.backdate(path, 2)
        self.backdate(path2, 2)
        # The caller re-marks path while preparing to post it.
        cache.mark_in_use(path)

        removed = cache.cleanup()
        self.assertTrue(os.path.exists(path))    # leased -> kept
        self.assertFalse(os.path.exists(path2))  # unleased -> removed
        self.assertEqual(
            [r["wa_id"] for r in self.db.media_cache_rows()], ["m1"]
        )
        cache.release(path)
        removed = cache.cleanup()
        self.assertFalse(os.path.exists(path))   # no longer leased -> gone
        self.assertEqual(self.db.media_cache_rows(), [])

    async def test_max_bytes_evicts_oldest_unique_path(self):
        cache = self.mk(max_bytes=150, days=0)  # days=0 -> no age eviction
        src1 = self.write_src("a.png", b"x" * 100)
        src2 = self.write_src("b.png", b"y" * 100)
        p_old, _, _ = await cache.get("c1", "m1", lambda: _async(src1, "a.png"))
        cache.release(p_old)
        self.backdate(p_old, 3)
        p_new, _, _ = await cache.get("c2", "m2", lambda: _async(src2, "b.png"))
        cache.release(p_new)

        removed = cache.cleanup()
        self.assertFalse(os.path.exists(p_old))
        self.assertTrue(os.path.exists(p_new))
        survivors = self.db.media_cache_rows()
        self.assertEqual([r["wa_id"] for r in survivors], ["m2"])
        self.assertGreaterEqual(removed, 1)

    async def test_cleanup_missing_file_drops_all_rows_for_path(self):
        cache = self.mk(days=1)
        src = self.write_src("a.png", b"data")
        path, _, _ = await cache.get("c1", "m1", lambda: _async(src, "a.png"))
        cache.release(path)
        # Simulate a second row aliasing the same shared path, then lose it.
        self.db.media_cache_put("c2", "m2", "sha", path, 4, "old")
        os.remove(path)

        removed = cache.cleanup()
        self.assertEqual(removed, 0)  # nothing was unlinked
        self.assertEqual(self.db.media_cache_rows(), [])


class TestStats(_Base):
    async def test_stats_reports_unique_files(self):
        cache = self.mk()
        src = self.write_src("a.png", b"seven7")
        async def fetch():
            return src, "a.png"
        p, _, _ = await cache.get("c1", "m1", fetch)
        await cache.get("c2", "m2", fetch)

        stats = cache.stats()
        self.assertEqual(stats["files"], 1)  # not 2 rows
        self.assertEqual(stats["bytes"], 6)  # real unique bytes, not 2 rows
        cache.release(p)


class TestConvertImage(_Base):
    @unittest.skipUnless(mc.PILLOW_OK, "Pillow not installed")
    def test_convert_image_webp_to_png(self):
        from PIL import Image
        src = os.path.join(self.base, "in.webp")
        dst = os.path.join(self.base, "out.png")
        Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(src, "WEBP")

        ok = mc.convert_image(src, dst)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(dst))
        self.assertGreater(os.path.getsize(dst), 0)
        with Image.open(dst) as img:
            self.assertEqual(img.format, "PNG")
            self.assertEqual(img.size, (4, 4))

    @unittest.skipUnless(mc.PILLOW_OK, "Pillow not installed")
    def test_convert_image_bad_file_returns_false(self):
        src = os.path.join(self.base, "bad.webp")
        dst = os.path.join(self.base, "out.png")
        with open(src, "wb") as fh:
            fh.write(b"not an image")
        self.assertFalse(mc.convert_image(src, dst))
        self.assertFalse(os.path.exists(dst))


if __name__ == "__main__":
    unittest.main()
