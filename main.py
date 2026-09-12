"""Telegram Channel Monitor & Auto-Republisher — Main Orchestrator.

Wires together Telethon user client, SQLite persistence, parser,
keyword blocklist, duplicate detection, and admin approval bot.
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

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
DEFAULT_MULTIPLIER = float(os.environ.get("PRICE_MULTIPLIER", "0.75"))
PUBLISH_INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "1.5"))
PUBLISH_MAX_RETRIES = int(os.environ.get("PUBLISH_MAX_RETRIES", "4"))
BACKFILL_ON_START = os.environ.get("BACKFILL_ON_START", "0").strip().lower() in ("1", "true", "yes")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# Upper sanity bound for an AI-reported price; anything beyond this is
# treated as a hallucination and routed to manual review (AI-1).
MAX_PRICE = float(os.environ.get("MAX_PRICE", "100000") or 100000)
# Free-tier reliability toggles: the chatter pre-filter saves AI quota, and the
# deterministic fallback keeps real listings publishing when every AI path is down.
PRE_FILTER_CHATTER = os.environ.get("PRE_FILTER_CHATTER", "1").strip().lower() in ("1", "true", "yes")
DETERMINISTIC_FALLBACK = os.environ.get("DETERMINISTIC_FALLBACK", "1").strip().lower() in ("1", "true", "yes")
AI_CACHE_TTL_HOURS = float(os.environ.get("AI_CACHE_TTL_HOURS", "48") or 48)
# How long identical content stays blocked as a duplicate: a re-post of the
# same listing within DEDUP_HOURS is skipped (content fingerprint + price).
DEDUP_HOURS = int(os.environ.get("DEDUP_HOURS", "48") or 48)
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
    # CFG-1: reject unrealistic price multipliers instead of silently corrupting prices.
    if not (0 < DEFAULT_MULTIPLIER <= 5.0):
        errors.append(
            f"PRICE_MULTIPLIER must be between 0 (exclusive) and 5.0 (got {DEFAULT_MULTIPLIER})"
        )

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
    rows = db.list_suppliers(active_only=True)
    if only_unresolved:
        rows = [s for s in rows if s.get("channel_id") is None]
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
            db.set_supplier_display_name(
                channel_id if channel_id is not None else username, display
            )

        real_owner = db.get_supplier_by_chat(chat_id=entity_id, username=None)
        if real_owner is not None and real_owner["id"] != supplier["id"]:
            db.merge_supplier_rows(real_owner["id"], supplier["id"])
            resolved.append({**real_owner, "channel_id": entity_id})
            logger.info(
                "Supplier %s resolved into existing row id %s (%s)",
                _supplier_label({**supplier, "display_name": None}),
                real_owner["id"],
                display,
            )
            continue

        if channel_id is None:
            db.set_supplier_channel_id(username, entity_id)
            channel_id = entity_id
        elif channel_id != entity_id:
            db.set_supplier_channel_id(username or str(channel_id), entity_id)
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


def resolve_supplier_for_event(event) -> Optional[dict]:
    """Find the active supplier matching this message event."""
    chat_id = event.chat_id
    chat_username = getattr(event.chat, "username", None)
    supplier = db.get_supplier_by_chat(chat_id=chat_id, username=chat_username)

    # If supplier was stored without channel_id, update it now
    if supplier and supplier.get("channel_id") is None and chat_id is not None:
        db.set_supplier_channel_id(supplier["channel_username"], chat_id)
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
    - Raises PublishError after exhausting retries.
    """
    await publish_guard.throttle()
    delay = 3
    last_error = "unknown error"
    for attempt in range(1, PUBLISH_MAX_RETRIES + 1):
        try:
            sent = await client.send_message(DEST_CHANNEL, text, formatting_entities=entities)
            return sent.id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)[:500]
            flood_seconds = getattr(exc, "seconds", 0) or 0
            if flood_seconds:
                logger.warning(
                    "FloodWait %ss hit while publishing (attempt %d/%d); sleeping.",
                    flood_seconds,
                    attempt,
                    PUBLISH_MAX_RETRIES,
                )
                await asyncio.sleep(flood_seconds)
                continue
            logger.warning(
                "Publish attempt %d/%d failed: %s",
                attempt,
                PUBLISH_MAX_RETRIES,
                last_error,
            )
            if attempt < PUBLISH_MAX_RETRIES:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    raise PublishError(last_error)


