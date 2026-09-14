"""Admin Bot for managing suppliers, checking status, and approving ambiguous listings."""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.tl.types import PeerChannel, PeerChat

import db
import parser
import publish_guard

load_dotenv()

logger = logging.getLogger("admin_bot")

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0)
DEST_CHANNEL = os.environ.get("DEST_CHANNEL", "")
CONTACT_USERNAME = os.environ.get("CONTACT_USERNAME", "")
DEFAULT_MULTIPLIER = float(os.environ.get("PRICE_MULTIPLIER", "0.75"))

# Statuses in which a listing may still be edited / approved / rejected. Once a
# listing leaves this set (published, failed, rejected...) any in-flight admin
# action (edit wizard, Approve tap) must be refused, not silently applied.
EDITABLE_LISTING_STATUSES = ("pending_approval", "pending_review")

# Skip digest: at most once per 10-minute window, only when there are NEW
# skipped messages since the last digest marker (SKIP-1). Each newly-skipped
# message goes out as ONE send_published_alert-style card (with its own
# Re-review + source-link buttons), capped per window so a burst of skips never
# floods the admin. The marker is persisted in app_settings so a restart never
# re-alerts old skips.
SKIP_DIGEST_MIN_INTERVAL = 10 * 60
SKIP_DIGEST_MAX_CARDS = 10
_skip_digest_last_sent = 0.0
_skip_digest_task: Optional[asyncio.Task] = None


def listing_is_editable(status: str) -> bool:
    return status in EDITABLE_LISTING_STATUSES

# Global reference to user_client if running unified under main.py
user_client_ref: Optional[TelegramClient] = None


def set_user_client(client: TelegramClient) -> None:
    """Set reference to the Telethon user client for immediate publishing on approval."""
    global user_client_ref
    user_client_ref = client


async def send_approval_prompt(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
) -> None:
    """Send a listing to the admin with inline Approve / Reject buttons."""
    listing_id = listing["id"]
    platform = listing.get("platform_name") or listing.get("game_name") or "Unknown"
    review_reason = listing.get("_review_reason", "")
    ai_preview = (listing.get("clean_text") or "")[:400]
    raw_preview = (listing.get("raw_text") or "")[:400]

    reason_label = {
        "unknown_platform": "Platform Not Identified",
        "ai_unavailable": "AI Unavailable — Manual Review",
        "ai_blocked_review": "⚠️ AI Flagged Content — Verify",
    }.get(review_reason, "Manual Review")
    header = f"📬 {reason_label} — Listing #{listing_id}"
    price_info = "Price : DM"

    platform_display = platform.title() if platform and platform != "Unknown" else "—"

    content_section = ai_preview if ai_preview else raw_preview

    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Supplier : {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))}\n"
        f"Platform : {platform_display}\n"
        f"{price_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{content_section}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Approve → republish  |  Reject → dismiss"
    )

    buttons = [
        [
            Button.inline("👁️ Preview",  data=f"preview:{listing_id}"),
            Button.inline("✏️ Edit",     data=f"edit:{listing_id}"),
            Button.inline("✅ Approve", data=f"approve:{listing_id}"),
            Button.inline("❌ Reject",  data=f"reject:{listing_id}"),
        ]
    ]

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode=None)
        logger.info("Sent approval prompt for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send approval prompt to admin for listing #%s", listing_id)


async def send_failed_alert(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
) -> None:
    """Notify the admin whenever a listing moves to the failed queue (DLQ)."""
    listing_id = listing["id"]
    error = listing.get("last_error") or "unknown error"
    retries = listing.get("retry_count", 0)
    raw_preview = (listing.get("clean_text") or listing.get("raw_text") or "")[:200]

    text = (
        f"⚠️ **Publish Failed — Listing #{listing_id}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Supplier : {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))}\n"
        f"Retries  : {retries}\n"
        f"Error    : `{(error or '')[:300]}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{raw_preview}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"The listing could not be published after retrying. "
        f"Tap **Retry** (or send `/retry {listing_id}`) to send it again, "
        f"or use `/failed` to see all failed listings."
    )

    buttons = [
        [
            Button.inline("👁️ Preview", data=f"preview:{listing_id}"),
            Button.inline("🔁 Retry", data=f"retry:{listing_id}"),
        ]
    ]

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode=None)
        logger.info("Sent failed alert for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send failed alert to admin for listing #%s", listing_id)


async def send_published_alert(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
) -> None:
    """Short DM to the admin after every auto-published post."""
    listing_id = listing["id"]
    post_number = listing.get("post_number")
    platform = (listing.get("platform_name") or listing.get("game_name") or "")[:60]
    post_disp = f"#{post_number}" if post_number is not None else f"Listing #{listing_id}"

    text = (
        f"✅ **Auto-Published — Post {post_disp}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{platform.title() if platform else 'Platform ?'}\n"
        f"Price    : DM\n"
        f"Supplier : {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + (f"Find it later with `/post {post_number}`." if post_number is not None else "")
    )

    buttons = []
    src_url = _source_url(listing)
    dest_url = _destination_url(listing)
    if src_url or dest_url:
        row = []
        if src_url:
            row.append(Button.url("📥 View in source channel", src_url))
        if dest_url:
            row.append(Button.url("📝 View in my channel", dest_url))
        buttons.append(row)

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode="markdown")
        logger.info("Sent published alert for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send published alert to admin for listing #%s", listing_id)


def _format_listing(n: int, l: dict) -> str:
    """Single-line listing summary for /pending, /failed, /preview lists."""
    platform = l.get("platform_name") or l.get("game_name") or "?"
    created = (l.get("created_at") or "")[:16].replace("T", " ")
    return (
        f"{n}. **#{l['id']}** | {platform} | DM | "
        f"{_pretty_source(l.get('supplier_username'), l.get('supplier_display_name'))}\n"
        f"   Status: `{l.get('status')}` | {created}"
    )


async def _build_preview_text(listing: dict) -> str:
    """Render the exact formatted post for a listing (used by /preview and 👁️ Preview button).

    Uses the stored AI-rewritten body (clean_text) directly — no re-AI call, so the
    button answers instantly and previews always match what will be published.
    """
    content_text = listing.get("clean_text") or listing.get("raw_text") or ""
    content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
    platform = listing.get("platform_name") or listing.get("game_name")
    intent = listing.get("intent") or "neutral"

    preview_text, _ = parser.build_ai_message(
        content_lines=content_lines,
        our_price=None,
        platform=platform,
        contact_username=CONTACT_USERNAME,
        intent=intent,
        header_word=listing.get("header_word"),
        listing_seed=listing["id"],
        post_number=listing.get("post_number"),
        source_text=listing.get("raw_text"),
    )
    return preview_text


def _repair_targets(max_posts: int = 100) -> List[dict]:
    """Find published posts whose body still contains leaked source prices /
    @handles / DM lines. Returns the diff of what IS published vs what a clean
    (sanitized) rebuild would be, with entities ready to apply via edit_message."""
    published = db.get_published_listings(limit=max_posts)
    targets: List[dict] = []
    for p in published:
        listing_id = p["id"]
        content_text = p.get("clean_text") or p.get("raw_text") or ""
        content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
        sanitized = parser.sanitize_body_lines(content_lines) or ["Available"]
        platform = p.get("platform_name") or p.get("game_name")
        intent = p.get("intent") or "neutral"
        post_number = p.get("post_number")

        current_text, _ = parser.build_ai_message(
            content_lines=content_lines,
            our_price=None,
            platform=platform,
            contact_username=CONTACT_USERNAME,
            intent=intent,
            header_word=p.get("header_word"),
            listing_seed=listing_id,
            post_number=post_number,
            sanitize_body=False,
            source_text=p.get("raw_text"),
        )
        repaired_text, entities = parser.build_ai_message(
            content_lines=sanitized,
            our_price=None,
            platform=platform,
            contact_username=CONTACT_USERNAME,
            intent=intent,
            header_word=p.get("header_word"),
            listing_seed=listing_id,
            post_number=post_number,
            source_text=p.get("raw_text"),
        )
        if repaired_text != current_text:
            targets.append({
                "listing_id": listing_id,
                "post_number": post_number,
                "published_message_id": p.get("published_message_id"),
                "current_text": current_text,
                "repaired_text": repaired_text,
                "entities": entities,
            })
    return targets


