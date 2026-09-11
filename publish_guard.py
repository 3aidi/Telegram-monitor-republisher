"""Shared destination-publish throttling + lock.

Both `main.py` (auto-publish + approved-listings worker) and `admin_bot.py`
(admin approve in unified mode) publish to the same destination channel. This
module gives them ONE lock and ONE rate limiter so publishes are serialized:
two coroutines can never fire closer together than PUBLISH_INTERVAL, and an
admin-triggered publish is throttled against a worker publish (CONC-1/CONC-3).
"""

import asyncio
import os
import time

PUBLISH_INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "1.5") or 1.5)

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