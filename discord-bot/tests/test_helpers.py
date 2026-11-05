"""Unit tests for sender_display LID placeholders and discord_util retries.

No network is used: the Discord REST calls are faked via the factory
callable, and sleeps are recorded instead of awaited.
"""

import asyncio
import types
import unittest
from unittest import mock

import relay_core as rc
import discord
from discord_util import DiscordIO


def http_exc(status, retry_after=None):
    resp = types.SimpleNamespace(status=status, reason="X", headers={})
    exc = discord.HTTPException(resp, "boom")
    if retry_after is not None:
        exc.retry_after = retry_after
    return exc


class Failer:
    """Callable that raises `exc` until it reaches `succeed_at` attempts."""

    def __init__(self, exc, succeed_at=None):
        self.exc = exc
        self.succeed_at = succeed_at
        self.attempts = 0

    async def __call__(self):
        self.attempts += 1
        if self.succeed_at is not None and self.attempts >= self.succeed_at:
            return "ok"
        raise self.exc


class MockSleep:
    """Records asyncio.sleep durations instead of sleeping."""

    def __init__(self):
        self.sleeps = []
        self._patcher = mock.patch("discord_util.asyncio.sleep", self._sleep)

    async def _sleep(self, seconds):
        self.sleeps.append(seconds)

    def __enter__(self):
        self._patcher.start()
        return self

    def __exit__(self, *exc_info):
        self._patcher.stop()
        return False


def _run(coro):
    return asyncio.run(coro)


class TestSenderDisplayPlaceholder(unittest.TestCase):
    """Unknown LID senders must never render as raw numbers."""

    def test_unknown_lid_in_group(self):
        self.assertEqual(
            rc.sender_display("999901234@lid", {}, {}, "", "120345678@g.us"),
            "Anggota grup",
        )

    def test_unknown_bare_lid_in_group(self):
        self.assertEqual(
            rc.sender_display("999901234", {}, {}, "", "120345678@g.us"),
            "Anggota grup",
        )

    def test_unknown_lid_in_dm(self):
        self.assertEqual(
            rc.sender_display("999901234@lid", {}, {}, "", "999901234@lid"),
            "Kontak",
        )

    def test_saved_lid_name_wins(self):
        self.assertEqual(
            rc.sender_display(
                "999901234@lid", {"999901234@lid": "Sinta"}, {}, "", "120@g.us"
            ),
            "Sinta",
        )

    def test_lid_phone_fallback_still_works(self):
        self.assertEqual(
            rc.sender_display("999901234@lid", {}, {"999901234": "+628123"},
                              "", "120@g.us"),
            "+628123",
        )

    def test_lid_resolved_via_contact_phone_name(self):
        self.assertEqual(
            rc.sender_display(
                "999901234@lid",
                {"+628123@s.whatsapp.net": "Budi"},
                {"999901234": "+628123"},
                "", "120@g.us",
            ),
            "Budi",
        )

    def test_push_name_wins(self):
        self.assertEqual(
            rc.sender_display("999901234@lid", {}, {}, "Cecep", "120@g.us"),
            "Cecep",
        )

    def test_saved_phone_jid_name_wins(self):
        self.assertEqual(
            rc.sender_display(
                "628123", {"628123@s.whatsapp.net": "Budi"}, {}, "", "120@g.us"
            ),
            "Budi",
        )

    def test_bare_numeric_without_chat_unaffected(self):
        # Regression guard for the historical behavior.
        self.assertEqual(rc.sender_display("6281", {}, {}), "6281")


class TestDiscordIORetry(unittest.TestCase):
    RETRIES = 3

    def _io(self):
        return DiscordIO(rate_per_sec=1000, max_retries=self.RETRIES)

    def test_non_retryable_4xx_raise_immediately(self):
        for status in (400, 401, 403, 404, 413):
            d = self._io()
            failer = Failer(http_exc(status))
            with MockSleep() as ms:
                with self.assertRaises(discord.HTTPException) as ctx:
                    _run(d.call(failer))
            self.assertIn("boom", str(ctx.exception))
            self.assertEqual(ms.sleeps, [])  # never retried
            self.assertEqual(failer.attempts, 1)

    def test_5xx_retries_with_backoff_then_raises(self):
        d = self._io()
        failer = Failer(http_exc(500))
        with MockSleep() as ms:
            with self.assertRaises(discord.HTTPException) as ctx:
                _run(d.call(failer))
        self.assertIn("boom", str(ctx.exception))
        self.assertEqual(failer.attempts, self.RETRIES)
        self.assertEqual(len(ms.sleeps), self.RETRIES)
        for attempt, wait in enumerate(ms.sleeps):
            self.assertGreaterEqual(wait, 2 ** attempt)
            self.assertLessEqual(wait, (2 ** attempt) + 0.5 + 1e-9)
        self.assertLessEqual(ms.sleeps[-1], 30.0)

    def test_429_retries_with_retry_after_and_jitter(self):
        d = self._io()
        failer = Failer(http_exc(429, retry_after=2.6))
        with MockSleep() as ms:
            with self.assertRaises(discord.HTTPException):
                _run(d.call(failer))
        self.assertEqual(failer.attempts, self.RETRIES)
        for wait in ms.sleeps:
            self.assertGreaterEqual(wait, 2.6)
            self.assertLessEqual(wait, 2.6 + 0.4 + 1e-9)

    def test_preemptive_ratelimited_retried(self):
        d = self._io()
        failer = Failer(discord.RateLimited(1.5))
        with MockSleep() as ms:
            with self.assertRaises(discord.RateLimited):
                _run(d.call(failer))
        self.assertEqual(failer.attempts, self.RETRIES)
        for wait in ms.sleeps:
            self.assertGreaterEqual(wait, 1.55)
            self.assertLessEqual(wait, 1.5 + 0.4 + 1e-9)

    def test_5xx_recovers_after_two_failures(self):
        d = self._io()
        failer = Failer(http_exc(503), succeed_at=3)
        with MockSleep():
            result = _run(d.call(failer))
        self.assertEqual(result, "ok")
        self.assertEqual(failer.attempts, 3)

    def test_non_discord_exception_propagates(self):
        d = self._io()
        failer = Failer(ValueError("nope"))
        with self.assertRaises(ValueError):
            _run(d.call(failer))


if __name__ == "__main__":
    unittest.main()