def _skip_notification(k: dict) -> Tuple[str, List[List[object]]]:
    """One send_published_alert-style card for a single skipped message.

    Mirrors the published-alert layout (header + divider + Supplier/Reason +
    snippet + divider) so skipped notifications read like their published
    counterparts. Each card carries its own Re-review button and a source-link
    button when the supplier's channel resolves to a t.me URL.
    """
    reason = (k.get("reason") or "unknown").replace("_", " ")
    header = f"⏳ **Skipped — {reason}**"
    src = _pretty_source(k.get("channel_username"), k.get("display_name")) or "?"
    snippet = (k.get("raw_text") or "").strip().replace("\n", " ")[:240]
    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Supplier : {src}\n"
        f"Reason   : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{snippet}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Nothing was published for this message."
    )
    buttons = [[Button.inline("🔁 Re-review", data=f"reskip:{k['skip_id']}")]]
    src_url = _source_url({
        "supplier_username": k.get("channel_username"),
        "supplier_channel_id": None,
        "source_message_id": k.get("message_id"),
    })
    if src_url:
        buttons[0].append(Button.url("📥 View in source channel", src_url))
    return text, buttons


async def skip_digest_worker(bot: TelegramClient) -> None:
    """Per-skip DM cards for skipped messages (SKIP-1): fires at most once per
    10-minute window, only when new skips exist since the last marker, sending
    ONE send_published_alert-style card per newly-skipped message (capped by
    SKIP_DIGEST_MAX_CARDS) so each card carries its own Re-review + source-link
    buttons. Immediate DMs are reserved for payment-proof detections, which
    never land in the skips table."""
    global _skip_digest_last_sent
    while True:
        try:
            await asyncio.sleep(60)
            if not bot.is_connected():
                continue
            marker = db.get_skip_digest_marker()
            now = time.monotonic()
            if now - _skip_digest_last_sent < SKIP_DIGEST_MIN_INTERVAL:
                continue
            recent = await asyncio.to_thread(db.get_skipped_listings, 1000)
            new_skips = [k for k in recent if k["skip_id"] > marker]
            if not new_skips:
                continue
            for k in new_skips[:SKIP_DIGEST_MAX_CARDS]:
                text, buttons = _skip_notification(k)
                try:
                    await bot.send_message(
                        ADMIN_USER_ID, text, buttons=buttons, parse_mode="markdown"
                    )
                except Exception:
                    logger.exception(
                        "Failed to send skip notification for skip #%s", k["skip_id"]
                    )
                    continue
            db.set_skip_digest_marker(max(k["skip_id"] for k in new_skips))
            _skip_digest_last_sent = now
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in skip digest worker")


def _listing_with_draft(listing: dict, draft: str) -> dict:
    """Copy of a listing whose content is taken from an admin draft."""
    updated = dict(listing)
    updated["clean_text"] = draft
    return updated


def _edit_prompt(listing: dict, existing_draft: Optional[str]) -> str:
    """Prompt for the ✏️ Edit wizard, seeded with the current content so the
    admin can copy-tweak-resend the body instead of retyping it from scratch.

    The current body comes from the last draft (if any), else the listing's
    clean_text, else its raw_text — rendered as a blockquote box that copies
    cleanly (Telegram blockquotes are formatting, the ``>`` markers are not
    part of the copied text)."""
    content = (existing_draft or "").strip()
    if not content:
        content = (listing.get("clean_text") or "").strip()
    if not content:
        content = (listing.get("raw_text") or "").strip()
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        body = "_(no content yet — write the body lines)_"
    else:
        body = "\n".join(f"> {ln}" for ln in lines)
    return (
        f"✏️ **Edit Listing #{listing['id']}**\n"
        f"**Current content** — copy it, tweak it, then send back the FULL body:\n"
        f">{body}\n\n"
        f"• Price & contact are added automatically.\n"
        f"Then I'll show you the new preview before publishing."
    )


# Home reply keyboard — persistent 1-tap UI. Labels reflect current state.
def _home_keyboard() -> List[List[object]]:
    pause_label = "▶ All Start" if db.is_paused() else "⏸ All Stop"
    return [
        [Button.text("📊 Status", resize=True), Button.text("⏳ Pending", resize=True), Button.text("⚠️ Failed", resize=True)],
        [Button.text("📋 Sources", resize=True), Button.text(pause_label, resize=True), Button.text("❓ Help", resize=True)],
        [Button.text("🚫 Skipped", resize=True), Button.text("📜 Published", resize=True), Button.text(_asleep_label(), resize=True)],
    ]


def _tgram_chat_link(chat_ref: Optional[str], message_id: Optional[int]) -> Optional[str]:
    """Build a t.me deep link for a message in a public channel ('@user') or supergroup id."""
    if not message_id:
        return None
    ref = (chat_ref or "").strip()
    if ref.startswith("@"):
        return f"https://t.me/{ref[1:]}/{message_id}"
    if ref.startswith("-100") and ref[1:].isdigit():
        return f"https://t.me/c/{ref[1:][3:]}/{message_id}"
    return None


def _source_url(listing: dict) -> Optional[str]:
    """Link back to the supplier's original source message."""
    ref = listing.get("supplier_username") or listing.get("supplier_channel_id")
    if ref is None:
        return None
    ref_str = str(ref)
    if not ref_str.startswith("@") and not ref_str.startswith("-100"):
        ref_str = f"@{ref_str}"
    return _tgram_chat_link(ref_str, listing.get("source_message_id"))


def _destination_url(listing: dict) -> Optional[str]:
    """Link back to the republished post in the destination channel."""
    if not listing.get("published_message_id"):
        return None
    ref = (DEST_CHANNEL or "").strip()
    if not ref:
        return None
    if not ref.startswith("@") and not ref.startswith("-100"):
        ref = f"@{ref}"
    return _tgram_chat_link(ref, listing.get("published_message_id"))


def _asleep_label() -> str:
    """Label for the 'I'm Asleep' toggle button (mirrors the pause label)."""
    return "☀️ I'm Awake" if db.is_buyer_asleep() else "😴 I'm Asleep"


async def _message_delete_send(
    event,
    text: str,
    buttons=None,
    parse_mode: str = "markdown",
) -> None:
    """Delete the tapped message and send a fresh one in its place.

    Keeps the admin chat clean: menu/section navigation and button actions stop
    editing-in-place (which left every old screen sitting in the chat forever)
    and instead replace the tapped message with a fresh one. Both steps are
    best-effort; a deleted source or blocked send degrades gracefully.
    """
    try:
        await event.delete()
    except Exception:
        pass
    try:
        await event.client.send_message(
            ADMIN_USER_ID, text, buttons=buttons, parse_mode=parse_mode
        )
    except Exception:
        logger.exception("Failed to send fresh message after delete-and-refresh")


def _listing_action_buttons(listing_id: int, status: str) -> List[List[object]]:
    """Inline actions shown with a preview: failed listings can be retried,
    pending ones can be edited, then approved or rejected."""
    if status in ("failed", "error"):
        return [[Button.inline("🔁 Retry", data=f"retry:{listing_id}")]]
    return [
        [
            Button.inline("✏️ Edit", data=f"edit:{listing_id}"),
            Button.inline("✅ Approve", data=f"approve:{listing_id}"),
            Button.inline("❌ Reject", data=f"reject:{listing_id}"),
        ]
    ]


# Wizard state: sender_id -> {"step": "add" | "edit", ...}
_wizard_state: Dict[int, dict] = {}
# Listing drafts: listing_id -> content lines typed by the admin via ✏️ Edit.
_drafts: Dict[int, str] = {}


def _pretty_source(username: Optional[str], display_name: Optional[str] = None) -> str:
    """Friendly source label for admin messages.

    Uses display_name when set (a real username renders as '@name', a channel
    title or numeric id renders as-is); otherwise falls back to the stored
    username. Never renders '@-1003824...' for numeric-id suppliers.
    """
    raw = (display_name or username or "").strip()
    if not raw:
        return "?"
    if db.is_numeric_identifier(raw) or any(c.isspace() for c in raw):
        return raw
    return raw if raw.startswith("@") else f"@{raw}"


def _channel_id_from_fwd(fwd) -> Optional[int]:
    """Extract the original chat/channel id from a MessageFwdHeader.

    This is the single most reliable way to add a private channel/group with no
    public username: when the admin forwards any post `from` it, the header
    carries the exact chat id regardless of how the account identifies it.
    """
    if fwd is None:
        return None
    from_id = getattr(fwd, "from_id", None)
    if isinstance(from_id, PeerChannel):
        return from_id.channel_id
    if isinstance(from_id, PeerChat):
        return from_id.chat_id
    # Legacy Telethon exposes the id directly on the header.
    for attr in ("channel_id", "chat_id"):
        cid = getattr(fwd, attr, None)
        if cid:
            return int(cid)
    saved_from_peer = getattr(fwd, "saved_from_peer", None)
    if isinstance(saved_from_peer, PeerChannel):
        return saved_from_peer.channel_id
    if isinstance(saved_from_peer, PeerChat):
        return saved_from_peer.chat_id
    return None


