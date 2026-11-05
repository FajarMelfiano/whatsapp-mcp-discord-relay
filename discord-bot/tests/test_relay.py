import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay_core as rc


UTC = datetime.timezone.utc


class TestJids(unittest.TestCase):
    def test_private_is_relayable(self):
        self.assertTrue(rc.is_relayable_jid("628123@s.whatsapp.net"))

    def test_newsletter_not_relayable(self):
        self.assertFalse(rc.is_relayable_jid("123@newsletter"))

    def test_broadcast_not_relayable(self):
        self.assertFalse(rc.is_relayable_jid("123@broadcast"))

    def test_status_not_relayable(self):
        self.assertFalse(rc.is_relayable_jid("status@broadcast"))

    def test_empty_not_relayable(self):
        self.assertFalse(rc.is_relayable_jid(""))


class TestTitles(unittest.TestCase):
    def test_group_title(self):
        title, send_to = rc.chat_title("1@g.us", "Keluarga", {}, {})
        self.assertEqual(title, "Grup · Keluarga")
        self.assertEqual(send_to, "1@g.us")

    def test_group_numeric_name_fallback(self):
        title, _ = rc.chat_title("123456789012345@g.us", "123456789012345", {}, {})
        self.assertTrue(title.startswith("Grup "))

    def test_dm_known_name(self):
        title, send_to = rc.chat_title(
            "6281@s.whatsapp.net", "", {"6281@s.whatsapp.net": "Budi"}, {}
        )
        self.assertEqual(title, "DM · Budi")
        self.assertEqual(send_to, "6281@s.whatsapp.net")

    def test_dm_lid_uses_phone(self):
        title, send_to = rc.chat_title("99@lid", "", {}, {"99": "62877"})
        self.assertEqual(title, "DM · 62877")
        self.assertEqual(send_to, "62877")

    def test_dm_unknown_is_kontak(self):
        title, _ = rc.chat_title("6289999@s.whatsapp.net", "", {}, {})
        self.assertTrue(title.startswith("DM · Kontak"))

    def test_dm_bad_name_falls_back(self):
        title, _ = rc.chat_title(
            "6289999@s.whatsapp.net", "", {"6289999@s.whatsapp.net": "..."}, {}
        )
        self.assertIn("Kontak", title)

    def test_sender_display_known(self):
        self.assertEqual(
            rc.sender_display("6281", {"6281@s.whatsapp.net": "Budi"}, {}), "Budi"
        )

    def test_sender_display_numeric(self):
        self.assertEqual(rc.sender_display("6281", {}, {}), "6281")

    def test_sender_display_push_name(self):
        self.assertEqual(
            rc.sender_display("999@lid", {}, {}, "Budi", "120@g.us"), "Budi"
        )

    def test_sender_display_group_jid_never_shown(self):
        self.assertEqual(
            rc.sender_display("120@g.us", {}, {}, "", "120@g.us"), "Anggota grup"
        )

    def test_sender_display_lid_phone_fallback(self):
        self.assertEqual(
            rc.sender_display("999", {}, {"999": "628123"}, "", "120@g.us"), "628123"
        )

    def test_sender_display_lid_contact_name(self):
        self.assertEqual(
            rc.sender_display(
                "999@lid", {"628123@s.whatsapp.net": "Budi"}, {"999": "628123"},
                "", "120@g.us",
            ), "Budi"
        )


class TestSenders(unittest.TestCase):
    def test_full_sender_phone(self):
        self.assertEqual(
            rc.full_sender("1@s.whatsapp.net", "6281"), "6281@s.whatsapp.net"
        )

    def test_full_sender_lid(self):
        self.assertEqual(rc.full_sender("1@lid", "99"), "99@lid")

    def test_full_sender_keeps_jid(self):
        self.assertEqual(rc.full_sender("1@g.us", "99@lid"), "99@lid")


