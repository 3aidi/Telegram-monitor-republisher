"""Telegram Channel Monitor & Auto-Republisher — Main Orchestrator.

Wires together Telethon user client, SQLite persistence, parser,
keyword blocklist, duplicate detection, and admin approval bot.
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatIdInvalidError,
    ChatWriteForbiddenError,
    PeerIdInvalidError,
    UserBannedInChannelError,
    UserKickedError,
    UsernameNotOccupiedError,
)

import admin_bot
import ai_rephraser
import db
import filters
import parser
import publish_guard

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration from .env
# ---------------------------------------------------------------------------
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
DEST_CHANNEL = os.environ.get("DEST_CHANNEL", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0)
CONTACT_USERNAME = os.environ.get("CONTACT_USERNAME", "")
PUBLISH_INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "1.5"))
PUBLISH_MAX_RETRIES = int(os.environ.get("PUBLISH_MAX_RETRIES", "4"))
# F3: once a listing is claimed ('publishing'), how long before a crashed/stuck
# claim is considered abandoned. The approved-listings worker then verifies the
# destination channel and either records the (actually-landed) message or
# releases the claim for a clean retry.
PUBLISH_CLAIM_GRACE_SECONDS = int(
    os.environ.get("PUBLISH_CLAIM_GRACE_SECONDS", "300") or 300
)
BACKFILL_ON_START = os.environ.get("BACKFILL_ON_START", "0").strip().lower() in ("1", "true", "yes")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# Free-tier reliability toggles: the chatter pre-filter saves AI quota, and the
# deterministic fallback keeps real listings publishing when every AI path is down.
PRE_FILTER_CHATTER = os.environ.get("PRE_FILTER_CHATTER", "1").strip().lower() in ("1", "true", "yes")
DETERMINISTIC_FALLBACK = os.environ.get("DETERMINISTIC_FALLBACK", "1").strip().lower() in ("1", "true", "yes")
AI_CACHE_TTL_HOURS = float(os.environ.get("AI_CACHE_TTL_HOURS", "48") or 48)
# How long identical content stays blocked as a duplicate: a re-post of the
# same listing within DEDUP_HOURS is skipped (content fingerprint + price).
DEDUP_HOURS = int(os.environ.get("DEDUP_HOURS", "48") or 48)
# Startup stale-body rephrase sweep (REPHR-1): bounded to a small batch of
# listings older than a few minutes, so restart recovery never becomes an
# unbounded AI batch and never races messages the live pipeline is still
# holding in-flight. AI results are cached per fingerprint anyway (ai_cache).
REPHRASE_SWEEP_LIMIT = int(os.environ.get("REPHRASE_SWEEP_LIMIT", "50") or 50)
REPHRASE_SWEEP_MIN_AGE_SECONDS = int(
    os.environ.get("REPHRASE_SWEEP_MIN_AGE_SECONDS", "120") or 120
)
# Destination forwarding (DEST-1): the worker runs independently of the publish
# path so a slow/flooded destination never slows main-channel publishing. The
# flood budget bounds how long ONE destination may sleep before the worker
# defers it (marked transient-failed with backoff) and moves on to the rest.
FORWARD_FLOODWAIT_BUDGET_SECONDS = float(
    os.environ.get("FORWARD_FLOODWAIT_BUDGET_SECONDS", "60") or 60
)
FORWARD_WORKER_INTERVAL = float(os.environ.get("FORWARD_WORKER_INTERVAL", "3") or 3)

# Errors that will never succeed on retry: reporting them as the queue's final
# state (failed) beats grinding retries forever (DEST-1).
PERMANENT_FORWARD_ERRORS = (
    ChatWriteForbiddenError,
    ChatAdminRequiredError,
    UserBannedInChannelError,
    UserKickedError,
    ChatIdInvalidError,
    PeerIdInvalidError,
    UsernameNotOccupiedError,
    ChannelInvalidError,
    ChannelPrivateError,
)

# Runtime health (MON-1): process-level state surfaced in the periodic health
# line so a silently-dead worker / stalled publish path is visible in the logs
# with no dashboard and no extra dependencies. Times are monotonic seconds.
_RUNTIME_STARTED_AT = time.monotonic()
_LAST_ACTIVITY_AT: Optional[float] = None
_LAST_PUBLISH_AT: Optional[float] = None
_LAST_FAILURE: Optional[Tuple[float, str]] = None
_WORKER_HEARTBEATS: Dict[str, float] = {}


def _mark_activity() -> None:
    global _LAST_ACTIVITY_AT
    _LAST_ACTIVITY_AT = time.monotonic()


def _mark_publish() -> None:
    global _LAST_PUBLISH_AT, _LAST_ACTIVITY_AT
    _LAST_PUBLISH_AT = time.monotonic()
    _LAST_ACTIVITY_AT = _LAST_PUBLISH_AT


def _record_failure(where: str, err: str) -> None:
    global _LAST_FAILURE
    _LAST_FAILURE = (time.monotonic(), f"{where}: {err[:200]}")


def _mark_worker_heartbeat(name: str) -> None:
    _WORKER_HEARTBEATS[name] = time.monotonic()


def _runtime_health_suffix() -> str:
    """Short summary of process-level liveness for the health log line."""
    upstream = max(
        [t for t in (_LAST_ACTIVITY_AT, _LAST_PUBLISH_AT) if t is not None], default=0
    )
    alive = (
        f"inbound-activity-{(time.monotonic() - upstream):.0f}s-ago"
        if upstream
        else "no-inbound-activity-yet"
    )
    last_pub = (
        f"last-publish-{(time.monotonic() - _LAST_PUBLISH_AT):.0f}s-ago"
        if _LAST_PUBLISH_AT
        else "no-publish-yet"
    )
    beats = ",".join(
        f"{name}-{(time.monotonic() - ts):.0f}s" for name, ts in sorted(_WORKER_HEARTBEATS.items())
    ) or "no-worker-heartbeats"
    failure = (
        f"last-failure={_LAST_FAILURE[1]}"
        if _LAST_FAILURE
        else "no-failures"
    )
    return (
        f" | uptime={(time.monotonic() - _RUNTIME_STARTED_AT) / 60:.0f}m | {alive} | "
        f"{last_pub} | beats: {beats} | {failure}"
    )
# Manual mode (`python main.py --manual`): start the admin bot + user client
# ONLY. No listener, no auto-publish, no backfill, no supplier resolution —
# the admin reviews and publishes everything. Approvals drain the queue worker.
MANUAL_MODE = "--manual" in sys.argv

# Parse source channels list from comma-separated string
SOURCE_CHANNELS_RAW = os.environ.get("SOURCE_CHANNELS", "")
SOURCE_CHANNELS = [
    ch.strip() for ch in SOURCE_CHANNELS_RAW.split(",") if ch.strip()
]

SESSION_NAME = "monitor_session"
LOG_FILE = "monitor.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("monitor")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Route admin_bot logs to file and console (also fixes the admin_bot INFO level)
admin_bot_logger = logging.getLogger("admin_bot")
admin_bot_logger.setLevel(logging.INFO)
admin_bot_logger.handlers = []
admin_bot_logger.addHandler(console_handler)
admin_bot_logger.addHandler(file_handler)

# Route ai_rephraser logs to file and console so AI failures (timeouts,
# empty returns, JSON validation errors) are visible in monitor.log.
ai_logger = logging.getLogger("ai_rephraser")
ai_logger.setLevel(logging.INFO)
ai_logger.handlers = []
ai_logger.addHandler(console_handler)
ai_logger.addHandler(file_handler)

# ---------------------------------------------------------------------------
# Single-instance guard (Windows named mutex)
# ---------------------------------------------------------------------------
# The venv launcher shim (venv\Scripts\python.exe) spawns the real base
# interpreter without ever executing this module's body, so the guard only runs
# inside the actual bot process. It therefore never falsely flags the normal
# shim -> interpreter pair as a duplicate.
_INSTANCE_MUTEX_NAME = "AidikycTeleMonitorRepublisher_1"
_instance_mutex_handle = None


def arm_single_instance_guard() -> None:
    """Exit immediately if another real bot instance is already running."""
    global _instance_mutex_handle
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ERROR_ALREADY_EXISTS = 183
        _instance_mutex_handle = kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            logger.error(
                "Another monitor instance is already running. "
                "This duplicate launch will exit to avoid double-processing."
            )
            raise SystemExit(1)
        logger.info("Single-instance guard armed (only one bot may run).")
    except (ImportError, AttributeError, OSError):
        logger.warning("Single-instance guard unavailable; continuing without it.")


class PublishError(Exception):
    """Raised when a listing could not be published after all retries."""


# All destination publishes (auto-publish, worker, admin-approve) go through
# publish_guard.throttle() so they are serialized + rate-limited by ONE lock.
# The legacy module-level _publish_lock/_throttle_publish were removed in favor
# of publish_guard (fixes CONC-1: the lock was defined but never used).


def _validate_config() -> None:
    """Fail fast with clear messages when critical env vars are missing."""
    errors: List[str] = []
    publish_guard.set_publish_interval(PUBLISH_INTERVAL)
    if not API_ID:
        errors.append("API_ID is missing/0 (get it from https://my.telegram.org)")
    if not API_HASH:
        errors.append("API_HASH is missing in .env")
    if not DEST_CHANNEL:
        errors.append("DEST_CHANNEL is missing in .env")
    # PRIC-1: no price multiplier exists anymore; nothing to validate.
    if errors:
        for err in errors:
            logger.error("Configuration error: %s", err)
        raise SystemExit(
            "Fatal: critical environment configuration missing. Fix .env and restart."
        )

    warnings = []
    if not BOT_TOKEN:
        warnings.append("BOT_TOKEN missing — admin bot disabled (auto-publish still works)")
    if not ADMIN_USER_ID:
        warnings.append("ADMIN_USER_ID missing — approval prompts won't be delivered")
    if not CONTACT_USERNAME:
        warnings.append("CONTACT_USERNAME missing — posts will ship without an order line")
    for w in warnings:
        logger.warning("Configuration warning: %s", w)


def _supplier_label(supplier: dict) -> str:
    """Human-friendly supplier label for logs: display_name when available,
    else the username, else the raw numeric id."""
    display = (supplier.get("display_name") or "").strip()
    username = (supplier.get("channel_username") or "").strip()
    if display:
        if db.is_numeric_identifier(display):
            return f"id {display}"
        return f"@{display}" if not display.startswith("@") else display
    if username:
        if db.is_numeric_identifier(username):
            return f"id {username}"
        return f"@{username}" if not username.startswith("@") else username
    return "?"


_self_echo_pattern = None


def _build_self_echo_pattern() -> Optional[re.Pattern]:
    """Regex matching our own destination footer in a re-shared message.

    The footer is ``Contact  : @<CONTACT_USERNAME>``; a re-share can reformat
    it (extra spaces, case, '@' dropped), so the old exact-string match was
    brittle enough to let an echo slip through and be re-published. The pattern
    tolerates spacing/case/@-prefix while still anchoring on the word 'contact'
    so it cannot misfire on unrelated posts.
    """
    global _self_echo_pattern
    if _self_echo_pattern is None and CONTACT_USERNAME:
        user = CONTACT_USERNAME.strip().lstrip("@")
        if user:
            _self_echo_pattern = re.compile(
                rf"contact\s*:\s*@?{re.escape(user)}", re.IGNORECASE
            )
    return _self_echo_pattern


def deterministic_fallback_publish_ok(
    paused: bool,
    risky_keyword: Optional[str],
    has_payment_proof: Optional[str],
    has_clear_signal: bool,
    body_ok: bool,
) -> bool:
    """Gate the deterministic AI-down fallback auto-publish.

    During an AI outage a concrete listing with a substantive body routes
    straight to the channel — but only when publishing is not paused via the
    admin "All Stop" switch. Unpaused, a risky/payment-proof/weak body still
    falls through to manual review. Paused, EVERY fallback listing routes to
    manual review instead of auto-publishing.
    """
    return (
        DETERMINISTIC_FALLBACK
        and not paused
        and not risky_keyword
        and not has_payment_proof
        and has_clear_signal
        and body_ok
    )


def buy_auto_publish_ok(
    intent: str,
    paused: bool,
    body_ok: bool,
    has_payment_proof: Optional[str],
) -> bool:
    """Gate the buy-intent auto-publish.

    A buy demand auto-publishes when it has a substantive sanitized body and no
    payment-proof signal — prices and platforms never factor into the decision.
    "All Stop" gates it too: while paused, buy signals route to manual approval
    instead of auto-publishing (the admin's Approve is still honored).
    """
    return (
        intent == "buy"
        and not paused
        and body_ok
        and not has_payment_proof
    )


def zero_resolved_suppliers_alert_text(active_total: int, resolved_ok: int) -> Optional[str]:
    """Return a loud admin alert when suppliers exist but NONE resolved.

    A bot that has configured suppliers yet zero resolvable channel ids is
    technically running while monitoring NOTHING — a silent failure distinct from
    a crash. active_total is the number of active supplier rows, resolved_ok the
    number that actually carry a channel_id. Fires only when something IS
    configured but nothing resolved (0 configured is a valid calm state).
    """
    if active_total > 0 and resolved_ok == 0:
        return (
            f"🚨 **0 of {active_total} suppliers resolved — the bot is running but "
            f"MONITORING NOTHING.** Check that the account is still in the source "
            f"channels (kicked? deleted? logged out?) or fix the sources in the "
            f"admin bot's Sources menu."
        )
    return None


async def _warn_if_zero_suppliers_resolved(
    bot_client: Optional[TelegramClient],
    active_total: int,
    resolved_ok: int,
) -> Optional[str]:
    """Startup watchdog: log clearly and DM the admin when nothing resolved."""
    alert_text = zero_resolved_suppliers_alert_text(active_total, resolved_ok)
    if not alert_text:
        return None
    logger.error("%s", alert_text)
    if bot_client and ADMIN_USER_ID:
        try:
            await bot_client.send_message(ADMIN_USER_ID, alert_text)
        except Exception:
            logger.exception("Could not DM admin about 0-resolved suppliers")
    return alert_text


async def resolve_supplier_entities(
    client: TelegramClient, only_unresolved: bool = False
) -> List[Dict[str, Any]]:
    """
    Resolve supplier identities so events match on chat_id even when a
    channel has no username.

    - Username-based suppliers are resolved to numeric channel ids.
    - Numeric-id suppliers are validated against the live chat and get a friendly
      display_name (username preferred, else channel/group title). A failed lookup
      is logged LOUDLY so stale IDs (deleted channel / you left the group) are
      discovered instead of monitoring silently doing nothing.
    - When a row that was stored without a channel_id finally resolves to one
      already owned by a different row, the two are MERGED into that owner instead
      of racing the UNIQUE(channel_id) constraint and creating a duplicate.

    Returns the freshly-resolved rows (with their final channel_id).
    """
    if only_unresolved:
        rows = db.get_unresolved_suppliers(active_only=True)
    else:
        rows = db.list_suppliers(active_only=True)
    resolved: List[Dict[str, Any]] = []
    for supplier in rows:
        channel_id = supplier.get("channel_id")
        username = supplier.get("channel_username")
        reference = channel_id if channel_id is not None else username
        if reference is None:
            continue
        try:
            entity = await client.get_entity(reference)
        except asyncio.CancelledError:
            raise
        except Exception:
            if channel_id is not None:
                logger.warning(
                    "Configured source ID %s did NOT resolve: the channel/group may "
                    "have been deleted, or your account left it. Verify/remove it in "
                    ".env SOURCE_CHANNELS or the admin Sources menu.",
                    channel_id,
                )
            else:
                logger.debug(
                    "Could not resolve supplier entity @%s yet (channel may be private). "
                    "Only events matched by chat_id will be processed.",
                    username,
                )
            continue

        entity_id = db.normalize_channel_id(entity)
        if entity_id is None:
            continue

        display = getattr(entity, "username", None) or getattr(entity, "title", None)
        if display:
            await db.run_async(
                db.set_supplier_display_name,
                channel_id if channel_id is not None else username,
                display,
            )

        real_owner = db.get_supplier_by_chat(chat_id=entity_id, username=None)
        if real_owner is not None and real_owner["id"] != supplier["id"]:
            await db.run_async(db.merge_supplier_rows, real_owner["id"], supplier["id"])
            resolved.append({**real_owner, "channel_id": entity_id})
            logger.info(
                "Supplier %s resolved into existing row id %s (%s)",
                _supplier_label({**supplier, "display_name": None}),
                real_owner["id"],
                display,
            )
            continue

        if channel_id is None:
            await db.run_async(db.set_supplier_channel_id, username, entity_id)
            channel_id = entity_id
        elif channel_id != entity_id:
            await db.run_async(
                db.set_supplier_channel_id, username or str(channel_id), entity_id
            )
            channel_id = entity_id

        resolved.append({**supplier, "channel_id": channel_id})
        logger.info(
            "Resolved source %s -> %s (id %s)",
            _supplier_label({**supplier, "display_name": None}),
            display or "?",
            entity_id,
        )
    return resolved


async def supplier_resolution_worker(
    client: TelegramClient,
    bot_client: Optional[TelegramClient],
    stop_event: asyncio.Event,
    interval: int = 300,
) -> None:
    """
    Periodic self-healing for suppliers that could not be resolved yet.

    A private channel added by username while the account has not joined it stays
    unresolved (no channel_id); the sources menu shows a ⚠️ so it is never silently
    ignored. This loop retries the resolve every `interval` seconds and DMs the
    admin the moment a source finally resolves, so nobody quietly depends on a
    dead source.
    """
    while not stop_event.is_set():
        try:
            resolved = await resolve_supplier_entities(client, only_unresolved=True)
            fresh = [s for s in resolved if s.get("channel_id") is not None]
            for s in fresh:
                if not (bot_client and ADMIN_USER_ID):
                    break
                try:
                    await bot_client.send_message(
                        ADMIN_USER_ID,
                        f"🟢 Self-healed source `{_supplier_label(s)}` — now monitoring "
                        f"it again (id `{s['channel_id']}`).",
                    )
                except Exception:
                    logger.exception(
                        "Could not DM admin about self-healed supplier %s",
                        _supplier_label(s),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Supplier resolution loop crashed (will retry next tick)")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            pass


async def resolve_chat(client: TelegramClient, supplier: dict):
    """Get the TDLib/Telethon entity for a supplier row."""
    if supplier.get("channel_id"):
        return await client.get_entity(supplier["channel_id"])
    return await client.get_entity(supplier["channel_username"])


async def resolve_supplier_for_event(event) -> Optional[dict]:
    """Find the active supplier matching this message event."""
    chat_id = event.chat_id
    chat_username = getattr(event.chat, "username", None)
    supplier = db.get_supplier_by_chat(chat_id=chat_id, username=chat_username)

    # If supplier was stored without channel_id, update it now
    if supplier and supplier.get("channel_id") is None and chat_id is not None:
        await db.run_async(
            db.set_supplier_channel_id, supplier["channel_username"], chat_id
        )
        supplier["channel_id"] = chat_id

    if supplier and supplier.get("active"):
        return supplier
    return None


# ---------------------------------------------------------------------------
# Publish helpers (retry + flood-wait + throttle)
# ---------------------------------------------------------------------------
async def publish_to_destination(
    client: TelegramClient, text: str, entities: list
) -> int:
    """
    Send a message to DEST_CHANNEL with retry/backoff.

    - Serialized + throttled through publish_guard (shared with admin_bot).
    - FloodWaitError is honored (sleeps the requested amount and continues).
    - Other failures retry up to PUBLISH_MAX_RETRIES with exponential backoff.
    - An ambiguous failure (a send that MAY have landed server-side before the
      error surfaced) is verified against the destination channel FIRST: an
      exact-text match returns that message id instead of resending, so a lost
      response can never produce a duplicate publication (F3 / NET-AMB).
    - Raises PublishError after exhausting retries (verification confirming the
      message is genuinely absent from the destination).
    """
    await publish_guard.throttle()
    delay = 3
    last_error = "unknown error"
    dest_peer = db.to_peer_reference(DEST_CHANNEL)
    for attempt in range(1, PUBLISH_MAX_RETRIES + 1):
        try:
            sent = await client.send_message(dest_peer, text, formatting_entities=entities)
            _mark_publish()
            return sent.id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)[:500]
            _record_failure("publish_to_destination", last_error)
            flood_seconds = getattr(exc, "seconds", 0) or 0
            if flood_seconds:
                # FloodWait = nothing was sent; safe to sleep and retry blindly.
                logger.warning(
                    "FloodWait %ss hit while publishing (attempt %d/%d); sleeping.",
                    flood_seconds,
                    attempt,
                    PUBLISH_MAX_RETRIES,
                )
                await asyncio.sleep(flood_seconds)
                continue
            # Ambiguous timeout/error: the message may have landed even though
            # the response was lost. Confirm against the channel before retrying.
            try:
                existing_id = await publish_guard.confirm_message_on_destination(
                    client, dest_peer, text
                )
            except asyncio.CancelledError:
                raise
            if existing_id is not None:
                logger.warning(
                    "Publish attempt %d/%d for listing reported %r but the message "
                    "was already on the destination (msg id %s) — not resending.",
                    attempt,
                    PUBLISH_MAX_RETRIES,
                    last_error,
                    existing_id,
                )
                _mark_publish()
                return existing_id
            logger.warning(
                "Publish attempt %d/%d failed (not on destination): %s",
                attempt,
                PUBLISH_MAX_RETRIES,
                last_error,
            )
            if attempt < PUBLISH_MAX_RETRIES:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    raise PublishError(last_error)


async def _alert_admin_on_failure(
    bot_client: Optional[TelegramClient], supplier: dict, listing_id: int
) -> None:
    """DM the admin when a listing moves to the failed queue (never silent)."""
    if not (bot_client and ADMIN_USER_ID):
        return
    try:
        listing_dict = db.get_listing_by_id(listing_id)
        if not listing_dict:
            return
        listing_dict["supplier_username"] = supplier.get("channel_username")
        listing_dict["supplier_display_name"] = supplier.get("display_name")
        await admin_bot.send_failed_alert(bot_client, ADMIN_USER_ID, listing_dict)
    except Exception:
        logger.exception("Failed to alert admin about failed listing #%s", listing_id)


async def _alert_admin_on_published(
    bot_client: Optional[TelegramClient], supplier: dict, listing_id: int
) -> None:
    """DM the admin a short confirmation for every auto-published post."""
    if not (bot_client and ADMIN_USER_ID):
        return
    try:
        listing_dict = db.get_listing_by_id(listing_id)
        if not listing_dict:
            return
        listing_dict["supplier_username"] = supplier.get("channel_username")
        listing_dict["supplier_channel_id"] = supplier.get("channel_id")
        listing_dict["supplier_display_name"] = supplier.get("display_name")
        await admin_bot.send_published_alert(bot_client, ADMIN_USER_ID, listing_dict)
    except Exception:
        logger.exception("Failed to notify admin about published listing #%s", listing_id)


# ---------------------------------------------------------------------------
# Destination forwarding (DEST-1)
# ---------------------------------------------------------------------------
# The ONLY way forwarding work is created is enqueue_destination_forwardings(),
# which is called exclusively from the successful-publication path — never from
# monitoring events in the main channel. That is what guarantees manually
# written channel messages can never be forwarded to destination groups.
async def enqueue_destination_forwardings(
    listing_id: int, published_msg_id: int
) -> int:
    """Persist one pending forward for every ACTIVE destination."""
    if not DEST_CHANNEL:
        return 0
    return await db.run_async(
        db.queue_forwarding, listing_id, DEST_CHANNEL, published_msg_id
    )


async def _confirm_forward_on_destination(
    client: TelegramClient, to_peer, expected_channel_post_id: int
) -> Optional[int]:
    """Check whether a forward of channel post ``expected_channel_post_id``
    already exists in ``to_peer`` (F6 forward-verification).

    Only messages published by the bot are ever forwarded, and
    ``fwd_from.channel_post`` is the per-channel sequence id of the ORIGINAL
    message, so an exact match is definitive proof that a previous forward
    attempt actually landed even if its response was lost. Returns the message
    id found, else None (including when the destination can't be scanned —
    callers must treat that as ambiguous, never as license to blindly resend).
    """
    try:
        messages = await client.get_messages(to_peer, limit=25)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Could not scan destination %r for forward confirmation", to_peer
        )
        return None
    for m in messages:
        fwd = getattr(m, "fwd_from", None)
        if fwd is None:
            continue
        if getattr(fwd, "channel_post", None) == expected_channel_post_id:
            return getattr(m, "id", None)
    return None


async def _drain_forward_queue(client: TelegramClient) -> None:
    """One pass over the pending destination-forward batch.

    Sequential (never a parallel forward storm), oldest-first, each destination
    isolated: one failing destination is marked and the next is still attempted.
    Uses the SAME FloodWait retry helper as publishing (TEL-1) with a bounded
    sleep budget so a long flood defers that destination instead of blocking
    the others. Rows are CLAIMED ('forwarding') before the external forward so
    two workers can never forward the same message, and an ambiguous failure is
    VERIFIED against the destination before it is retried or failed (F6).
    """
    pending = await db.run_async(db.get_pending_forwardings, 10)
    for row in pending:
        fwd_id = row["id"]
        to_peer = db.to_peer_reference(row["destination_chat_id"])
        from_peer = db.to_peer_reference(row["published_chat_id"])
        expected_post_id = int(row["published_message_id"])

        claimed = await db.run_async(db.claim_forwarding, fwd_id)
        if not claimed:
            logger.info(
                "Forward row #%s already claimed by another path; skipping.",
                fwd_id,
            )
            continue

        # A startup reset of a stale claim (crash between claim and forward)
        # marks the row so we VERIFY before forwarding: the previous attempt may
        # actually have landed, and a blind resend would duplicate it.
        if (row.get("error") or "") == "stale_claim_reset":
            already_there = await _confirm_forward_on_destination(
                client, to_peer, expected_post_id
            )
            if already_there is not None:
                await db.run_async(db.mark_forwarded, fwd_id)
                logger.warning(
                    "Forward row #%s (msg %s) was already forwarded before restart "
                    "(msg id %s) — recorded, not re-sent.",
                    fwd_id,
                    expected_post_id,
                    already_there,
                )
                continue

        async def _forward_call():
            try:
                return await client.forward_messages(
                    to_peer,
                    messages=[expected_post_id],
                    from_peer=from_peer,
                )
            except (ValueError, ChannelInvalidError) as res_err:
                # If the peer wasn't cached in this session yet (e.g. private group),
                # refresh dialogs to fetch entity access_hash and retry once.
                logger.info(
                    "Destination peer %s not resolved (%s); refreshing dialogs and retrying...",
                    row["destination_chat_id"],
                    res_err,
                )
                try:
                    await client.get_dialogs(limit=50)
                except Exception as diag_err:
                    logger.debug("Failed refreshing dialogs during forward retry: %s", diag_err)
                return await client.forward_messages(
                    to_peer,
                    messages=[expected_post_id],
                    from_peer=from_peer,
                )

        try:
            await publish_guard.run_with_floodwait_retry(
                _forward_call,
                f"forward listing #{row['listing_id']} "
                f"msg {expected_post_id} to {row['destination_chat_id']}",
                max_total_sleep=FORWARD_FLOODWAIT_BUDGET_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise_as_error = exc
            # Ambiguous failure: the forward may have landed before the error
            # surfaced (timeout/lost response). Verify BEFORE declaring failure,
            # otherwise the retry below would forward the message a second time.
            already_there = await _confirm_forward_on_destination(
                client, to_peer, expected_post_id
            )
            if already_there is not None:
                await db.run_async(db.mark_forwarded, fwd_id)
                logger.warning(
                    "Forward to %s reported %r but the message is already there "
                    "(msg id %s) — recorded, not re-sent.",
                    row["destination_chat_id"],
                    str(exc)[:200],
                    already_there,
                )
                continue
            permanent = isinstance(raise_as_error, PERMANENT_FORWARD_ERRORS)
            await db.run_async(db.mark_forward_failed, fwd_id, str(exc)[:500], permanent)
            logger.warning(
                "Forward to destination %s failed%s: %s",
                row["destination_chat_id"],
                " (permanent)" if permanent else "",
                exc,
            )
            continue
        await db.run_async(db.mark_forwarded, fwd_id)
        logger.info(
            "Forwarded bot post #%s (msg %s) to destination %s",
            row["listing_id"],
            row["published_message_id"],
            row["destination_chat_id"],
        )


async def forwarding_worker(
    client: TelegramClient, stop_event: asyncio.Event, interval: float = None
) -> None:
    """Background loop draining the persistent destination-forward queue.

    Runs independently of the publish path, so a temporarily unavailable
    destination never delays main-channel publishing. Pending rows live in
    SQLite, so a crash between 'published' and 'forwarded' recovers on restart
    by simply draining whatever is still pending.
    """
    interval = FORWARD_WORKER_INTERVAL if interval is None else interval
    while not stop_event.is_set():
        try:
            await _drain_forward_queue(client)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Destination forwarding worker crashed (will retry next tick)")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
# Per-message in-flight locks (CONC-2). Telegram handlers run concurrently and
# the same source message may be delivered twice; this serializes processing
# per (supplier_id, source_msg_id).
#
# Each entry carries a holder count (current holders + waiters). An entry is
# removed from the dict ONLY when the count drops to zero: a waiter that has
# already captured the entry object still runs against the same Lock, and a
# NEW caller can never get a fresh lock while another task is still using the
# key. This bounds memory without introducing a removal race (CONC-3).
class _ProcessingLockEntry:
    __slots__ = ("lock", "holders")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.holders = 0


_processing_locks: Dict[Tuple[int, int], _ProcessingLockEntry] = {}


def _acquire_processing_lock(supplier_id: int, source_msg_id: int) -> _ProcessingLockEntry:
    key = (supplier_id, source_msg_id)
    entry = _processing_locks.get(key)
    if entry is None:
        entry = _ProcessingLockEntry()
        _processing_locks[key] = entry
    entry.holders += 1
    return entry


def _release_processing_lock(
    supplier_id: int, source_msg_id: int, entry: _ProcessingLockEntry
) -> None:
    """Drop a holder/waiter reference; remove the entry once nobody is left.

    The check-and-pop runs without awaiting, so no other task can interleave:
    by the time the count reaches zero all processing for the key is done.
    """
    key = (supplier_id, source_msg_id)
    entry.holders -= 1
    if entry.holders == 0 and _processing_locks.get(key) is entry:
        del _processing_locks[key]


async def process_supplier_message(
    client: TelegramClient,
    bot_client: Optional[TelegramClient],
    supplier: dict,
    msg,
) -> None:
    """Serialized entry point for one message (event or backfill)."""
    if not msg:
        return
    source_msg_id = getattr(msg, "id", None)
    if source_msg_id is None:
        return
    entry = _acquire_processing_lock(supplier["id"], source_msg_id)
    try:
        async with entry.lock:
            try:
                await _process_supplier_message(client, bot_client, supplier, msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # ERR-1: an unexpected pipeline failure must never leave the listing
                # stuck as 'received' with no audit trail and no admin alert.
                try:
                    listing = db.get_listing_by_source(supplier["id"], source_msg_id)
                    if listing:
                        await db.run_async(
                            db.mark_listing_failed,
                            listing["id"],
                            f"pipeline error: {str(exc)[:500]}",
                        )
                        await db.run_async(
                            db.record_audit,
                            "pipeline_error_failed",
                            listing["id"],
                            detail=str(exc)[:500],
                        )
                        await _alert_admin_on_failure(bot_client, supplier, listing["id"])
                except Exception:
                    logger.exception(
                        "Could not mark/fail listing for source message %s", source_msg_id
                    )
                logger.exception(
                    "Pipeline error processing source message %s from supplier %s",
                    source_msg_id,
                    _supplier_label(supplier),
                )
                _record_failure("process_supplier_message", str(exc))
    finally:
        _mark_activity()
        _release_processing_lock(supplier["id"], source_msg_id, entry)


async def _process_supplier_message(
    client: TelegramClient,
    bot_client: Optional[TelegramClient],
    supplier: dict,
    msg,
) -> None:
    """Pipeline for handling one message from a monitored supplier (event or backfill)."""
    if not msg:
        return

    supplier_id = supplier["id"]
    source_msg_id = getattr(msg, "id", None)
    raw_text = msg.text or ""
    has_media = bool(getattr(msg, "media", None))

    # Media-only posts (screenshot/image ads with no caption) must never be
    # silently dropped: they are routed to manual review so the admin can see
    # them and type an Edit body if they are a real listing. Purely textless,
    # medialess noise (e.g. random emoji messages) is still dropped.
    if not source_msg_id:
        return
    if not raw_text.strip() and not has_media:
        return
    media_only = has_media and not raw_text.strip()
    if media_only:
        raw_text = "[📷 Media-only post — no text caption]"

    # Idempotency guard: if this source message is already recorded, ignore
    # re-deliveries so nothing is double-processed or double-published.
    existing = db.get_listing_by_source(supplier_id, source_msg_id)
    if existing:
        logger.debug(
            "Source message %s from supplier %s already processed (status=%s); ignoring.",
            source_msg_id,
            _supplier_label(supplier),
            existing.get("status"),
        )
        return

    # Self-echo guard: if our own formatted output was looped back into a source
    # channel, don't re-analyze/re-publish it. Its footer carries the destination
    # contact signature; skipping stops repricing compounding (e.g. $60 -> $45 -> $34)
    # and duplicate re-posts. This is a *containment* measure for re-shares of our
    # own posts, not a filter on real supplier ads. The footer match tolerates
    # spacing/case/@-prefix variations introduced by re-sharing.
    echo_pattern = _build_self_echo_pattern()
    if echo_pattern and echo_pattern.search(raw_text):
        logger.info(
            "Source message %s from %s looks like our own destination output; skipping",
            source_msg_id,
            _supplier_label(supplier),
        )
        await db.run_async(db.log_skip, supplier_id, source_msg_id, "self_echo", raw_text)
        return

    logger.info(
        "Incoming message %s from supplier %s (chat_id: %s)",
        source_msg_id,
        _supplier_label(supplier),
        getattr(msg.chat, "id", None) if getattr(msg, "chat", None) else None,
    )

    fallback_clean_text = parser.strip_all_emoji(raw_text)

    # Fingerprint is computed and stored with the row itself (CONC-2 race fix):
    # the dedup query reads fingerprints from the DB, so a concurrent identical
    # twin arriving right after this insert must be able to match this row while
    # it is still in-flight (status 'received').
    source_price, _ = parser.extract_price(raw_text)
    fingerprint = db.make_listing_fingerprint(raw_text, price=source_price)

    listing_id = await db.run_async(
        db.insert_listing,
        supplier_id=supplier_id,
        source_message_id=source_msg_id,
        game_name=None,
        rank_tier=None,
        status="received",
        raw_text=raw_text,
        clean_text=fallback_clean_text,
        fingerprint=fingerprint,
    )

    # Media-only posts have no analyzable text: skip fingerprint/AI entirely and
    # route straight to admin review (never silently dropped, never auto-published).
    if media_only:
        await db.run_async(db.update_listing_status, listing_id, "pending_review")
        await db.run_async(
            db.record_audit,
            "media_only_review",
            listing_id,
            detail="Media-only post (no caption) routed for manual review",
        )
        logger.info(
            "Listing #%s is a media-only post (no caption); routed to pending_review.",
            listing_id,
        )
        if bot_client and ADMIN_USER_ID:
            listing_dict = db.get_listing_by_id(listing_id)
            if listing_dict:
                listing_dict["supplier_username"] = supplier.get("channel_username")
                listing_dict["_review_reason"] = "media_only"
                await admin_bot.send_approval_prompt(
                    bot_client, ADMIN_USER_ID, listing_dict
                )
        return

    # Step 1: Content-based filter check FIRST — plain-text fingerprint
    # (normalized text + parsed price), no AI needed. Skip identical listings
    # already processed within DEDUP_HOURS; reason is logged to the skips
    # table so the daily report can break skipped stats down per rule (DEDUP-2).
    # exclude_listing_id prevents a message from matching itself.
    skip_reason = filters.check_filters(
        raw_text,
        hours=DEDUP_HOURS,
        price=source_price,
        exclude_listing_id=listing_id,
    )
    if skip_reason:
        await db.run_async(db.update_listing_status, listing_id, f"skipped_{skip_reason}")
        await db.run_async(db.log_skip, supplier_id, source_msg_id, skip_reason, raw_text)
        await db.run_async(db.record_audit, skip_reason, listing_id, detail=raw_text[:200])
        logger.info("Listing #%s skipped by filter '%s'.", listing_id, skip_reason)
        return

    # Step 1.5 (optional): cheap chatter pre-filter. Obvious non-listings
    # (rule posts, admin pins, welcome greetings, bot tests) never reach the AI
    # model, saving quota for real ads. Conservative: any listing signal vetoes.
    if PRE_FILTER_CHATTER:
        chatter_reason = filters.obvious_non_listing(raw_text)
        if chatter_reason == filters.REASON_CHATTER:
            await db.run_async(db.update_listing_status, listing_id, f"skipped_{chatter_reason}")
            await db.run_async(db.log_skip, supplier_id, source_msg_id, chatter_reason, raw_text)
            await db.run_async(db.record_audit, chatter_reason, listing_id, detail=raw_text[:200])
            logger.info(
                "Listing #%s skipped by chatter pre-filter '%s'.",
                listing_id,
                chatter_reason,
            )
            return

    # Step 1.75: payment-proof / confirmation messages are NEVER auto-published.
    # A "I paid $X / here is the proof" message is not a listing, leaks buyer
    # payment details, and is the #1 misclassification into the buy auto-publish
    # path — so it is caught BEFORE the AI and routed to manual review.
    payment_proof = filters.detect_payment_proof(raw_text)
    if payment_proof:
        await db.run_async(db.update_listing_status, listing_id, "pending_approval")
        await db.run_async(
            db.record_audit,
            "payment_proof_review",
            listing_id,
            detail=payment_proof[:200],
        )
        logger.warning(
            "Listing #%s flagged as payment proof ('%s') — manual review, not published.",
            listing_id,
            payment_proof,
        )
        if bot_client and ADMIN_USER_ID:
            listing_dict = db.get_listing_by_id(listing_id)
            if listing_dict:
                listing_dict["supplier_username"] = supplier.get("channel_username")
                listing_dict["_review_reason"] = "payment_proof"
                await admin_bot.send_approval_prompt(bot_client, ADMIN_USER_ID, listing_dict)
        return

    # Step 2: AI analysis + rewriting — ONE call decides everything. Nothing
    # else gates publishing: platform/price are recorded for display only.
    analysis = await ai_rephraser.analyze_message(raw_text)

    if analysis is None:
        # AI unavailable / failed. Record the neutral intent (the regex price is
        # used only for the dedup fingerprint, never for publishing).
        await db.run_async(
            db.update_listing_fields,
            listing_id,
            intent="neutral",
        )

        # Deterministic fallback (no AI): keep real listings publishing during
        # an AI outage via the regex/emoji-strip path already used for previews.
        # Gated HARD (FALLBACK-1): only a message with a concrete listing
        # signal + a substantive sanitized body + no payment-proof may
        # auto-publish. Anything weaker routes to manual review below. The
        # blocked-keyword screen is an additional safety valve — stolen/hacked
        # content ALWAYS still routes to manual review, never auto-published.
        risky_keyword = filters.contains_blocked_keyword(raw_text)
        fb_content_lines = [
            ln.strip()
            for ln in fallback_clean_text.split("\n")
            if ln.strip()
        ] or ["Available"]
        fb_lines, fb_body_ok = parser.prepare_body(fb_content_lines, raw_text)
        if deterministic_fallback_publish_ok(
            paused=await db.run_async(db.is_paused),
            risky_keyword=risky_keyword,
            has_payment_proof=filters.detect_payment_proof(raw_text),
            has_clear_signal=filters.has_clear_listing_signal(raw_text),
            body_ok=fb_body_ok,
        ):
            content_lines = fb_lines
            post_number = await db.run_async(db.next_post_number)
            out_text, entities = parser.build_ai_message(
                content_lines=content_lines,
                platform=None,
                contact_username=CONTACT_USERNAME,
                intent="neutral",
                header_word=None,
                listing_seed=listing_id,
                post_number=post_number,
                source_text=raw_text,
            )
            try:
                published_msg_id = await publish_to_destination(client, out_text, entities)
                await db.run_async(
                    db.update_listing_status,
                    listing_id=listing_id,
                    status="published",
                    published_message_id=published_msg_id,
                    post_number=post_number,
                )
                await db.run_async(
                    db.record_audit,
                    "published_deterministic_fallback",
                    listing_id,
                    detail=f"AI unavailable (msg_id {published_msg_id})",
                )
                await enqueue_destination_forwardings(listing_id, published_msg_id)
                logger.info(
                    "Listing #%s published via deterministic fallback (AI down) -> %s (msg_id: %s)",
                    listing_id,
                    DEST_CHANNEL,
                    published_msg_id,
                )
                await _alert_admin_on_published(bot_client, supplier, listing_id)
            except PublishError as exc:
                await db.run_async(db.mark_listing_failed, listing_id, str(exc))
                logger.error(
                    "Listing #%s deterministic fallback exhausted publish retries; "
                    "moved to failed queue. Last error: %s",
                    listing_id,
                    exc,
                )
                await _alert_admin_on_failure(bot_client, supplier, listing_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                await db.run_async(
                    db.mark_listing_failed,
                    listing_id,
                    "unexpected deterministic fallback publish error",
                )
                logger.exception(
                    "Failed to publish listing #%s via deterministic fallback",
                    listing_id,
                )
                await _alert_admin_on_failure(bot_client, supplier, listing_id)
            return

        # AI is down AND the content is either risky or publishing is paused:
        # route to admin review instead of dropping the message silently.
        reason_detail = (
            f"blocked-keyword screen: {risky_keyword}"
            if risky_keyword
            else "No AI response; routed for manual review"
        )
        await db.run_async(db.update_listing_status, listing_id, "pending_review")
        await db.run_async(db.record_audit, "ai_unavailable", listing_id, detail=reason_detail)
        logger.warning(
            "Listing #%s: AI analysis unavailable (%s). Routed to pending_review.",
            listing_id,
            reason_detail,
        )
        if bot_client and ADMIN_USER_ID:
            listing_dict = db.get_listing_by_id(listing_id)
            if listing_dict:
                listing_dict["supplier_username"] = supplier.get("channel_username")
                listing_dict["_review_reason"] = "ai_unavailable"
                await admin_bot.send_approval_prompt(bot_client, ADMIN_USER_ID, listing_dict)
        return

    # Blocklist check (AI-detected illicit / hacked / stolen content).
    # A wrong "blocked" would silently destroy a real listing, so flagged
    # messages go to admin review, not the trash.
    # INJECT-1: the deterministic keyword screen also overrides a lenient AI
    # verdict — if the raw source trips a blocked keyword, the listing ALWAYS
    # routes to manual review, never auto-published, without trusting the model.
    risky_keyword = filters.contains_blocked_keyword(raw_text)
    if analysis.get("blocked") or risky_keyword:
        reason = analysis.get("block_reason") or f"blocked-keyword screen: {risky_keyword}"
        await db.run_async(db.update_listing_status, listing_id, "pending_review")
        await db.run_async(db.record_blocklist_hit, listing_id, reason[:200])
        await db.run_async(db.record_audit, "ai_blocked_review", listing_id, detail=reason)
        logger.warning(
            "Listing #%s flagged as blocked by AI (%s) — routed to manual review.",
            listing_id,
            reason,
        )
        if bot_client and ADMIN_USER_ID:
            listing_dict = db.get_listing_by_id(listing_id)
            if listing_dict:
                listing_dict["supplier_username"] = supplier.get("channel_username")
                listing_dict["_review_reason"] = "ai_blocked_review"
                await admin_bot.send_approval_prompt(bot_client, ADMIN_USER_ID, listing_dict)
        return

    # Not a legitimate listing (spam / admin chatter / nonsense)
    if not analysis.get("is_listing"):
        await db.run_async(db.update_listing_status, listing_id, "skipped_filter")
        await db.run_async(db.log_skip, supplier_id, source_msg_id, "not_a_listing", raw_text)
        await db.run_async(db.record_audit, "not_a_listing", listing_id, detail="AI: not a listing")
        logger.info(
            "Listing #%s skipped (AI classified as not a listing).",
            listing_id,
        )
        return

    platform_name = analysis.get("platform")
    intent = analysis.get("intent") or "neutral"
    content_lines = analysis.get("content") or []
    ai_clean_text = "\n".join(content_lines) if content_lines else fallback_clean_text

    # Persist the AI-rewritten body so the worker / admin preview reuse it as-is.
    # platform is informational only — it does NOT gate publishing.
    await db.run_async(
        db.update_listing_fields,
        listing_id,
        platform_name=platform_name,
        intent=intent,
        header_word=analysis.get("header"),
    )
    if ai_clean_text:
        await db.run_async(db.update_listing_content, listing_id, clean_text=ai_clean_text)

    # Sanitize the AI body now (strip leaked prices/@handles/DM lines) and gate
    # the buy auto-publish: a buy demand auto-publishes when it has a
    # substantive sanitized body and shows no payment-proof signal — prices and
    # platforms are never part of the decision. Everything else routes to manual
    # approval.
    body_lines, body_ok = parser.prepare_body(content_lines, ai_clean_text or raw_text)
    buy_auto_ok = buy_auto_publish_ok(
        intent=intent,
        paused=await db.run_async(db.is_paused),
        body_ok=body_ok,
        has_payment_proof=filters.detect_payment_proof(raw_text),
    )

    if buy_auto_ok:
        # Buy demand -> rephrase + auto-publish (gated).
        post_number = await db.run_async(db.next_post_number)
        our_text, entities = parser.build_ai_message(
            content_lines=body_lines,
            platform=platform_name,
            contact_username=CONTACT_USERNAME,
            intent=intent,
            header_word=analysis.get("header"),
            listing_seed=listing_id,
            post_number=post_number,
            source_text=raw_text,
        )

        try:
            published_msg_id = await publish_to_destination(client, our_text, entities)
            await db.run_async(
                db.update_listing_status,
                listing_id=listing_id,
                status="published",
                published_message_id=published_msg_id,
                post_number=post_number,
            )
            await db.run_async(db.record_audit, "published_auto", listing_id, detail=published_msg_id)
            await enqueue_destination_forwardings(listing_id, published_msg_id)
            logger.info(
                "Published listing #%s -> %s (msg_id: %s, post #%s)",
                listing_id,
                DEST_CHANNEL,
                published_msg_id,
                post_number,
            )
            await _alert_admin_on_published(bot_client, supplier, listing_id)
        except PublishError as exc:
            await db.run_async(db.mark_listing_failed, listing_id, str(exc))
            logger.error(
                "Listing #%s exhausted publish retries; moved to failed queue. Last error: %s",
                listing_id,
                exc,
            )
            await _alert_admin_on_failure(bot_client, supplier, listing_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await db.run_async(db.mark_listing_failed, listing_id, "unexpected publish error")
            logger.exception("Failed to publish message for listing #%s", listing_id)
            await _alert_admin_on_failure(bot_client, supplier, listing_id)
    else:
        # Not an auto-publishable buy signal (unsafe buy, sell, neutral, paused)
        # -> route to manual approval with the exact reason attached.
        gate_reason = None
        if intent == "buy":
            if filters.detect_payment_proof(raw_text):
                gate_reason = "payment_proof"
            elif not body_ok:
                gate_reason = "buy_gate_weak_body"
            else:
                gate_reason = "buy_gate"
        await db.run_async(db.update_listing_status, listing_id, "pending_approval")
        logger.info(
            "Listing #%s marked pending_approval (gate=%s). Alerting admin...",
            listing_id,
            gate_reason or "sell/neutral",
        )

        if bot_client and ADMIN_USER_ID:
            listing_dict = db.get_listing_by_id(listing_id)
            if listing_dict:
                listing_dict["supplier_username"] = supplier.get("channel_username")
                listing_dict["_review_reason"] = gate_reason
                await admin_bot.send_approval_prompt(bot_client, ADMIN_USER_ID, listing_dict)


async def process_edited_message(client: TelegramClient, event) -> None:
    """Handle edited message in a monitored supplier channel."""
    msg = event.message
    if not msg:
        return

    supplier = await resolve_supplier_for_event(event)
    if not supplier:
        return

    supplier_id = supplier["id"]
    source_msg_id = msg.id
    raw_text = msg.text or ""

    existing = db.get_listing_by_source(supplier_id, source_msg_id)
    if not existing or existing.get("status") != "published" or not existing.get("published_message_id"):
        logger.debug("Edit on unindexed or unpublished source message %s; ignoring", source_msg_id)
        return

    # AI analysis + rewriting of the edited content
    analysis = await ai_rephraser.analyze_message(raw_text)

    if analysis is None:
        logger.warning(
            "Edited message %s: AI analysis unavailable. Leaving published post intact.",
            source_msg_id,
        )
        return

    if analysis.get("blocked"):
        logger.warning(
            "Edited message %s now flagged as blocked by AI. Leaving existing published post intact.",
            source_msg_id,
        )
        return

    if not analysis.get("is_listing"):
        logger.info(
            "Edited message %s no longer qualifies as a valid listing. Leaving published post intact.",
            source_msg_id,
        )
        return

    platform_name = analysis.get("platform")
    intent = analysis.get("intent") or "neutral"
    content_lines = analysis.get("content") or []
    ai_clean_text = "\n".join(content_lines) if content_lines else parser.strip_all_emoji(raw_text)
    header_word = analysis.get("header")

    # AI-only decision: update the destination post from whatever the analysis
    # returns (the footer price is always the static "Price DM" line).

    # No-op guard: skip edit if nothing meaningfully changed.
    if ai_clean_text == existing.get("clean_text"):
        logger.debug("Edit on source msg %s is a no-op; skipping", source_msg_id)
        return

    updated_text, entities = parser.build_ai_message(
        content_lines=content_lines,
        platform=platform_name,
        contact_username=CONTACT_USERNAME,
        intent=intent,
        header_word=header_word,
        listing_seed=existing["id"],
        post_number=existing.get("post_number"),
        source_text=raw_text,
    )

    published_msg_id = existing["published_message_id"]
    dest_peer = db.to_peer_reference(DEST_CHANNEL)
    try:
        await client.edit_message(
            dest_peer, published_msg_id, updated_text,
            formatting_entities=entities
        )
        await db.run_async(
            db.update_listing_fields,
            listing_id=existing["id"],
            platform_name=platform_name,
            intent=intent,
            header_word=header_word,
        )
        await db.run_async(
            db.update_listing_content,
            listing_id=existing["id"],
            clean_text=ai_clean_text,
            rank_tier=None,
        )
        # TEL-3: refresh the dedup fingerprint from the edited raw text so a
        # re-processed/re-delivered version of this message is recognized.
        edit_price, _ = parser.extract_price(raw_text)
        await db.run_async(
            db.set_listing_fingerprint,
            existing["id"],
            raw_text,
            price=edit_price,
        )
        await db.run_async(
            db.record_audit,
            "edited_destination",
            existing["id"],
            detail=f"source {source_msg_id}",
        )
        logger.info(
            "Updated destination post %s in %s for source msg %s",
            published_msg_id,
            DEST_CHANNEL,
            source_msg_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Failed to edit destination message %s for source %s",
            published_msg_id,
            source_msg_id,
        )


async def process_deleted_message(event) -> None:
    """Handle deleted message notification from source channels."""
    supplier = await resolve_supplier_for_event(event)
    if not supplier:
        return

    for deleted_id in event.deleted_ids:
        existing = db.get_listing_by_source(supplier["id"], deleted_id)
        if existing and existing.get("published_message_id"):
            logger.warning(
                "Source message %s was deleted in %s. Published post %s retained for review.",
                deleted_id,
                _supplier_label(supplier),
                existing.get("published_message_id"),
            )


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------
async def _recover_stale_publish_claims(client: TelegramClient) -> None:
    """Resolve publish claims abandoned mid-flight (F3 crash recovery).

    A claim held while the process died leaves the listing in 'publishing'.
    The claim timestamp (updated_at) plus PUBLISH_CLAIM_GRACE_SECONDS decides
    abandonment. Every stale row is verified against the destination channel:

    * message present -> the last attempt actually landed; record it as
      published (exact same deterministic text as the crashed attempt, since
      the post number was persisted at claim time) so it is NEVER re-sent;
    * message absent  -> the crashed attempt never reached Telegram; release
      the claim back to 'approved' so the normal drain claims + publishes it.
    """
    stale = await db.run_async(db.get_stale_publish_claims, PUBLISH_CLAIM_GRACE_SECONDS, 10)
    for row in stale:
        listing_id = row["id"]
        content_text = row.get("clean_text") or row.get("raw_text") or ""
        content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
        try:
            out_text, entities = parser.build_ai_message(
                content_lines=content_lines,
                platform=row.get("platform_name"),
                contact_username=CONTACT_USERNAME,
                intent=row.get("intent") or "neutral",
                header_word=row.get("header_word"),
                listing_seed=listing_id,
                post_number=row.get("post_number"),
                source_text=row.get("raw_text"),
            )
        except Exception:
            logger.exception(
                "Could not rebuild deterministic text for stale claim on listing #%s; "
                "releasing claim.",
                listing_id,
            )
            await db.run_async(db.release_publish_claim, listing_id, "approved")
            continue

        found_id = await publish_guard.confirm_message_on_destination(
            client, db.to_peer_reference(DEST_CHANNEL), out_text
        )
        if found_id is not None:
            await db.run_async(
                db.update_listing_status,
                listing_id=listing_id,
                status="published",
                published_message_id=found_id,
                post_number=row.get("post_number"),
            )
            await enqueue_destination_forwardings(listing_id, found_id)
            logger.warning(
                "Recovered stale publish claim for listing #%s: message already on "
                "destination (msg id %s) — recorded, not re-sent.",
                listing_id,
                found_id,
            )
        else:
            await db.run_async(db.release_publish_claim, listing_id, "approved")
            logger.warning(
                "Released stale publish claim for listing #%s (no message on "
                "destination); it will be re-claimed and published on the next drain.",
                listing_id,
            )


async def approved_listings_worker(
    client: TelegramClient, stop_event: asyncio.Event, bot_client: Optional[TelegramClient]
) -> None:
    """Publish admin-approved listings (DLQ-safe: requires published_message_id IS NULL).

    Approval is the deliberate signal to publish: the queue drain runs even
    while "All Stop" is on. Pausing only gates the AUTOMATIC publish paths
    (buy-intent auto-publish and the deterministic AI-down fallback), never a
    listing an admin has explicitly approved.
    """
    while not stop_event.is_set():
        _mark_worker_heartbeat("approved_worker")
        try:
            # F3: first resolve claims that died mid-publish (crash between the
            # claim and the DB bookkeeping). Verification-based recovery either
            # records the message that actually landed or releases the claim.
            await _recover_stale_publish_claims(client)
            approved = await db.run_async(db.get_approved_listings_to_publish, 5)
            for listing in approved:
                listing_id = listing["id"]
                # clean_text already holds the AI-rewritten body (or the raw fallback
                # for listings captured before AI was available).
                content_text = listing.get("clean_text") or listing.get("raw_text") or ""
                content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
                intent = listing.get("intent") or "neutral"
                post_number = await db.run_async(db.next_post_number)

                # F3: atomic claim BEFORE any external send. Only the single
                # winner proceeds; a second worker, admin double-tap, or
                # crash-retry sees status 'publishing' / non-NULL msg id here and
                # must NOT send another Telegram message.
                claimed = await db.run_async(
                    db.claim_listing_for_publish, listing_id, post_number
                )
                if not claimed:
                    logger.info(
                        "Listing #%s already claimed for publishing by another path; skipping.",
                        listing_id,
                    )
                    continue

                out_text, entities = parser.build_ai_message(
                    content_lines=content_lines,
                    platform=listing.get("platform_name"),
                    contact_username=CONTACT_USERNAME,
                    intent=intent,
                    header_word=listing.get("header_word"),
                    listing_seed=listing_id,
                    post_number=post_number,
                    source_text=listing.get("raw_text"),
                )

                published_msg_id = None
                try:
                    published_msg_id = await publish_to_destination(client, out_text, entities)
                except PublishError as exc:
                    await db.run_async(db.mark_listing_failed, listing_id, str(exc))
                    logger.error(
                        "Worker failed to publish approved listing #%s after retries → failed queue: %s",
                        listing_id,
                        exc,
                    )
                    await _alert_admin_on_failure(
                        bot_client, {"channel_username": listing.get("supplier_username")}, listing_id
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await db.run_async(db.mark_listing_failed, listing_id, str(exc)[:500])
                    logger.exception("Worker failed to publish approved listing #%s", listing_id)
                    await _alert_admin_on_failure(
                        bot_client, {"channel_username": listing.get("supplier_username")}, listing_id
                    )
                    continue

                try:
                    await db.run_async(
                        db.update_listing_status,
                        listing_id=listing_id,
                        status="published",
                        published_message_id=published_msg_id,
                        post_number=post_number,
                    )
                    await db.run_async(db.record_audit, "published_approved", listing_id, detail=published_msg_id)
                    await enqueue_destination_forwardings(listing_id, published_msg_id)
                except Exception as exc:
                    logger.exception(
                        "Worker published approved listing #%s (msg id %s) but bookkeeping failed: %s",
                        listing_id, published_msg_id, exc,
                    )
                    try:
                        await db.run_async(
                            db.update_listing_status,
                            listing_id=listing_id,
                            status="published",
                            published_message_id=published_msg_id,
                            post_number=post_number,
                        )
                        await enqueue_destination_forwardings(listing_id, published_msg_id)
                    except Exception:
                        logger.exception("Could not record published state for listing #%s", listing_id)
                logger.info(
                    "Worker published approved listing #%s to %s (msg id: %s)",
                    listing_id,
                    DEST_CHANNEL,
                    published_msg_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in approved listings worker")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


async def health_check_worker(stop_event: asyncio.Event) -> None:
    """Periodic health check logging every hour."""
    while not stop_event.is_set():
        try:
            stats = await db.run_async(db.get_today_stats)
            _mark_worker_heartbeat("health_check")
            logger.info(
                "Periodic Health Check: Active Suppliers=%s | Today Processed=%s | "
                "Published=%s | Pending=%s | Skipped=%s | Errors=%s%s",
                stats["active_suppliers"],
                stats["total_processed"],
                stats["published"],
                stats["pending"],
                stats["total_skipped"],
                stats["errors"],
                _runtime_health_suffix(),
            )
        except Exception:
            logger.exception("Error in health check worker")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


async def run_backfill(client: TelegramClient, bot_client: Optional[TelegramClient]) -> None:
    """
    Optional catch-up sweep. When BACKFILL_ON_START=1, iterate supplier channels
    and re-process any messages newer than the last one we already recorded.
    """
    if not BACKFILL_ON_START:
        return

    logger.info("BACKFILL_ON_START enabled — sweeping source channels for missed messages.")
    for supplier in db.list_suppliers(active_only=True):
        try:
            entity = await resolve_chat(client, supplier)
            last_id = db.get_last_source_message_id(supplier["id"])
            count = 0
            async for msg in client.iter_messages(
                entity, min_id=last_id, reverse=True, limit=200
            ):
                await process_supplier_message(client, bot_client, supplier, msg)
                count += 1
            logger.info(
                "Backfill done for supplier %s: %s new message(s) processed.",
                _supplier_label(supplier),
                count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Backfill failed for supplier %s",
                _supplier_label(supplier),
            )
    logger.info("Backfill sweep finished.")


# ---------------------------------------------------------------------------
# One-time stale-body rephrase sweep
# ---------------------------------------------------------------------------
async def rephrase_unpublished() -> None:
    """Re-run AI rewriting over unpublished listings whose stored body is still
    the regex fallback (emoji-stripped source), so admin previews show real AI
    rephrasing instead. Already-AI bodies are left untouched to avoid churn.

    Bounded (REPHR-1): the sweep is capped per run and only touches listings
    older than REPHRASE_SWEEP_MIN_AGE_SECONDS, so startup recovery stays fast,
    burns a bounded amount of AI quota, and never races a listing the live
    pipeline or the approval worker is still working on. Identical content
    re-uses the fingerprint-keyed ai_cache. Re-running the sweep later covers
    the next oldest slice of the queue.
    """
    if not ai_rephraser.is_available():
        logger.info("AI not available; skipping stale rephrase sweep.")
        return
    listings = db.get_unpublished_listings(
        limit=REPHRASE_SWEEP_LIMIT,
        min_age_seconds=REPHRASE_SWEEP_MIN_AGE_SECONDS,
    )
    refreshed = 0
    for listing in listings:
        listing_id = listing["id"]
        clean = (listing.get("clean_text") or "").strip()
        raw = listing.get("raw_text") or ""
        if clean and clean != parser.strip_all_emoji(raw):
            continue
        analysis = await ai_rephraser.analyze_message(raw)
        if not analysis:
            logger.warning(
                "Rephrase sweep: AI returned nothing for listing #%s",
                listing_id,
            )
            continue
        content_lines = analysis.get("content") or []
        if not content_lines:
            continue
        ai_clean_text = "\n".join(content_lines)
        await db.run_async(
            db.update_listing_fields,
            listing_id,
            platform_name=analysis.get("platform"),
            intent=analysis.get("intent"),
        )
        await db.run_async(db.update_listing_content, listing_id, clean_text=ai_clean_text)
        refreshed += 1
        logger.info(
            "Rephrase sweep: listing #%s body refreshed via AI (platform=%s)",
            listing_id,
            analysis.get("platform"),
        )
    logger.info(
        "Rephrase sweep finished: %s unpublished listing(s) refreshed (%d considered).",
        refreshed,
        len(listings),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    _validate_config()
    db.init_db()
    try:
        norm_result = db.normalize_supplier_channel_ids()
        if norm_result["normalized"] or norm_result["merged"]:
            logger.warning(
                "Supplier channel-id normalization: re-marked %d bare id(s), "
                "merged %d row(s) into canonical rows (legacy bare ids fixed "
                "to -100... marked form).",
                norm_result["normalized"],
                norm_result["merged"],
            )
    except Exception:
        logger.exception("Could not normalize legacy supplier channel ids at startup")
    try:
        db.prune_ai_cache(AI_CACHE_TTL_HOURS)
    except Exception:
        logger.exception("Could not prune AI analysis cache at startup")
    try:
        unresolved_reset = db.reset_unresolved_forwardings()
        if unresolved_reset:
            logger.info("Reset %d forwardings with resolution errors for immediate retry.", unresolved_reset)
    except Exception:
        logger.exception("Could not reset unresolved forwardings at startup")
    try:
        stale_forward_reset = db.reset_stale_forward_claims()
        if stale_forward_reset:
            logger.info(
                "Reset %d stale forwarding claims (crash-recovered); they will be "
                "verified against their destinations before any re-forward.",
                stale_forward_reset,
            )
    except Exception:
        logger.exception("Could not reset stale forwarding claims at startup")
    env_seed_notice = db.validate_env_seed_config(SOURCE_CHANNELS_RAW)
    if env_seed_notice:
        logger.warning("%s", env_seed_notice)
    env_seed_result = db.ensure_env_seed(SOURCE_CHANNELS)
    logger.info(
        "Env seeding: state=%s seeded=%s migrated=%s — .env is NOT consulted again "
        "unless /reseed_from_env is run manually.",
        env_seed_result["state"],
        env_seed_result["seeded"],
        env_seed_result.get("migrated", False),
    )
    ai_rephraser.init_groq(GROQ_API_KEY)

    stop_event = asyncio.Event()

    # User client for monitoring and publishing
    user_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    # Optional admin bot client
    bot_client: Optional[TelegramClient] = None
    if BOT_TOKEN:
        try:
            bot_client = await admin_bot.create_admin_bot_client()
            admin_bot.set_user_client(user_client)
            logger.info("Admin bot integrated successfully.")
        except Exception:
            logger.exception("Failed to start integrated admin bot; continuing with user listener only.")

    # Register user client event handlers — skipped entirely in manual mode,
    # where ingestion / auto-publish must not run (the admin publishes only).
    if not MANUAL_MODE:
        @user_client.on(events.NewMessage)
        async def on_new_message(event):
            try:
                async def _handle_new():
                    supplier = await resolve_supplier_for_event(event)
                    if supplier:
                        await process_supplier_message(user_client, bot_client, supplier, event.message)

                await publish_guard.run_with_floodwait_retry(
                    _handle_new,
                    f"new message {getattr(event.message, 'id', None)}",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error processing new message %s", getattr(event.message, "id", None))

        @user_client.on(events.MessageEdited)
        async def on_message_edited(event):
            try:
                async def _handle_edit():
                    await process_edited_message(user_client, event)

                await publish_guard.run_with_floodwait_retry(
                    _handle_edit,
                    f"edited message {getattr(event.message, 'id', None)}",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error processing edited message %s", getattr(event.message, "id", None))

        @user_client.on(events.MessageDeleted)
        async def on_message_deleted(event):
            try:
                await process_deleted_message(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unhandled error processing deleted message")

    await user_client.start()
    try:
        await user_client.get_dialogs(limit=50)
        logger.debug("Warmed up user client dialogs cache.")
    except Exception:
        logger.debug("Could not pre-warm user client dialogs cache.")
    if MANUAL_MODE:
        # Manual mode: no supplier resolution, no ingestion, no auto-publish —
        # just the user client + admin bot ready for approval/repair work.
        logger.info(
            "MANUAL MODE active: admin bot + user client online. "
            "Ingestion/auto-publish disabled. Publishing to %s",
            DEST_CHANNEL,
        )
        if bot_client and ADMIN_USER_ID:
            try:
                await bot_client.send_message(
                    ADMIN_USER_ID,
                    "🛠 **Manual mode online.** Ingestion and auto-publish are OFF. "
                    "Approvals publish immediately; /skipped and /repair are available.",
                )
            except Exception:
                logger.exception("Could not send manual-mode notice")
    else:
        startup_resolved = await resolve_supplier_entities(user_client)
        active_total = len(db.list_suppliers(active_only=True, db_path=db.DEFAULT_DB_PATH))
        await _warn_if_zero_suppliers_resolved(
            bot_client, active_total, len(startup_resolved)
        )

        try:
            dedupe_result = await asyncio.get_running_loop().run_in_executor(
                None, db.dedupe_suppliers
            )
            if dedupe_result.get("merges") and bot_client and ADMIN_USER_ID:
                try:
                    await bot_client.send_message(
                        ADMIN_USER_ID,
                        f"🧹 Merged {len(dedupe_result['merges'])} duplicate supplier row(s) "
                        f"({dedupe_result['rows_removed']} removed). No action needed — "
                        f"listings were preserved on the surviving row.",
                    )
                except Exception:
                    logger.exception("Could not DM admin about supplier dedupe")
        except Exception:
            logger.exception("Startup supplier dedupe failed")

        logger.info(
            "User listener connected. Monitoring %s configured suppliers, publishing to %s",
            active_total,
            DEST_CHANNEL,
        )

        # Startup ping: an immediate DM proves the bot is online AND that it can send.
        # Kept best-effort so a failure here never blocks the monitor.
        if bot_client and ADMIN_USER_ID:
            try:
                await bot_client.send_message(
                    ADMIN_USER_ID,
                    "✅ Bot is online and monitoring suppliers.",
                )
                logger.info("Sent online notice to admin %s", ADMIN_USER_ID)
            except Exception:
                logger.exception("Could not send startup online notice")

    # Start background workers
    worker_task = asyncio.create_task(approved_listings_worker(user_client, stop_event, bot_client))
    forward_task = asyncio.create_task(forwarding_worker(user_client, stop_event))
    health_task = None
    resolve_task = None
    rephrase_task = None
    backfill_task = None
    if not MANUAL_MODE:
        health_task = asyncio.create_task(health_check_worker(stop_event))
        resolve_task = asyncio.create_task(
            supplier_resolution_worker(user_client, bot_client, stop_event)
        )
        rephrase_task = asyncio.create_task(rephrase_unpublished())
        if BACKFILL_ON_START:
            backfill_task = asyncio.create_task(run_backfill(user_client, bot_client))

    # Reconnect loop with capped backoff
    backoff = 10
    try:
        while not stop_event.is_set():
            try:
                await user_client.run_until_disconnected()
                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "Listener client disconnected unexpectedly. Reconnecting in %ss...",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
    finally:
        logger.info("Shutting down workers and clients...")
        stop_event.set()
        worker_task.cancel()
        forward_task.cancel()
        for task in (health_task, resolve_task, rephrase_task):
            if task is not None:
                task.cancel()
        if backfill_task:
            backfill_task.cancel()
        # SHUT-1: actually await the cancelled tasks so their finally-blocks and
        # DB connection check-ins complete instead of leaking as orphans.
        pending_tasks = [worker_task, forward_task]
        for task in (health_task, resolve_task, rephrase_task):
            if task is not None:
                pending_tasks.append(task)
        if backfill_task:
            pending_tasks.append(backfill_task)
        try:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        except Exception:
            logger.exception("Error awaiting background task shutdown")
        if bot_client:
            try:
                await bot_client.disconnect()
            except Exception:
                logger.exception("Error disconnecting admin bot client")
        try:
            await user_client.disconnect()
        except Exception:
            logger.exception("Error disconnecting user client")


if __name__ == "__main__":
    arm_single_instance_guard()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user.")
    except SystemExit:
        raise
    except Exception:
        logger.exception("Fatal error in monitor execution")