def _supplier_ref_from_text(text: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse a channel reference -> (username_str, numeric_id_or_None).

    Accepts '@handle', bare 'handle', 't.me/handle' links and numeric ids like
    '-1001234567890'. Usernames are lowercased and stripped of '@'; numeric ids
    are returned as ints.
    """
    ref = (text or "").strip()
    if not ref:
        return None, None
    m = re.match(
        r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)$",
        ref,
        re.IGNORECASE,
    )
    if m:
        ref = m.group(1)
    if ref.lstrip("-").isdigit():
        return str(int(ref)), int(ref)
    return ref.lstrip("@").lower(), None


def _source_status_icon(s: dict) -> str:
    """🟢 active · 🔴 paused · ⚠️ unresolved (so dead suppliers are never invisible again)."""
    if not s.get("channel_id"):
        return "⚠️"
    return "🟢" if s.get("active") else "🔴"


def _entity_display(entity, fallback: Optional[str] = None) -> str:
    """Friendly label for a resolved Telethon entity."""
    if entity is None:
        return fallback or "?"
    name = (
        (getattr(entity, "username", None) or "").strip()
        or (getattr(entity, "title", None) or "").strip()
    )
    return name or fallback or f"channel {getattr(entity, 'id', '?')}"


async def _edit_supplier_menu(event, s: dict) -> None:
    """Render the supplier detail menu (used by the detail view and after toggle)."""
    sid = s["id"]
    icon = _source_status_icon(s)
    handle = _pretty_source(s.get("channel_username"), s.get("display_name"))
    toggle_label = "⏸ Pause" if s["active"] else "▶ Resume"
    if not s.get("channel_id"):
        status_line = (
            "⚠️ **Unresolved — NOT monitored yet.** Wrong username or private chat; "
            "re-add it or forward a message from the channel.\n"
        )
    else:
        status_line = f"Status: `{'Active' if s['active'] else 'Paused'}`\n"
    await _message_delete_send(
        event,
        f"{icon} **{handle}**\n"
        f"{status_line}"
        f"ID: `{s['channel_id'] or 'unresolved'}`\n\n"
        f"What would you like to do?",
        buttons=[
            [Button.inline(toggle_label, data=f"suptoggle:{sid}")],
            [Button.inline("🗑 Delete Permanently", data=f"supdel:{sid}")],
            [Button.inline("⬅️ Back", data="menu:sources")],
        ],
    )


async def _run_add_supplier_flow(event, text: str, fwd=None) -> None:
    """Resolve-first, then-persist add flow used by /addsupplier and the wizard.

    - Attempts to resolve the reference to a real Telegram entity via the user
      client BEFORE any DB write.
    - Never inserts a silently-dead row: if resolution fails the admin is told
      why and given the "forward a message" alternative, or an explicit
      "Add anyway (retry later)" confirmation (handled by the supaddunresolved
      callback) — never an automatic NULL-channel_id insert.
    - Re-uses an existing supplier row by channel_id (no duplicate rows for the
      same real channel).
    """
    if fwd is not None:
        raw_channel_id = _channel_id_from_fwd(fwd)
        if raw_channel_id is None:
            await event.reply(
                "That forward isn't from a channel/group I can identify. "
                "Forward a post **made by the channel** (not a message you typed "
                "yourself), or send a username / numeric ID instead.",
                buttons=_home_keyboard(),
            )
            return
        channel_id = db.normalize_channel_id(raw_channel_id)
        entity = None
        if user_client_ref and user_client_ref.is_connected():
            try:
                entity = await user_client_ref.get_entity(channel_id)
            except Exception:
                entity = None
        entity_username = (
            (getattr(entity, "username", None) or "").strip().lstrip("@") or None
        )
        display = _entity_display(entity, fallback=f"channel {channel_id}")
        sid = db.add_supplier(
            entity_username or str(channel_id),
            channel_id=channel_id,
            markup_multiplier=DEFAULT_MULTIPLIER,
        )
        if display:
            db.set_supplier_display_name(str(channel_id), display)
        db.record_audit(
            "supplier_added", sid, actor_id=ADMIN_USER_ID,
            detail=f"forwarded post -> {display} (id {channel_id})",
        )
        if entity is None:
            await event.reply(
                f"✅ **Source added by forward** — stored ID `{channel_id}`.\n"
                f"⚠️ My monitor account can't see this chat yet. Add the monitor "
                f"account as a member and I'll start listening automatically.",
                buttons=_home_keyboard(),
            )
        else:
            await event.reply(
                f"✅ **Source added by forward**: `{display}` (ID `{channel_id}`)",
                buttons=_home_keyboard(),
            )
        return

    username, numeric = _supplier_ref_from_text(text)
    if not username:
        await event.reply(
            "I couldn't read a channel reference from that. Send a channel "
            "username (e.g. `@kycgroupke`), a numeric ID (e.g. `-1001234567890`), "
            "or forward a message from the channel.",
            buttons=_home_keyboard(),
        )
        return

    entity = None
    if user_client_ref and user_client_ref.is_connected():
        reference = int(numeric) if numeric is not None else username
        try:
            entity = await user_client_ref.get_entity(reference)
        except Exception as exc:
            logger.warning("Could not resolve supplier reference %r: %s", username, exc)

    if entity is not None:
        entity_id = db.normalize_channel_id(entity)
        entity_username = (
            (getattr(entity, "username", None) or "").strip().lstrip("@") or None
        )
        display = _entity_display(entity, fallback=username)
        store_username = entity_username or (str(entity_id) if entity_id else username)
        sid = db.add_supplier(
            store_username,
            channel_id=entity_id,
            markup_multiplier=DEFAULT_MULTIPLIER,
        )
        if entity_id and display:
            db.set_supplier_display_name(str(entity_id), display)
        db.record_audit(
            "supplier_added", sid, actor_id=ADMIN_USER_ID,
            detail=f"{text} -> {display} (id {entity_id})",
        )
        await event.reply(
            f"✅ **Source added**: `{display}` (ID `{entity_id}`)",
            buttons=_home_keyboard(),
        )
        return

    _wizard_state[ADMIN_USER_ID] = {"step": "add_confirm_unresolved", "raw": text}
    await event.reply(
        f"❌ Couldn't resolve that reference (`{text[:60]}`).\n\n"
        f"• If this is a **private group/channel with no username**, forward me any "
        f"message **from that channel/group** and I'll grab its exact ID automatically.\n"
        f"• Or tap **Add anyway** to store it unresolved — I'll retry in the background "
        f"and message you the moment monitoring actually starts.\n\n"
        f"Until it resolves, the source will show as ⚠️ unresolved and won't be listened to.",
        buttons=[
            [Button.inline("✅ Add anyway (retry later)", data="supaddunresolved")],
            [Button.inline("🚫 Cancel", data="wiz:cancel")],
        ],
        parse_mode=None,
    )


def _sources_menu_text(suppliers: List[dict]) -> str:
    if not suppliers:
        return "No sources configured yet."
    return "Tap a source below to manage it."


def _sources_buttons(suppliers: List[dict]) -> List[List[object]]:
    buttons = []
    for s in suppliers[:12]:
        icon = _source_status_icon(s)
        label = f"{icon} {_pretty_source(s.get('channel_username'), s.get('display_name'))}"
        buttons.append([Button.inline(label, data=f"sup:{s['id']}")])
    buttons.append([
        Button.inline("➕ Add Source", data="supadd"),
        Button.inline("⬅️ Back", data="menu:home"),
    ])
    return buttons


def _home_button_row() -> List[List[object]]:
    """Inline "back to Home" row used by every section rendered from the Home menu."""
    return [[Button.inline("🏠 Home", data="menu:home")]]


def _home_text() -> str:
    return "🏠 **Home** — tap a button below."


def _home_inline_keyboard() -> List[List[object]]:
    """Inline navigation for the Home landing message (mirrors the reply keyboard)."""
    pause_label = "▶ All Start" if db.is_paused() else "⏸ All Stop"
    return [
        [
            Button.inline("📊 Status", data="home:status"),
            Button.inline("⏳ Pending", data="home:pending"),
            Button.inline("⚠️ Failed", data="home:failed"),
        ],
        [
            Button.inline("📋 Sources", data="menu:sources"),
            Button.inline(pause_label, data="home:toggle"),
            Button.inline("❓ Help", data="home:help"),
        ],
        [
            Button.inline("🚫 Skipped", data="home:skipped"),
            Button.inline("📜 Published", data="home:published"),
        ],
        [
            Button.inline(_asleep_label(), data="home:asleep"),
        ],
    ]


def _help_text() -> str:
    return (
        "🤖 **Telegram Monitor Admin Bot**\n\n"
        "Everything is one tap — no commands to remember:\n"
        "• **📊 Status** — today's report\n"
        "• **⏳ Pending** — approve / preview / edit new listings\n"
        "• **⚠️ Failed** — retry failed publishes\n"
        "• **📋 Sources** — add, manage, remove sources\n"
        "• **📜 Published** — every post with its **#Post number** + channel & source links\n"
        "• **🔢 /post 12** — jump straight to post #12\n"
        "• **⏸ All Stop / ▶ All Start** — pause or resume AUTOMATIC publishing only. "
        "Manual Approve taps still publish immediately.\n\n"
        "Slash shortcuts still work if you prefer typing them."
    )


def _status_report_text() -> str:
    stats = db.get_today_stats()

    supplier_lines = []
    for s in stats["supplier_breakdown"]:
        handle = _pretty_source(s.get("channel_username"), s.get("supplier_display_name"))
        supplier_lines.append(
            f"• {handle} — processed `{s['processed']}` / "
            f"published `{s['published']}` / skipped `{s['skipped']}`"
        )
    if not supplier_lines:
        supplier_lines = ["• None"]
    supplier_text = "\n".join(supplier_lines)

    reason_lines = [f"• {r}: `{c}`" for r, c in stats["skip_reasons"].items()]
    if not reason_lines:
        reason_lines = ["• None"]
    reason_text = "\n".join(reason_lines)

    msg = (
        "📊 **Today's Activity Report**\n\n"
        f"• Active Suppliers: `{stats['active_suppliers']}`\n"
        f"• Total Processed: `{stats['total_processed']}`\n"
        f"• Published: `{stats['published']}`\n"
        f"• Pending Approval: `{stats['pending']}`\n"
        f"• Total Skipped: `{stats['total_skipped']}`\n"
        f"• Errors: `{stats['errors']}`\n\n"
        f"**By Supplier**\n{supplier_text}\n\n"
        f"**Skip Breakdown**\n{reason_text}"
    )
    if db.is_paused():
        msg = "⏸ **PAUSED — automatic publishing is stopped**\n(manual Approve taps still publish)\n\n" + msg
    return msg


def _failed_digest(failed: List[dict]) -> Tuple[str, List[List[object]]]:
    lines = ["⚠️ **Failed Listings (DLQ)** — tap an action below:\n"]
    for i, l in enumerate(failed, 1):
        lines.append(_format_listing(i, l))
        error = l.get("last_error")
        if error:
            lines.append(f"   └ Last error: `{(error or '')[:120]}`")
    buttons = []
    for l in failed:
        buttons.append([
            Button.inline(f"👁️ #{l['id']} Preview", data=f"preview:{l['id']}"),
            Button.inline(f"🔁 #{l['id']} Retry", data=f"retry:{l['id']}"),
        ])
    buttons.extend(_home_button_row())
    return "\n".join(lines), buttons


def _relative_time(iso_ts: Optional[str]) -> str:
    """Compact human age for a skip timestamp ('2h ago'), '' when unknown.

    Stdlib only; naive timestamps are assumed UTC (skips are stored aware via
    datetime.now(timezone.utc).isoformat())."""
    if not iso_ts:
        return ""
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = int(max((datetime.now(timezone.utc) - ts).total_seconds(), 0))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 7:
        return f"{days}d ago"
    return f"{days // 7}w ago"


# Per-reason icon map for the /skipped screen: reasons are scannable without
# reading text. Every reason produced by the codebase (filters.py constants +
# main.py's log_skip call sites) is covered; unknown/legacy rows get a neutral
# fallback. Keep in sync with filters.REASON_* and main.py log_skip callers.
SKIP_REASON_ICONS = {
    "duplicate": "🔁",      # fingerprint duplicate within DEDUP_HOURS
    "not_a_listing": "💬",  # AI judged the message as not a listing
    "chatter": "🗨️",        # obvious chatter: rule posts, welcomes, pins
    "no_content": "⬜",     # empty / whitespace-only body
    "self_echo": "🔄",      # our own destination output looped back to a source
}


def _skip_reason_icon(reason: Optional[str]) -> str:
    """Distinct icon per skip reason ('📄' for anything unmapped)."""
    return SKIP_REASON_ICONS.get((reason or "").strip(), "📄")


def _skipped_digest(skips: List[dict]) -> Tuple[str, List[List[object]]]:
    """Buttons-only recent-skips list.

    The body is intentionally empty (a single em-dash placeholder): the "Skipped"
    context already reads from the button that opened the screen, and each
    Re-review button label carries a reason icon + reason + supplier + age
    ('🔁 duplicate · @kycgroupke · 2h ago') so you can pick the right one at a
    glance instead of reading a wall. Skips with no linked listing can't be
    reopened, so those drop to a one-line count instead of a dead text entry."""
    reopenable = [k for k in skips if k.get("listing_id")]
    buttons = []
    for k in reopenable:
        reason = (k.get("reason") or "unknown").replace("_", " ")
        src = _pretty_source(k.get("channel_username"), k.get("display_name")) or "?"
        age = _relative_time(k.get("timestamp"))
        label = f"{_skip_reason_icon(k.get('reason'))} {reason} · {src}"
        if age:
            label += f" · {age}"
        buttons.append([Button.inline(label, data=f"reskip:{k['skip_id']}")])
    if reopenable:
        text = "—"
        dropped = len(skips) - len(reopenable)
        if dropped:
            text += f"\n_({dropped} more recent skip(s) not re-reviewable — no listing linked.)_"
    else:
        text = "**Skipped** — none of the recent skips can be re-opened."
    buttons.extend(_home_button_row())
    return text, buttons


def _published_digest(rows: List[dict]) -> Tuple[str, List[List[object]]]:
    """Buttons-only published list: per post ONE row of two no-emoji URL buttons.

    Button A links the post's #number to the published post in our channel;
    Button B links the source group/channel name to the original source post.
    No text lines: the button labels carry all the meaning."""
    text = "📜 **Published posts** — tap a button to open a post:\n"
    buttons = []
    for p in rows:
        post_num = p.get("post_number")
        num_disp = f"#{post_num}" if post_num is not None else f"Listing {p.get('id')}"
        src_disp = _pretty_source(p.get("supplier_username"), p.get("supplier_display_name"))
        if not src_disp or src_disp == "?":
            platform = (p.get("platform_name") or p.get("game_name") or "").title()
            src_disp = platform or "Source"
        src_url = _source_url(p)
        dest_url = _destination_url(p)
        row = []
        if dest_url:
            row.append(Button.url(num_disp, dest_url))
        if src_url:
            row.append(Button.url(src_disp, src_url))
        if row:
            buttons.append(row)
    buttons.extend(_home_button_row())
    return text, buttons


def setup_admin_handlers(bot: TelegramClient) -> None:
    """Register command and callback handlers for the admin bot."""

    async def check_admin(event) -> bool:
        if event.sender_id != ADMIN_USER_ID:
            logger.warning(
                "Message received from unauthorized sender_id %s (configured ADMIN_USER_ID is %s)",
                event.sender_id,
                ADMIN_USER_ID,
            )
            await event.reply(
                f"⚠️ Access Denied. Your Telegram User ID is: {event.sender_id}"
            )
            return False
        return True

    @bot.on(events.NewMessage(pattern=r"^(?:/start|/help|❓ Help)"))
    async def handle_start(event):
        if not await check_admin(event):
            return
        await event.reply(_help_text(), buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^(?:/suppliers|/sources|📋 Sources)"))
    async def handle_suppliers(event):
        if not await check_admin(event):
            return
        suppliers = db.list_suppliers(active_only=False)
        text = _sources_menu_text(suppliers)
        if suppliers:
            await event.reply(
                text,
                buttons=_sources_buttons(suppliers),
                parse_mode=None,
            )
        else:
            await event.reply(
                text + "\nTap **➕ Add Source** to configure your first one.",
                buttons=_sources_buttons(suppliers),
                parse_mode=None,
            )

    @bot.on(events.NewMessage(pattern=r"^(?:/pause|/resume|⏸ All Stop|▶ All Start)$"))
    async def handle_pause_toggle(event):
        if not await check_admin(event):
            return
        paused = db.is_paused()
        db.set_paused(not paused)
        state_label = "⏸ **Paused**" if not paused else "▶ **Resumed**"
        await event.reply(
            f"{state_label}. New listings are still captured and shown here for review. "
            f"Automatic publishing is {'stopped' if not paused else 'running'} — "
            f"but manual **Approve** taps always publish immediately.",
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )

    @bot.on(events.NewMessage(pattern=r"^(?:😴 I'm Asleep|☀️ I'm Awake)$"))
    async def handle_asleep_toggle(event):
        if not await check_admin(event):
            return
        asleep = db.is_buyer_asleep()
        db.set_buyer_asleep(not asleep)
        if not asleep:
            state_label = (
                "😴 **Asleep** — every new post now carries the footer\n"
                "`Buyer away, back shortly`\n"
                "Use the button again to switch it back off."
            )
        else:
            state_label = "☀️ **Awake** — posts go out with their normal footer again."
        await event.reply(state_label, buttons=_home_keyboard(), parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/addsupplier(?:\s+(.+))?"))
    async def handle_add_supplier(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "**Add a source** — any of these work:\n"
                "• Channel username: `@kycgroupke`\n"
                "• Numeric ID: `-1001234567890`\n"
                "• **Forward a message FROM the channel/group** — best for private "
                "chats with no username (I grab its exact ID automatically).\n\n"
                "Example: `/addsupplier @kycgroupke`",
                buttons=_home_keyboard(),
            )
            return
        await _run_add_supplier_flow(event, arg)

    @bot.on(events.NewMessage(pattern=r"^/removesupplier(?:\s+(.+))?"))
    async def handle_remove_supplier(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "Usage: `/removesupplier <channel_username>`\n"
                "Example: `/removesupplier @kycgroupke`\n"
                "This **permanently deletes** the source (history is kept). For a "
                "temporary stop use the Sources menu → Pause.",
                buttons=_home_keyboard(),
            )
            return

        s = db.get_supplier_by_chat(username=arg.lstrip("@"))
        if not s:
            await event.reply(f"❌ Supplier **@{arg.lstrip('@')}** not found.", buttons=_home_keyboard())
            return
        db.delete_supplier(s["id"])
        db.record_audit("supplier_deleted", None, actor_id=event.sender_id, detail=f"@{arg.lstrip('@')}")
        await event.reply(f"🗑 Supplier **@{arg.lstrip('@')}** permanently deleted.", buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^/dedupe_suppliers$"))
    async def handle_dedupe_suppliers(event):
        if not await check_admin(event):
            return
        summary = db.dedupe_suppliers()
        merges = summary.get("merges", [])
        if not merges:
            await event.reply(
                "🧹 No duplicate suppliers found — the table is already clean.",
                buttons=_home_keyboard(),
            )
            return
        lines = [f"🧹 **Merged {summary['rows_removed']} duplicate supplier row(s):**"]
        for m in merges:
            lines.append(
                f"• row `#{m['removed']}` (stored `{m.get('duplicate_username') or '?'}`) "
                f"merged into row `#{m['merged_into']}` (channel `{m['channel_id']}`)"
            )
        for m in merges:
            db.record_audit(
                "supplier_dedupe", None, actor_id=event.sender_id, detail=str(m)
            )
        await event.reply("\n".join(lines), buttons=_home_keyboard(), parse_mode="markdown")
        suppliers = db.list_suppliers(active_only=False)
        await event.client.send_message(
            ADMIN_USER_ID,
            _sources_menu_text(suppliers),
            buttons=_sources_buttons(suppliers),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/reseed_from_env$"))
    async def handle_reseed_from_env(event):
        if not await check_admin(event):
            return
        raw = os.environ.get("SOURCE_CHANNELS", "")
        channels = [ch.strip() for ch in raw.split(",") if ch.strip()]
        preview = ", ".join(f"`{c}`" for c in channels) if channels else "*(none listed in .env)*"
        await event.reply(
            "⚠️ **Re-import SOURCE_CHANNELS from .env?**\n\n"
            f"Currently in .env: {preview}\n\n"
            "This is the deliberate escape hatch only:\n"
            "• Adds/refreshes every channel listed in .env.\n"
            "• Does **NOT** delete anything — channels removed from .env are not removed here.\n"
            "• After this, .env is ignored again on future restarts.",
            buttons=[
                [Button.inline("✅ Yes, Re-import", data="reseed:yes")],
                [Button.inline("❌ Cancel", data="wiz:cancel")],
            ],
        )

    @bot.on(events.NewMessage(pattern=r"^(?:/status|📊 Status)"))
    async def handle_status(event):
        if not await check_admin(event):
            return
        await event.reply(_status_report_text(), buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^(?:/pending|⏳ Pending)"))
    async def handle_pending(event):
        if not await check_admin(event):
            return
        pending = db.get_pending_listings(limit=5)
        if not pending:
            await event.reply("✅ No listings pending approval right now.", buttons=_home_keyboard())
            return

        await event.reply(
            f"Found {len(pending)} listing(s) pending review:", buttons=_home_keyboard()
        )
        for l in pending:
            await send_approval_prompt(bot, ADMIN_USER_ID, l)

    @bot.on(events.NewMessage(pattern=r"^(?:/failed|⚠️ Failed)"))
    async def handle_failed(event):
        if not await check_admin(event):
            return
        failed = db.get_failed_listings(limit=10)
        if not failed:
            await event.reply("✅ No failed publishes in the queue.", buttons=_home_keyboard())
            return

        text, buttons = _failed_digest(failed)
        await event.reply(text, buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^(?:/skipped|🚫 Skipped)"))
    async def handle_skipped(event):
        if not await check_admin(event):
            return
        skips = db.get_skipped_listings(limit=15)
        if not skips:
            await event.reply("✅ No skipped messages logged.", buttons=_home_keyboard())
            return

        text, buttons = _skipped_digest(skips)
        await event.reply(text, buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/repair(?:\s+(do|list))?"))
    async def handle_repair(event):
        if not await check_admin(event):
            return
        mode = (event.pattern_match.group(1) or "list").lower()
        targets = _repair_targets()

        if mode != "do":
            if not targets:
                await event.reply(
                    "✅ No published posts need repair — all bodies are clean.",
                    buttons=_home_keyboard(),
                )
                return
            nums = ", ".join(f"#{t['post_number'] or '?'}" for t in targets)
            lines = [
                f"🔧 **Repair dry-run** — {len(targets)} published post(s) would be edited "
                f"in place (leaked prices/@handles/DM lines removed):\n"
                f"Posts: {nums}\n"
                f"Detailed before/after for the first {min(5, len(targets))} below.\n",
            ]
            for t in targets[:5]:
                lines.append(
                    f"━━━ **Post #{t['post_number'] or '?'}** · listing #{t['listing_id']} ━━━\n"
                    f"**now:**\n{t['current_text']}\n"
                    f"**after:**\n{t['repaired_text']}"
                )
            lines.append(
                "\nRun `/repair do` to apply to all of them. "
                "**Review carefully — this EDITS the live channel.**"
            )
            await event.reply("\n\n".join(lines), buttons=_home_keyboard(), parse_mode="markdown")
            return

        if not targets:
            await event.reply("✅ Nothing to repair.", buttons=_home_keyboard())
            return
        if not user_client_ref or not user_client_ref.is_connected() or not DEST_CHANNEL:
            await event.reply(
                "⚠️ User client not connected — can't edit the destination channel.",
                buttons=_home_keyboard(),
            )
            return
        done = 0
        failed = 0
        for t in targets:
            try:
                await publish_guard.throttle()
                await user_client_ref.edit_message(
                    DEST_CHANNEL,
                    t["published_message_id"],
                    t["repaired_text"],
                    formatting_entities=t["entities"],
                )
                db.record_audit(
                    "repair_edited", t["listing_id"], actor_id=event.sender_id,
                    detail=f"post #{t['post_number']}",
                )
                done += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Repair edit failed for post %s", t.get("post_number"))
                failed += 1
        await event.reply(
            f"🔧 Repair done: **{done} edited**, {failed} failed.",
            buttons=_home_keyboard(),
        )
    @bot.on(events.NewMessage(pattern=r"^(?:/published|📜 Published)"))
    async def handle_published(event):
        if not await check_admin(event):
            return
        rows = db.get_published_listings(limit=10)
        if not rows:
            await event.reply("📜 No published posts yet.", buttons=_home_keyboard())
            return

        text, buttons = _published_digest(rows)
        await event.reply(text, buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/post(?:[ \t]+(\d+))?"))
    async def handle_post(event):
        if not await check_admin(event):
            return
        arg = event.pattern_match.group(1)
        if not arg:
            await event.reply(
                "Usage: `/post <number>`\n"
                "Shows the published post with that **Post number** (e.g. `/post 12`) "
                "and its source-channel link.",
                buttons=_home_keyboard(),
            )
            return
        post = db.get_post_by_number(int(arg))
        if not post:
            await event.reply(
                f"❌ No published post with number `#{arg}`.",
                buttons=_home_keyboard(),
            )
            return
        platform = (post.get("platform_name") or post.get("game_name") or "?").title()
        supplier = post.get("supplier_username")
        supplier_chat = post.get("supplier_channel_id")
        src_disp = _pretty_source(supplier, post.get("supplier_display_name"))
        if not src_disp or src_disp == "?":
            src_disp = f"channel {supplier_chat}"
        published = (post.get("published_at") or "")[:16].replace("T", " ")
        lines = [
            f"🔢 **Post #{arg}**",
            "━━━━━━━━━━━━━━━━━━━━",
            f"Platform : {platform}",
            f"Price    : DM",
            f"Supplier : {src_disp}",
            f"Published: {published}",
            f"Listing  : `#{post.get('id')}`",
        ]
        buttons = []
        src_url = _source_url(post)
        if src_url:
            buttons.append([Button.url("📥 View in source channel", src_url)])
        buttons.append([Button.inline("🏠 Home", data="menu:home")])
        await event.reply("\n".join(lines), buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/retry(?:[ \t]+(\d+))?"))
    async def handle_retry(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "Usage: `/retry <listing_id>`\nRe-queues a failed listing for publishing.",
                buttons=_home_keyboard(),
            )
            return

        listing_id = int(arg)
        listing = db.get_listing_by_id(listing_id)
        if not listing:
            await event.reply(f"❌ Listing **#{listing_id}** not found.", buttons=_home_keyboard())
            return
        if listing["status"] not in ("failed", "error"):
            await event.reply(
                f"⚠️ Listing **#{listing_id}** is not in a failed state (current: `{listing['status']}`).",
                buttons=_home_keyboard(),
            )
            return

        ok = db.requeue_listing(listing_id)
        db.record_audit("requeue", listing_id, actor_id=event.sender_id)
        if ok:
            await event.reply(
                f"🔁 Listing **#{listing_id}** re-queued for publishing. "
                f"The republisher will pick it up shortly.",
                buttons=_home_keyboard(),
            )
        else:
            await event.reply(f"❌ Could not re-queue listing **#{listing_id}**.", buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^/preview(?:[ \t]+(\d+))?"))
    async def handle_preview(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "Usage: `/preview <listing_id>`\nShows the exact formatted post before publishing.",
                buttons=_home_keyboard(),
            )
            return

        listing = db.get_listing_by_id(int(arg))
        if not listing:
            await event.reply(f"❌ Listing **#{arg}** not found.", buttons=_home_keyboard())
            return

        try:
            preview_text = await _build_preview_text(listing)
            await event.reply(
                f"📄 **Preview of Listing #{arg}**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{preview_text}",
                buttons=_listing_action_buttons(listing["id"], listing["status"]),
            )
        except Exception as exc:
            logger.warning("Preview too long to send for listing #%s: %s", arg, exc)
            await event.reply(
                f"⚠️ Preview for **#{arg}** is too long to display inline. "
                f"Use `/pending` or find it via `/failed`.",
                buttons=_home_keyboard(),
            )

    @bot.on(events.CallbackQuery)
    async def handle_callback(event):
        logger.info(
            "CallbackQuery received: sender=%s data=%r",
            event.sender_id,
            (event.data or b"").decode("utf-8", "replace")[:64],
        )
        # Validate admin via sender_id (callback queries can't use event.reply)
        if event.sender_id != ADMIN_USER_ID:
            await event.answer("⛔ Unauthorized", alert=True)
            return

        data_str = event.data.decode("utf-8")

        # ---- Skipped re-review ---------------------------------------------
        if data_str.startswith("reskip:"):
            try:
                skip_id = int(data_str.split(":", 1)[1])
            except ValueError:
                await event.answer("Invalid skip id", alert=True)
                return
            reopened = db.reopen_skipped(skip_id)
            if not reopened:
                await event.answer("Skip not found or already processed.", alert=True)
                return
            db.record_audit(
                "skipped_reopen", reopened["id"], actor_id=event.sender_id,
                detail=f"skip #{skip_id}",
            )
            # Delete the tapped message — whether it's one of the new per-skip
            # cards or the aggregated /skipped list (that whole list goes away,
            # which is the accepted tradeoff for never leaving stale screens).
            try:
                await event.delete()
            except Exception:
                pass
            listing_dict = db.get_listing_by_id(reopened["id"])
            if listing_dict:
                listing_dict["_review_reason"] = "skipped_reopen"
                await send_approval_prompt(bot, ADMIN_USER_ID, listing_dict)
            return

        # ---- Pure navigation -----------------------------------------------
        if data_str == "menu:home":
            await event.answer("🏠 Home")
            await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
            return

        if data_str == "menu:sources":
            suppliers = db.list_suppliers(active_only=False)
            text = _sources_menu_text(suppliers)
            if not suppliers:
                text += "\nTap **➕ Add Source** to configure your first one."
            await _message_delete_send(event, text, buttons=_sources_buttons(suppliers), parse_mode=None)
            return

        if data_str.startswith("home:"):
            home_action = data_str.split(":", 1)[1]
            if home_action == "status":
                await _message_delete_send(event, _status_report_text(), buttons=_home_button_row())
                return
            if home_action == "pending":
                pending = db.get_pending_listings(limit=5)
                if not pending:
                    await _message_delete_send(
                        event,
                        "✅ No listings pending approval right now.",
                        buttons=_home_button_row(),
                    )
                    return
                await _message_delete_send(
                    event,
                    f"Found {len(pending)} listing(s) pending review:",
                    buttons=_home_button_row(),
                )
                for l in pending:
                    await send_approval_prompt(bot, ADMIN_USER_ID, l)
                return
            if home_action == "failed":
                failed = db.get_failed_listings(limit=10)
                if not failed:
                    await _message_delete_send(
                        event,
                        "✅ No failed publishes in the queue.",
                        buttons=_home_button_row(),
                    )
                    return
                text, buttons = _failed_digest(failed)
                await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
                return
            if home_action == "help":
                await _message_delete_send(event, _help_text(), buttons=_home_button_row())
                return
            if home_action == "toggle":
                paused = db.is_paused()
                db.set_paused(not paused)
                await event.answer(
                    "⏸ Automatic publishing paused" if not paused else "▶ Automatic publishing resumed"
                )
                await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
                return
            if home_action == "asleep":
                asleep = db.is_buyer_asleep()
                db.set_buyer_asleep(not asleep)
                await event.answer(
                    "😴 Asleep — footer added to new posts" if not asleep
                    else "☀️ Awake — footer removed"
                )
                await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
                return
            if home_action == "skipped":
                skips = db.get_skipped_listings(limit=15)
                if not skips:
                    await _message_delete_send(
                        event,
                        "✅ No skipped messages logged.",
                        buttons=_home_button_row(),
                    )
                    return
                text, buttons = _skipped_digest(skips)
                await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
                return
            if home_action == "published":
                rows = db.get_published_listings(limit=10)
                if not rows:
                    await _message_delete_send(
                        event,
                        "📜 No published posts yet.",
                        buttons=_home_button_row(),
                    )
                    return
                text, buttons = _published_digest(rows)
                await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
                return
            return

        if data_str == "wiz:cancel":
            _wizard_state.pop(ADMIN_USER_ID, None)
            await event.answer("Cancelled")
            try:
                await event.delete()
            except Exception:
                pass
            return

        # ---- Suppliers submenu ---------------------------------------------
        sup_match = re.match(r"^sup:(\d+)$", data_str)
        if sup_match:
            sid = int(sup_match.group(1))
            s = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            await _edit_supplier_menu(event, s)
            return

        suptoggle_match = re.match(r"^suptoggle:(\d+)$", data_str)
        if suptoggle_match:
            sid = int(suptoggle_match.group(1))
            s = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            db.set_supplier_active(s["channel_username"], not s["active"])
            db.record_audit(
                "supplier_toggle", None, actor_id=ADMIN_USER_ID, detail=str(sid)
            )
            refreshed = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if refreshed:
                await _edit_supplier_menu(event, refreshed)
            return

        supdel_match = re.match(r"^supdel:(\d+)$", data_str)
        if supdel_match:
            sid = int(supdel_match.group(1))
            s = db.get_supplier_by_id(sid)
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            handle = _pretty_source(s.get("channel_username"), s.get("display_name"))
            await _message_delete_send(
                event,
                f"🗑 **Delete {handle} permanently?**\n"
                f"This removes the source completely and stops monitoring it.\n"
                f"Existing listings & history stay (their source link becomes '—').\n"
                f"This cannot be undone.",
                buttons=[
                    [Button.inline("✅ Yes, Delete Forever", data=f"del:yes:{sid}")],
                    [Button.inline("❌ Cancel", data="wiz:cancel")],
                ],
            )
            return

        del_match = re.match(r"^del:yes:(\d+)$", data_str)
        if del_match:
            sid = int(del_match.group(1))
            s = db.get_supplier_by_id(sid)
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            db.delete_supplier(sid)
            db.record_audit(
                "supplier_deleted", None, actor_id=ADMIN_USER_ID, detail=str(sid)
            )
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    f"🗑 **Source permanently deleted.**\n"
                    f"{_pretty_source(s.get('channel_username'), s.get('display_name'))} "
                    f"is gone from the list. History is kept.",
                    parse_mode="markdown",
                )
            except Exception:
                pass
            suppliers = db.list_suppliers(active_only=False)
            await event.client.send_message(
                ADMIN_USER_ID,
                _sources_menu_text(suppliers),
                buttons=_sources_buttons(suppliers),
                parse_mode=None,
            )
            return

        if data_str == "reseed:yes":
            try:
                raw = os.environ.get("SOURCE_CHANNELS", "")
                channels = [ch.strip() for ch in raw.split(",") if ch.strip()]
                db.clear_env_seed_completed()
                n = db.seed_suppliers_from_env(channels, DEFAULT_MULTIPLIER)
                db.mark_env_seed_completed()
                db.record_audit(
                    "env_reseed", None, actor_id=ADMIN_USER_ID, detail=f"re-seeded {n} supplier(s)"
                )
                try:
                    await event.delete()
                except Exception:
                    pass
                try:
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        f"✅ Re-imported {n} channel(s) from SOURCE_CHANNELS.\n"
                        "The one-time seed marker is set again — .env won't be consulted "
                        "on future restarts.",
                        buttons=_home_keyboard(),
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("reseed_from_env failed")
                await event.answer("Re-seed failed — check the logs.", alert=True)
            return

        if data_str == "supaddunresolved":
            state = _wizard_state.get(ADMIN_USER_ID) or {}
            raw = state.get("raw") if state.get("step") == "add_confirm_unresolved" else None
            if not raw:
                await event.answer("That request has expired — press ➕ Add Source to start again.", alert=True)
                return
            _wizard_state.pop(ADMIN_USER_ID, None)
            username, _ = _supplier_ref_from_text(raw)
            if not username:
                await event.answer("Invalid reference.", alert=True)
                return
            sid = db.add_supplier(username, channel_id=None, markup_multiplier=DEFAULT_MULTIPLIER)
            db.record_audit(
                "supplier_added_unresolved", sid, actor_id=ADMIN_USER_ID, detail=raw
            )
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    f"✅ Source **@{username}** stored as **unresolved**.\n"
                    f"I'll retry resolving it in the background and ping you the moment "
                    f"monitoring actually starts for it.\n\n"
                    f"Faster: forward any message **from that channel** and I'll add it instantly.",
                    buttons=_home_keyboard(),
                    parse_mode="markdown",
                )
            except Exception:
                pass
            return

        if data_str == "supadd":
            _wizard_state[ADMIN_USER_ID] = {"step": "add"}
            await _message_delete_send(
                event,
                "✏️ **Add a source** — any of these work:\n"
                "• Channel username: `@kycgroupke`\n"
                "• Numeric ID: `-1001234567890`\n"
                "• **Forward a message FROM the channel/group** — best for private "
                "chats with no username (I'll grab its exact ID automatically).\n\n"
                "Send any of the above now.",
                buttons=[Button.inline("🚫 Cancel", data="wiz:cancel")],
                parse_mode="markdown",
            )
            return

        # ---- Previews / listing actions ------------------------------------
        preview_match = re.match(r"^preview:(\d+)$", data_str)
        if preview_match:
            listing = db.get_listing_by_id(int(preview_match.group(1)))
            if not listing:
                await event.answer("Listing not found in database.", alert=True)
                return
            listing_id = listing["id"]
            # Answer the button IMMEDIATELY so Telegram doesn't consider the
            # callback expired (which made the button appear dead).
            await event.answer(f"📍 Building preview of Listing #{listing_id}")
            try:
                preview_text = await _build_preview_text(listing)
            except Exception:
                logger.exception("Failed to build preview for listing #%s", listing_id)
                await event.answer("Could not build preview for this listing.", alert=True)
                return
            # Delete the tapped prompt card; the preview is the new anchor message.
            try:
                await event.delete()
            except Exception:
                pass
            await event.client.send_message(
                ADMIN_USER_ID,
                f"📄 **Preview of Listing #{listing_id}**\n"
                f"Source: {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))} · Status: `{listing['status']}`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{preview_text}",
                buttons=_listing_action_buttons(listing_id, listing["status"]),
                parse_mode="markdown",
            )
            return

        edit_match = re.match(r"^edit:(\d+)$", data_str)
        if edit_match:
            listing = db.get_listing_by_id(int(edit_match.group(1)))
            if not listing:
                await event.answer("Listing not found in database.", alert=True)
                return
            if not listing_is_editable(listing["status"]):
                await event.answer(f"Cannot edit — status is {listing['status']}", alert=True)
                return
            listing_id = listing["id"]
            _wizard_state[ADMIN_USER_ID] = {"step": "edit", "listing_id": listing_id}
            # Delete the tapped prompt; the wizard prompt below becomes the new anchor.
            try:
                await event.delete()
            except Exception:
                pass
            try:
                sent = await event.client.send_message(
                    ADMIN_USER_ID,
                    _edit_prompt(listing, _drafts.get(listing_id)),
                    buttons=[Button.inline("🚫 Cancel", data="wiz:cancel")],
                    parse_mode="markdown",
                )
                prompt_id = getattr(sent, "id", None)
                if prompt_id:
                    _wizard_state[ADMIN_USER_ID]["prompt_message_id"] = prompt_id
            except Exception:
                logger.exception("Failed to send edit prompt for listing #%s", listing_id)
            return

        match = re.match(r"^(approve|reject|retry):(\d+)$", data_str)
        if not match:
            await event.answer("Unknown action", alert=True)
            return

        action, listing_id_str = match.groups()
        listing_id = int(listing_id_str)
        listing = db.get_listing_by_id(listing_id)

        if not listing:
            await event.answer("Listing not found in database.", alert=True)
            return

        if action == "retry":
            if listing["status"] not in ("failed", "error"):
                await event.answer(f"Already processed (status: {listing['status']})", alert=True)
                return
            ok = db.requeue_listing(listing_id)
            db.record_audit("requeue", listing_id, actor_id=event.sender_id)
            if ok:
                try:
                    await event.delete()
                except Exception:
                    pass
                try:
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        f"🔁 Listing #{listing_id} re-queued for publishing. "
                        f"The republisher will pick it up shortly.",
                        buttons=_home_keyboard(),
                        parse_mode="markdown",
                    )
                except Exception:
                    await event.answer(f"Re-queued #{listing_id}", alert=True)
            else:
                await event.answer("Re-queue failed.", alert=True)
            return

        # Approve / Reject only valid on pending listings (never on already-approved,
        # preventing double-publish races on re-taps).
        if not listing_is_editable(listing["status"]):
            await event.answer(
                f"Already processed (status: {listing['status']})", alert=True
            )
            return

        if action == "reject":
            _drafts.pop(listing_id, None)
            db.update_listing_status(listing_id, "rejected")
            db.record_audit("rejected", listing_id, actor_id=event.sender_id)
            await event.answer("❌ Rejected")
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    f"❌ Listing #{listing_id} rejected and dismissed.",
                    buttons=_home_keyboard(),
                    parse_mode="markdown",
                )
            except Exception:
                pass
            return

        if action == "approve":
            # Re-check the listing's CURRENT status immediately before applying any
            # draft. The listing loaded above is a snapshot taken when the callback
            # arrived; if it has since moved out of the editable set (published,
            # rejected, failed, requeued) the draft must NOT be silently applied to
            # a stale listing. Discard it and say so instead.
            fresh = db.get_listing_by_id(listing_id)
            if fresh is not None and not listing_is_editable(fresh["status"]):
                _drafts.pop(listing_id, None)
                await event.answer(
                    f"⚠️ Listing #{listing_id} is no longer editable "
                    f"(status: {fresh['status']}). Draft discarded — nothing was published.",
                    alert=True,
                )
                await _message_delete_send(
                    event,
                    f"⚠️ **Listing #{listing_id} was NOT published.**\n"
                    f"Status is now `{fresh['status']}`, so the draft you were "
                    f"editing was discarded.\n"
                    f"Check the Pending list for its current state.",
                    buttons=_home_keyboard(),
                )
                return

            # A draft (from ✏️ Edit) replaces the source content.
            draft = _drafts.pop(listing_id, None)
            content_text = draft or listing.get("clean_text") or listing.get("raw_text") or ""
            content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
            platform_name = listing.get("platform_name") or listing.get("game_name")
            intent = listing.get("intent") or "neutral"

            await event.answer("Processing...")
            # Approve is a deliberate one-at-a-time human decision: it always
            # publishes immediately, even while "All Stop" is on. Pausing only
            # gates AUTOMATIC publishing (auto-publish paths in main.py).
            # The tap was consumed: delete the prompt so no stale screen lingers,
            # then any confirmation below is a fresh message, never an in-place edit.
            try:
                await event.delete()
            except Exception:
                pass

            post_number = db.next_post_number()
            republished_text, entities = parser.build_ai_message(
                content_lines=content_lines,
                our_price=None,
                platform=platform_name,
                contact_username=CONTACT_USERNAME,
                intent=intent,
                header_word=listing.get("header_word"),
                listing_seed=listing_id,
                post_number=post_number,
                source_text=listing.get("raw_text"),
            )

            if user_client_ref and user_client_ref.is_connected() and DEST_CHANNEL:
                # CONC-3: share the same lock + rate-limit as the worker and the
                # auto-publish path so admin-approve and worker never interleave.
                await publish_guard.throttle()
                published_msg_id = None
                try:
                    sent_msg = await user_client_ref.send_message(
                        DEST_CHANNEL, republished_text,
                        formatting_entities=entities
                    )
                    published_msg_id = sent_msg.id
                except Exception as e:
                    logger.exception("Send failed for listing #%s via user_client: %s", listing_id, e)
                    # Nothing was sent — safe to re-queue for the worker.
                    db.update_listing_status(listing_id, "approved")
                    db.record_audit(
                        "approved_queued", listing_id, actor_id=event.sender_id, detail=str(e)[:200]
                    )
                    try:
                        await event.client.send_message(
                            ADMIN_USER_ID,
                            f"⚠️ Listing #{listing_id} approved but send failed.\n"
                            f"Queued for retry. Error: {e}",
                            buttons=_home_keyboard(),
                            parse_mode="markdown",
                        )
                    except Exception:
                        pass
                    return

                # Message IS on the channel now — under no circumstance re-queue it.
                try:
                    db.update_listing_status(
                        listing_id=listing_id,
                        status="published",
                        published_message_id=published_msg_id,
                        post_number=post_number,
                    )
                    db.record_audit(
                        "published_admin", listing_id,
                        actor_id=event.sender_id, detail=published_msg_id,
                    )
                except Exception as exc:
                    logger.exception(
                        "Listing #%s WAS published (msg id %s) but bookkeeping failed: %s",
                        listing_id, published_msg_id, exc,
                    )
                    try:
                        db.update_listing_status(
                            listing_id=listing_id,
                            status="published",
                            published_message_id=published_msg_id,
                            post_number=post_number,
                        )
                    except Exception:
                        logger.exception("Could not record published state for listing #%s", listing_id)

                try:
                    confirmation = (
                        f"✅ **Listing #{listing_id} Published!**\n"
                        f"📌 **Post #{post_number}**\n"
                        f"Channel: {DEST_CHANNEL}\n"
                        f"Price: `DM`"
                    )
                    await event.client.send_message(
                        ADMIN_USER_ID, confirmation, parse_mode="markdown",
                    )
                except Exception:
                    pass
                return
            else:
                db.update_listing_status(listing_id, "approved")
                db.record_audit(
                    "approved", listing_id, actor_id=event.sender_id,
                    detail="queued for worker publish",
                )
                try:
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        f"✅ Listing #{listing_id} marked approved.\n"
                        f"Will be dispatched to {DEST_CHANNEL} by the republisher.\n"
                        f"(User listener not connected — sent via the worker queue.)",
                        buttons=_home_keyboard(),
                        parse_mode="markdown",
                    )
                except Exception:
                    pass

    @bot.on(events.NewMessage())
    async def handle_wizard_input(event):
        """Fallback: complete the Add Source wizard by plain-text reply
        or by forwarding a message from the channel."""
        if not await check_admin(event):
            return
        state = _wizard_state.get(ADMIN_USER_ID)

        fwd = (
            getattr(event.message, "fwd_from", None)
            if getattr(event, "message", None)
            else None
        )
        if state and state.get("step") == "add" and fwd is not None:
            _wizard_state.pop(ADMIN_USER_ID, None)
            await _run_add_supplier_flow(event, None, fwd=fwd)
            return

        text = event.text
        if not text:
            return
        text = text.strip()
        if text.startswith("/"):
            return
        # Menu taps must not be swallowed while a wizard is waiting
        if text in ("📊 Status", "⏳ Pending", "⚠️ Failed", "📋 Sources",
                    "⏸ All Stop", "▶ All Start", "❓ Help", "📜 Published",
                    "😴 I'm Asleep", "☀️ I'm Awake"):
            _wizard_state.pop(ADMIN_USER_ID, None)
            return

        if not state:
            return
        prompt_message_id = state.get("prompt_message_id")
        _wizard_state.pop(ADMIN_USER_ID, None)

        if state["step"] == "add":
            await _run_add_supplier_flow(event, text)
            return

        if state["step"] == "edit":
            listing_id = state.get("listing_id")
            listing = db.get_listing_by_id(listing_id)
            if not listing:
                await event.reply("❌ Listing not found.", buttons=_home_keyboard())
                return
            if not listing_is_editable(listing["status"]):
                _drafts.pop(listing_id, None)
                await event.reply(
                    f"⚠️ Cannot edit Listing #{listing_id}: its status is now "
                    f"`{listing['status']}`, so it is no longer editable. "
                    f"Nothing was saved.",
                    buttons=_home_keyboard(),
                )
                return
            draft = text
            _drafts[listing_id] = draft
            try:
                preview_text = await _build_preview_text(_listing_with_draft(listing, draft))
            except Exception:
                logger.exception("Could not build draft preview for listing #%s", listing_id)
                await event.reply(
                    "⚠️ Could not build a preview from that text. Try again.",
                    buttons=_home_keyboard(),
                )
                return
            # The "send your draft" prompt is spent — delete it so the chat shows
            # only the draft preview, then the Approve/Edit buttons on it.
            if prompt_message_id:
                try:
                    await bot.delete_messages(ADMIN_USER_ID, [prompt_message_id])
                except Exception:
                    pass
            await event.reply(
                f"✏️ **Draft for Listing #{listing_id}** — review below, "
                f"then Approve or Edit again.\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{preview_text}",
                buttons=_listing_action_buttons(listing_id, listing["status"]),
                parse_mode="markdown",
            )
            return


async def create_admin_bot_client() -> TelegramClient:
    """Initialize and start the Telethon bot client.

    This is the ONLY documented way to run the admin bot: as part of main.py
    (unified mode) or `python main.py --manual` (manual mode). The legacy
    `python admin_bot.py` standalone entry point was removed: without a user
    client its approve taps could only queue listings, never publish them.
    """
    db.init_db()
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required in .env for admin bot")
    if not API_ID or not API_HASH:
        raise ValueError("API_ID and API_HASH are required in .env")

    bot = TelegramClient("admin_bot_session", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    setup_admin_handlers(bot)

    # Register command menu with Telegram so it appears in the Menu button
    try:
        from telethon.tl.functions.bots import SetBotCommandsRequest
        from telethon.tl.types import BotCommand, BotCommandScopeDefault
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[
                BotCommand(command="status", description="📊 Today's stats report"),
                BotCommand(command="pending", description="⏳ Pending approval listings"),
                BotCommand(command="failed", description="⚠️ Failed publishes (DLQ)"),
                BotCommand(command="skipped", description="🚫 Recently skipped messages"),
                BotCommand(command="sources", description="📋 Manage monitored sources"),
                BotCommand(command="published", description="📜 Published posts & channel links"),
                BotCommand(command="post", description="🔢 Look up a post by its number: /post 12"),
                BotCommand(command="repair", description="🔧 Edit leaked prices/handles out of live posts"),
                BotCommand(command="help", description="❓ Show buttons and shortcuts"),
            ]
        ))
    except Exception as e:
        logger.warning("Could not set bot commands menu: %s", e)

    # Skip digest: at most once per window, one card per newly-skipped message
    # (capped at SKIP_DIGEST_MAX_CARDS); the marker persists across restarts.
    global _skip_digest_task
    _skip_digest_task = asyncio.create_task(skip_digest_worker(bot))

    logger.info("Admin bot started successfully.")
    return bot