async def _run_with_floodwait_retry(coro_factory, context: str):
    """Await a coroutine factory, transparently honoring FloodWaitError (TEL-1)."""
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as fwe:
            logger.warning(
                "FloodWait(%ss) hit during %s — sleeping before retry.",
                fwe.seconds,
                context,
            )
            await asyncio.sleep(max(fwe.seconds, 1))


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
# Core pipeline
# ---------------------------------------------------------------------------
# Per-message in-flight locks (CONC-2). Telegram handlers run concurrently and
# the same source message may be delivered twice; this serializes processing
# per (supplier_id, source_msg_id). Entries are intentionally retained — removing
# one would race with waiters that already captured the Lock object.
_processing_locks: Dict[Tuple[int, int], asyncio.Lock] = {}


def _get_processing_lock(supplier_id: int, source_msg_id: int) -> asyncio.Lock:
    key = (supplier_id, source_msg_id)
    lock = _processing_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _processing_locks[key] = lock
    return lock


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
    async with _get_processing_lock(supplier["id"], source_msg_id):
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
                    db.mark_listing_failed(
                        listing["id"], f"pipeline error: {str(exc)[:500]}"
                    )
                    db.record_audit(
                        "pipeline_error_failed", listing["id"], detail=str(exc)[:500]
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

    if not source_msg_id or not raw_text.strip():
        return

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
    # own posts, not a filter on real supplier ads.
    if CONTACT_USERNAME and f"Order  : @{CONTACT_USERNAME}" in raw_text:
        logger.info(
            "Source message %s from %s looks like our own destination output; skipping",
            source_msg_id,
            _supplier_label(supplier),
        )
        db.log_skip(supplier_id, source_msg_id, "self_echo", raw_text)
        return

    logger.info(
        "Incoming message %s from supplier %s (chat_id: %s)",
        source_msg_id,
        _supplier_label(supplier),
        getattr(msg.chat, "id", None) if getattr(msg, "chat", None) else None,
    )

    fallback_clean_text = parser.strip_all_emoji(raw_text)

    listing_id = db.insert_listing(
        supplier_id=supplier_id,
        source_message_id=source_msg_id,
        game_name=None,
        rank_tier=None,
        original_price=None,
        our_price=None,
        status="received",
        raw_text=raw_text,
        clean_text=fallback_clean_text,
    )

    # Step 1: Content-based filter check FIRST — plain-text fingerprint
    # (normalized text + parsed price), no AI needed. Skip identical listings
    # already processed within DEDUP_HOURS; reason is logged to the skips
    # table so the daily report can break skipped stats down per rule (DEDUP-2).
    source_price, _ = parser.extract_price(raw_text)
    skip_reason = filters.check_filters(
        raw_text, hours=DEDUP_HOURS, price=source_price
    )
    if skip_reason:
        db.update_listing_status(listing_id, f"skipped_{skip_reason}")
        db.log_skip(supplier_id, source_msg_id, skip_reason, raw_text)
        db.record_audit(skip_reason, listing_id, detail=raw_text[:200])
        logger.info("Listing #%s skipped by filter '%s'.", listing_id, skip_reason)
        return
    db.set_listing_fingerprint(listing_id, raw_text, price=source_price)

    # Step 1.5 (optional): cheap chatter pre-filter. Obvious non-listings
    # (rule posts, admin pins, welcome greetings, bot tests) never reach the AI
    # model, saving quota for real ads. Conservative: any listing signal vetoes.
    if PRE_FILTER_CHATTER:
        chatter_reason = filters.obvious_non_listing(raw_text)
        if chatter_reason == filters.REASON_CHATTER:
            db.update_listing_status(listing_id, f"skipped_{chatter_reason}")
            db.log_skip(supplier_id, source_msg_id, chatter_reason, raw_text)
            db.record_audit(chatter_reason, listing_id, detail=raw_text[:200])
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
        db.update_listing_status(listing_id, "pending_approval")
        db.record_audit(
            "payment_proof_review", listing_id, detail=payment_proof[:200]
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
        db.update_listing_fields(
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
        if (
            DETERMINISTIC_FALLBACK
            and not risky_keyword
            and not filters.detect_payment_proof(raw_text)
            and filters.has_clear_listing_signal(raw_text)
            and fb_body_ok
            and not db.is_paused()
        ):
            content_lines = fb_lines
            post_number = db.next_post_number()
            out_text, entities = parser.build_ai_message(
                content_lines=content_lines,
                our_price=None,
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
                db.update_listing_status(
                    listing_id=listing_id,
                    status="published",
                    published_message_id=published_msg_id,
                    post_number=post_number,
                )
                db.record_audit(
                    "published_deterministic_fallback",
                    listing_id,
                    detail=f"AI unavailable (msg_id {published_msg_id})",
                )
                logger.info(
                    "Listing #%s published via deterministic fallback (AI down) -> %s (msg_id: %s)",
                    listing_id,
                    DEST_CHANNEL,
                    published_msg_id,
                )
                await _alert_admin_on_published(bot_client, supplier, listing_id)
            except PublishError as exc:
                db.mark_listing_failed(listing_id, str(exc))
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
                db.mark_listing_failed(listing_id, "unexpected deterministic fallback publish error")
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
        db.update_listing_status(listing_id, "pending_review")
        db.record_audit("ai_unavailable", listing_id, detail=reason_detail)
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
    if analysis.get("blocked"):
        reason = analysis.get("block_reason") or "blocked by AI analysis"
        db.update_listing_status(listing_id, "pending_review")
        db.record_blocklist_hit(listing_id, reason[:200])
        db.record_audit("ai_blocked_review", listing_id, detail=reason)
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
        db.update_listing_status(listing_id, "skipped_filter")
        db.log_skip(supplier_id, source_msg_id, "not_a_listing", raw_text)
        db.record_audit("not_a_listing", listing_id, detail="AI: not a listing")
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
    db.update_listing_fields(
        listing_id,
        platform_name=platform_name,
        intent=intent,
        header_word=analysis.get("header"),
    )
    if ai_clean_text:
        db.update_listing_content(listing_id, clean_text=ai_clean_text)

    # Sanitize the AI body now (strip leaked prices/@handles/DM lines) and gate
    # the buy auto-publish: a buy demand auto-publishes when it has a
    # substantive sanitized body and shows no payment-proof signal — prices and
    # platforms are never part of the decision. Everything else routes to manual
    # approval.
    body_lines, body_ok = parser.prepare_body(content_lines, ai_clean_text or raw_text)
    buy_auto_ok = (
        intent == "buy"
        and not db.is_paused()
        and body_ok
        and not filters.detect_payment_proof(raw_text)
    )

    if buy_auto_ok:
        # Buy demand -> rephrase + auto-publish (gated).
        post_number = db.next_post_number()
        our_text, entities = parser.build_ai_message(
            content_lines=body_lines,
            our_price=None,
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
            db.update_listing_status(
                listing_id=listing_id,
                status="published",
                published_message_id=published_msg_id,
                post_number=post_number,
            )
            db.record_audit("published_auto", listing_id, detail=published_msg_id)
            logger.info(
                "Published listing #%s -> %s (msg_id: %s, post #%s)",
                listing_id,
                DEST_CHANNEL,
                published_msg_id,
                post_number,
            )
            await _alert_admin_on_published(bot_client, supplier, listing_id)
        except PublishError as exc:
            db.mark_listing_failed(listing_id, str(exc))
            logger.error(
                "Listing #%s exhausted publish retries; moved to failed queue. Last error: %s",
                listing_id,
                exc,
            )
            await _alert_admin_on_failure(bot_client, supplier, listing_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            db.mark_listing_failed(listing_id, "unexpected publish error")
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
        db.update_listing_status(listing_id, "pending_approval")
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

    supplier = resolve_supplier_for_event(event)
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
    # returns (the footer price is always the static "Price: DM" line).

    # No-op guard: skip edit if nothing meaningfully changed.
    if ai_clean_text == existing.get("clean_text"):
        logger.debug("Edit on source msg %s is a no-op; skipping", source_msg_id)
        return

    updated_text, entities = parser.build_ai_message(
        content_lines=content_lines,
        our_price=None,
        platform=platform_name,
        contact_username=CONTACT_USERNAME,
        intent=intent,
        header_word=header_word,
        listing_seed=existing["id"],
        post_number=existing.get("post_number"),
        source_text=raw_text,
    )

    published_msg_id = existing["published_message_id"]
    try:
        await client.edit_message(
            DEST_CHANNEL, published_msg_id, updated_text,
            formatting_entities=entities
        )
        db.update_listing_fields(
            listing_id=existing["id"],
            platform_name=platform_name,
            intent=intent,
            header_word=header_word,
        )
        db.update_listing_content(
            listing_id=existing["id"],
            clean_text=ai_clean_text,
            rank_tier=None,
        )
        # TEL-3: refresh the dedup fingerprint from the edited raw text so a
        # re-processed/re-delivered version of this message is recognized.
        edit_price, _ = parser.extract_price(raw_text)
        db.set_listing_fingerprint(
            existing["id"],
            raw_text,
            price=edit_price,
        )
        db.record_audit("edited_destination", existing["id"], detail=f"source {source_msg_id}")
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
    supplier = resolve_supplier_for_event(event)
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
async def approved_listings_worker(
    client: TelegramClient, stop_event: asyncio.Event, bot_client: Optional[TelegramClient]
) -> None:
    """Publish admin-approved listings (DLQ-safe: requires published_message_id IS NULL)."""
    while not stop_event.is_set():
        try:
            approved = await asyncio.to_thread(db.get_approved_listings_to_publish, 5)
            if await asyncio.to_thread(db.is_paused):
                continue
            for listing in approved:
                listing_id = listing["id"]
                # clean_text already holds the AI-rewritten body (or the raw fallback
                # for listings captured before AI was available).
                content_text = listing.get("clean_text") or listing.get("raw_text") or ""
                content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
                intent = listing.get("intent") or "neutral"
                post_number = await asyncio.to_thread(db.next_post_number)

                out_text, entities = parser.build_ai_message(
                    content_lines=content_lines,
                    our_price=None,
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
                    await asyncio.to_thread(db.mark_listing_failed, listing_id, str(exc))
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
                    await asyncio.to_thread(db.mark_listing_failed, listing_id, str(exc)[:500])
                    logger.exception("Worker failed to publish approved listing #%s", listing_id)
                    await _alert_admin_on_failure(
                        bot_client, {"channel_username": listing.get("supplier_username")}, listing_id
                    )
                    continue

                try:
                    await asyncio.to_thread(
                        db.update_listing_status,
                        listing_id=listing_id,
                        status="published",
                        published_message_id=published_msg_id,
                        post_number=post_number,
                    )
                    await asyncio.to_thread(
                        db.record_audit,
                        "published_approved",
                        listing_id,
                        detail=published_msg_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Worker published approved listing #%s (msg id %s) but bookkeeping failed: %s",
                        listing_id, published_msg_id, exc,
                    )
                    try:
                        await asyncio.to_thread(
                            db.update_listing_status,
                            listing_id=listing_id,
                            status="published",
                            published_message_id=published_msg_id,
                            post_number=post_number,
                        )
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
            stats = await asyncio.to_thread(db.get_today_stats)
            logger.info(
                "Periodic Health Check: Active Suppliers=%s | Today Processed=%s | "
                "Published=%s | Pending=%s | Skipped=%s | Errors=%s",
                stats["active_suppliers"],
                stats["total_processed"],
                stats["published"],
                stats["pending"],
                stats["total_skipped"],
                stats["errors"],
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
    rephrasing instead. Already-AI bodies are left untouched to avoid churn."""
    if not ai_rephraser.is_available():
        logger.info("AI not available; skipping stale rephrase sweep.")
        return
    listings = db.get_unpublished_listings()
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
        db.update_listing_fields(
            listing_id,
            platform_name=analysis.get("platform"),
            intent=analysis.get("intent"),
        )
        db.update_listing_content(listing_id, clean_text=ai_clean_text)
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
    env_seed_notice = db.validate_env_seed_config(SOURCE_CHANNELS_RAW)
    if env_seed_notice:
        logger.warning("%s", env_seed_notice)
    env_seed_result = db.ensure_env_seed(SOURCE_CHANNELS, DEFAULT_MULTIPLIER)
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
                    supplier = resolve_supplier_for_event(event)
                    if supplier:
                        await process_supplier_message(user_client, bot_client, supplier, event.message)

                await _run_with_floodwait_retry(
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

                await _run_with_floodwait_retry(
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
            len(SOURCE_CHANNELS),
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
        for task in (health_task, resolve_task, rephrase_task):
            if task is not None:
                task.cancel()
        if backfill_task:
            backfill_task.cancel()
        # SHUT-1: actually await the cancelled tasks so their finally-blocks and
        # DB connection check-ins complete instead of leaking as orphans.
        pending_tasks = [worker_task]
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