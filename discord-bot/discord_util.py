"""Discord-side reliability helpers: token bucket + retry/backoff.

Discord.py already retries internally for most 429s, but a rebuild can issue
hundreds of deletes/sends at once. A global token bucket keeps the relay under
the REST limit; safe_call adds bounded retries with Retry-After + jitter.
"""

import asyncio
import logging
import random

import discord

log = logging.getLogger("wa-discord")


class TokenBucket:
    def __init__(self, rate_per_sec):
        self.rate = max(1.0, float(rate_per_sec or 1))
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, amount=1.0):
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self.updated == 0.0:
                self.updated = now
            elapsed = max(0.0, now - self.updated)
            self.updated = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens < amount:
                wait = (amount - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0.0
                self.updated = asyncio.get_running_loop().time()
            else:
                self.tokens -= amount


class DiscordIO:
    def __init__(self, rate_per_sec=4.0, max_retries=6):
        self.bucket = TokenBucket(rate_per_sec)
        self.max_retries = max(1, int(max_retries))

    async def call(self, factory):
        """Run factory() (returning an awaitable) with rate limiting/retries.

        Only 429 and 5xx responses are retried. Other 4xx responses
        (400, 401, 403, 404, ...) fail immediately: retrying them cannot
        succeed and only hammers the API.
        """
        last = None
        for attempt in range(self.max_retries):
            await self.bucket.acquire()
            try:
                return await factory()
            except discord.RateLimited as exc:
                # Pre-emptive local rate limit; also a 429 signal.
                last = exc
                wait = float(getattr(exc, "retry_after", 1.0) or 1.0)
                wait += random.uniform(0.05, 0.4)
                log.warning("discord rate limited, retry %d/%d in %.2fs",
                            attempt + 1, self.max_retries, wait)
            except discord.HTTPException as exc:
                status = getattr(exc, "status", 0) or 0
                if status == 429:
                    last = exc
                    wait = float(getattr(exc, "retry_after", 1.0) or 1.0)
                    wait += random.uniform(0.05, 0.4)
                elif 500 <= status < 600:
                    last = exc
                    wait = min(30.0, (2 ** attempt) + random.uniform(0, 0.5))
                else:
                    # Non-retryable 4xx (and anything unexpected): raise now.
                    raise
                log.warning("discord %s, retry %d/%d in %.2fs: %s",
                            status, attempt + 1, self.max_retries, wait, exc)
            await asyncio.sleep(wait)
        if last is not None:
            raise last

    async def send(self, target, content=None, **kwargs):
        if content is not None:
            kwargs.setdefault("content", content)
        return await self.call(lambda: target.send(**kwargs))

    async def reply(self, message, content=None, **kwargs):
        if content is not None:
            kwargs.setdefault("content", content)
        return await self.call(lambda: message.reply(**kwargs))

    async def edit(self, message, **kwargs):
        return await self.call(lambda: message.edit(**kwargs))

    async def delete(self, message):
        return await self.call(lambda: message.delete())

    async def add_reaction(self, message, emoji):
        return await self.call(lambda: message.add_reaction(emoji))

    async def remove_reaction(self, message, emoji, user):
        return await self.call(lambda: message.remove_reaction(emoji, user))