class TestFilenames(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(rc.safe_filename("my file (1).pdf"), "my_file__1_.pdf")

    def test_safe_filename_traversal(self):
        self.assertEqual(rc.safe_filename("../../etc/passwd"), "passwd")

    def test_safe_filename_empty(self):
        self.assertEqual(rc.safe_filename("", "x.bin"), "x.bin")

    def test_extension_lower(self):
        self.assertEqual(rc.extension("A.JPG"), "jpg")


class TestClassify(unittest.TestCase):
    def test_image(self):
        self.assertEqual(rc.classify_media("a.jpg"), "image")

    def test_video(self):
        self.assertEqual(rc.classify_media("a.mp4"), "video")

    def test_audio(self):
        self.assertEqual(rc.classify_media("a.ogg"), "audio")

    def test_document(self):
        self.assertEqual(rc.classify_media("a.pdf"), "document")

    def test_executable(self):
        self.assertEqual(rc.classify_media("a.apk"), "executable")

    def test_unknown(self):
        self.assertEqual(rc.classify_media("a.xyz"), "file")


class TestMediaAllowed(unittest.TestCase):
    def test_too_large(self):
        ok, reason = rc.media_allowed("a.jpg", "image", 9 * 1024 * 1024)
        self.assertFalse(ok)
        self.assertIn("melebihi", reason)

    def test_executable_rejected(self):
        ok, reason = rc.media_allowed("a.exe", "document", 10)
        self.assertFalse(ok)
        self.assertIn("executable", reason)

    def test_image_mismatch(self):
        ok, _ = rc.media_allowed("a.exe", "image", 10)
        self.assertFalse(ok)

    def test_valid_image(self):
        ok, reason = rc.media_allowed("a.jpg", "image", 1024)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_document_exec_rejected(self):
        ok, _ = rc.media_allowed("run.sh", "document", 10)
        self.assertFalse(ok)


class TestAttachments(unittest.TestCase):
    def test_count_limit(self):
        items = [("a.jpg", 10)] * 6
        ok, reason, _ = rc.validate_attachments(items)
        self.assertFalse(ok)
        self.assertIn("maksimal", reason)

    def test_total_limit(self):
        items = [("a.jpg", 5 * 1024 * 1024), ("b.jpg", 4 * 1024 * 1024)]
        ok, reason, _ = rc.validate_attachments(items)
        self.assertFalse(ok)
        self.assertIn("total", reason)

    def test_executable_rejected(self):
        ok, reason, _ = rc.validate_attachments([("a.exe", 10)])
        self.assertFalse(ok)
        self.assertIn("executable", reason)

    def test_ok(self):
        items = [("a.jpg", 10), ("b.pdf", 20)]
        ok, reason, accepted = rc.validate_attachments(items)
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(accepted, items)


class TestText(unittest.TestCase):
    def test_split_short(self):
        self.assertEqual(rc.split_message("hi"), ["hi"])

    def test_split_empty(self):
        self.assertEqual(rc.split_message(""), [])

    def test_split_long(self):
        text = "\n".join("line %d" % i for i in range(500))
        chunks = rc.split_message(text)
        self.assertTrue(all(len(c) <= rc.MAX_TEXT for c in chunks))
        self.assertGreater(len(chunks), 1)

    def test_quote_preview_collapses(self):
        self.assertEqual(rc.quote_preview("a\n\n b"), "a b")

    def test_quote_preview_truncates(self):
        out = rc.quote_preview("x" * 300, 50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))

    def test_format_incoming(self):
        self.assertEqual(
            rc.format_relay_message("Budi", "halo"), "**Budi**\nhalo"
        )

    def test_format_outgoing(self):
        self.assertEqual(rc.format_relay_message("Budi", "halo", True), "**You**\nhalo")

    def test_format_media_note(self):
        self.assertIn("*[image]*", rc.format_relay_message("Budi", "", media_type="image"))

    def test_format_quote(self):
        out = rc.format_relay_message("Budi", "iya", quote="pesan lama")
        self.assertIn("> pesan lama", out)


class TestTime(unittest.TestCase):
    def test_parse_iso(self):
        dt = rc.parse_ts("2026-01-02T03:04:05+00:00")
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, UTC)

    def test_parse_naive(self):
        dt = rc.parse_ts("2026-01-02 03:04:05")
        self.assertEqual(dt.tzinfo, UTC)

    def test_parse_invalid(self):
        self.assertIsNone(rc.parse_ts("not a date"))

    def test_within_days_true(self):
        now = datetime.datetime(2026, 1, 10, tzinfo=UTC)
        ts = now - datetime.timedelta(days=1)
        self.assertTrue(rc.within_days(ts, now, 7))

    def test_within_days_false(self):
        now = datetime.datetime(2026, 1, 10, tzinfo=UTC)
        ts = now - datetime.timedelta(days=8)
        self.assertFalse(rc.within_days(ts, now, 7))

    def test_within_days_zero_allows(self):
        now = datetime.datetime(2026, 1, 10, tzinfo=UTC)
        ts = now - datetime.timedelta(days=100)
        self.assertTrue(rc.within_days(ts, now, 0))


class TestReactions(unittest.TestCase):
    def test_unicode_ok(self):
        self.assertTrue(rc.reaction_emoji_ok("👍"))

    def test_empty_not_ok(self):
        self.assertFalse(rc.reaction_emoji_ok(""))

    def test_custom_name_not_ok(self):
        self.assertFalse(rc.reaction_emoji_ok(":blob:"))

    def test_skip_own(self):
        self.assertFalse(rc.should_mirror_reaction(True))

    def test_mirror_incoming(self):
        self.assertTrue(rc.should_mirror_reaction(False))

    def test_dedupe_key(self):
        self.assertEqual(rc.dedupe_key("a", "b"), "a|b")


class TestPermissions(unittest.TestCase):
    def test_default_denied(self):
        spec = rc.permission_spec("1", "2", [])
        self.assertIn("view_channel", spec["default"]["deny"])

    def test_owner_allowed(self):
        spec = rc.permission_spec("1", "2", [])
        self.assertIn("view_channel", spec["users"]["1"])

    def test_bot_allowed(self):
        spec = rc.permission_spec("1", "2", [])
        self.assertIn("manage_threads", spec["users"]["2"])

    def test_extra_user_allowed(self):
        spec = rc.permission_spec("1", "2", ["3"])
        self.assertIn("send_messages", spec["users"]["3"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
