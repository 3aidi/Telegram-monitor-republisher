"""Shared destination-publish throttling + lock.

Both `main.py` (auto-publish + approved-listings worker) and `admin_bot.py`
(admin approve in unified mode) publish to the same destination channel. This
module gives them ONE lock and ONE rate limiter so publishes are serialized:
two coroutines can never fire closer together than PUBLISH_INTERVAL, and an
admin-triggered publish is throttled against a worker publish (CONC-1/CONC-3).
"""

import asyncio
import logging
import os
import time
from typing import Optional

from telethon.errors import FloodWaitError

PUBLISH_INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "1.5") or 1.5)

logger = logging.getLogger("publish_guard")

_publish_lock = asyncio.Lock()
_last_publish_ts = 0.0


def set_publish_interval(seconds: float) -> None:
    """Override the throttle interval (main.py owns the single source of truth)."""
    global PUBLISH_INTERVAL
    PUBLISH_INTERVAL = float(seconds)


async def throttle() -> None:
    """Serialize and rate-limit any publish to the destination channel."""
    global _last_publish_ts
    async with _publish_lock:
        now = time.monotonic()
        wait = PUBLISH_INTERVAL - (now - _last_publish_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_publish_ts = time.monotonic()


async def run_with_floodwait_retry(
    coro_factory, context: str, max_total_sleep: Optional[float] = None
):
    """Await a coroutine factory, transparently honoring FloodWaitError (TEL-1).

    This is the SINGLE FloodWait retry mechanism shared by main.py and the
    admin bot so edit/repair/approve paths behave like the successful
    auto-publish path instead of each implementing its own retry.

    ``coro_factory`` takes no args and returns a coroutine (a fresh call must
    be possible after every flood sleep). FloodWaitError is honored by sleeping
    the requested amount and retrying. ``max_total_sleep`` bounds the TOTAL
    sleep budget — interactive admin actions that would otherwise hang the bot
    loop indefinitely raise the last FloodWaitError once the budget is spent
    so the caller can fail through (e.g. re-queue for the worker). When it is
    None the retry loop is unbounded (pipeline behavior).
    """
    total_slept = 0.0
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as fwe:
            wait = max(float(getattr(fwe, "seconds", 1) or 1), 1)
            if max_total_sleep is not None and total_slept + wait > max_total_sleep:
                logger.error(
                    "FloodWait budget exhausted after %ss of sleeping during %s; giving up.",
                    total_slept,
                    context,
                )
                raise
            logger.warning(
                "FloodWait(%ss) hit during %s — sleeping before retry.",
                wait,
                context,
            )
            await asyncio.sleep(wait)
            total_slept += wait