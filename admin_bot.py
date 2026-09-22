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

# Statuses in which a listing may still be edited / approved / skipped. Once a
# listing leaves this set (published, failed, skipped...) any in-flight admin
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

# Inbox review session: the card currently on screen shows its position as
# "1/4", "2/4", … where the total is snapshotted when the inbox opens (/pending)
# and "done" counts cards already shown. Falls back to "N remaining" when a new
# listing arrives mid-session or no inbox session is active.
_review_session_total: Optional[int] = None
_review_session_done: int = 0

# FloodWait budget for interactive admin send/edit actions (shared helper in
# publish_guard). Capped so a large flood can never hang the
# admin bot's event loop indefinitely — on exhaustion the action fails through
# into its existing error path (e.g. re-queue for the worker).
APPROVE_FLOODWAIT_BUDGET_SECONDS = float(
    os.environ.get("APPROVE_FLOODWAIT_BUDGET_SECONDS", "120") or 120
)


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
    as_next: bool = False,
    queue_pos: Optional[str] = None,
) -> None:
    """Send a listing to the admin with inline Approve / Skip buttons.

    ``as_next`` marks the card as the automatically-advanced next review in the
    inbox flow (header becomes '📬 Next Review — #id'). ``queue_pos`` — e.g.
    "1/4" or "3 remaining" — is rendered in the header so the admin always sees
    where the current card sits in the inbox."""
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

    if queue_pos:
        if as_next:
            header = f"📬 Next Review · {queue_pos} — Listing #{listing_id}"
        else:
            header = f"📬 {queue_pos} — {reason_label} — Listing #{listing_id}"
    elif as_next:
        header = f"📬 Next Review — Listing #{listing_id}"
    else:
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
        f"Approve → republish  |  Skip → for later"
    )

    buttons = [
        [
            Button.inline("👁️ Preview",  data=f"preview:{listing_id}"),
            Button.inline("✏️ Edit",     data=f"edit:{listing_id}"),
            Button.inline("✅ Approve", data=f"approve:{listing_id}"),
            Button.inline("⏭️ Skip",    data=f"skip:{listing_id}"),
        ]
    ]
    buttons.extend(_channel_view_buttons(listing))

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode=None)
        logger.info("Sent approval prompt for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send approval prompt to admin for listing #%s", listing_id)


async def send_skipped_alert(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
    reason: str,
) -> None:
    """One-line DM telling the admin whenever an inbound message gets skipped."""
    listing_id = listing["id"]
    src = _pretty_source(listing.get("supplier_username"), listing.get("supplier_display_name")) or "?"

    text = (
        f"⏳ Skipped — {reason}: {src} — Listing #{listing_id} — "
        f"tap /skipped to review."
    )

    try:
        await bot_client.send_message(admin_id, text, parse_mode=None)
        logger.info("Sent skipped alert for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send skipped alert to admin for listing #%s", listing_id)


async def send_review_notification(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
) -> None:

    listing_id = listing["id"]
    review_reason = listing.get("_review_reason") or ""
    src = _pretty_source(
        listing.get("supplier_username"), listing.get("supplier_display_name")
    )
    src_part = f" from {src}\n" if src and src != "?" else ""
    text = (
        f"📬 Review needed : Listing #{listing_id} \n {src_part} "
        f"\n tap /pending to review."
    )

    try:
        await bot_client.send_message(admin_id, text, parse_mode=None)
        logger.info("Sent review notification for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send review notification for listing #%s", listing_id)


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
        f"━━━━━━━━━━━━━━━━━\n"
        f"{platform.title() if platform else 'Platform ?'}\n"
        f"Supplier : {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))}\n"
        f"━━━━━━━━━━━━━━━━━\n"
    )

    buttons = _channel_view_buttons(listing)

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode="markdown")
        logger.info("Sent published alert for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send published alert to admin for listing #%s", listing_id)


def _format_listing(n: int, l: dict) -> str:
    """Single-line listing summary for /pending, /preview lists."""
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
        platform=platform,
        contact_username=CONTACT_USERNAME,
        intent=intent,
        header_word=listing.get("header_word"),
        listing_seed=listing["id"],
        post_number=listing.get("post_number"),
        source_text=listing.get("raw_text"),
    )
    return preview_text


def _skip_notification(k: dict) -> Tuple[str, List[List[object]]]:
    """One-line notification for a single skipped message.

    Compact by design: just the reason + supplier on one line, with a small
    Re-review button (and a source-link button when the supplier's channel
    resolves to a t.me URL) so it is actionable without being a big card.
    """
    reason = (k.get("reason") or "unknown").replace("_", " ")
    src = _pretty_source(k.get("channel_username"), k.get("display_name")) or "?"
    listing_part = f" — Listing #{k['listing_id']}" if k.get("listing_id") else ""
    text = (
        f"⏳ Skipped — {reason}: {src}{listing_part} — "
        f"tap /skipped to review."
    )
    buttons = [[Button.inline("🔁 Re-review", data=f"reskip:{k['skip_id']}")]]
    src_url = _source_url({
        "supplier_username": k.get("channel_username"),
        "supplier_channel_id": None,
        "source_message_id": k.get("message_id"),
    })
    if src_url:
        buttons[0].append(Button.url("View in Buyer channel", src_url))
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
            new_skips = [
                k for k in recent
                if k["skip_id"] > marker and k.get("reason") != "admin_skip"
            ]
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
            await db.run_async(
                db.set_skip_digest_marker,
                max(k["skip_id"] for k in new_skips),
            )
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
# Destinations is a reply-keyboard BUTTON, so its tap arrives as plain text that
# must be caught by a text-tap router (exactly like 📋 Sources). The label and
# the router share one constant, so a rename on either side can't silently break
# the button again. The legacy '📤' label is also routed so keyboards rendered
# before the label swap keep working.
DESTINATIONS_BTN = "📥 Destinations"
DESTINATIONS_BTN_LEGACY = "📤 Destinations"
DESTINATIONS_ROUTE_RE = (
    r"^(?:/destinations|"
    + DESTINATIONS_BTN
    + r"|"
    + DESTINATIONS_BTN_LEGACY
    + r")$"
)


def _home_keyboard() -> List[List[object]]:
    pause_label = "▶ All Start" if db.is_paused() else "⏸ All Stop"
    return [
        [Button.text("📊 Status", resize=True), Button.text("⏳ Pending", resize=True)],
        [Button.text("📋 Sources", resize=True), Button.text(DESTINATIONS_BTN, resize=True), Button.text(pause_label, resize=True)],
        [Button.text("🚫 Skipped", resize=True), Button.text("✅ Published", resize=True), Button.text(_asleep_label(), resize=True)],
        [Button.text("❓ Help", resize=True)],
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


def _channel_view_buttons(listing: dict) -> List[List[object]]:
    """One URL row linking back to the source ('Buyer channel') and, once a
    post is live, forward to the republished copy in the destination channel.
    Empty list when neither URL can be resolved."""
    row = []
    src_url = _source_url(listing)
    if src_url:
        row.append(Button.url("View in Buyer channel", src_url))
    dest_url = _destination_url(listing)
    if dest_url:
        row.append(Button.url("View in my channel", dest_url))
    if not row:
        return []
    return [row]


def _asleep_label() -> str:
    """Label for the 'I'm Asleep' toggle button (mirrors the pause label)."""
    return "☀️ I'm Awake" if db.is_buyer_asleep() else "💤 I'm Asleep"


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


def _listing_action_buttons(listing: dict) -> List[List[object]]:
    """Inline actions shown with a preview: pending ones can be edited, then
    approved or skipped. A 'View in Buyer channel' link is appended when the
    source post resolves to a t.me URL."""
    listing_id = listing["id"]
    status = listing["status"]
    if status in ("failed", "error"):
        buttons = []
    else:
        buttons = [
            [
                Button.inline("✏️ Edit", data=f"edit:{listing_id}"),
                Button.inline("✅ Approve", data=f"approve:{listing_id}"),
                Button.inline("⏭️ Skip", data=f"skip:{listing_id}"),
            ]
        ]
    buttons.extend(_channel_view_buttons(listing))
    return buttons


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

    Accepts '@handle', bare 'handle', 't.me/handle' links, private chat links like
    't.me/c/1234567890/123', and numeric ids like '-1001234567890'.
    Usernames are lowercased and stripped of '@'; numeric ids are returned as ints.
    """
    ref = (text or "").strip()
    if not ref:
        return None, None
    # Support t.me/c/1234567890/123 private channel links
    m_c = re.match(
        r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)(?:/\d+)?/?$",
        ref,
        re.IGNORECASE,
    )
    if m_c:
        bare_id = int(m_c.group(1))
        marked = db.normalize_channel_id(bare_id)
        return str(marked), marked
    # Support t.me/+hash or t.me/joinchat/hash invite links
    m_inv = re.match(
        r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)/?$",
        ref,
        re.IGNORECASE,
    )
    if m_inv:
        return ref, None
    m = re.match(
        r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/?$",
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
        sid = await db.run_async(
            db.add_supplier,
            entity_username or str(channel_id),
            channel_id=channel_id,
        )
        if display:
            await db.run_async(db.set_supplier_display_name, str(channel_id), display)
        await db.run_async(
            db.record_audit,
            "supplier_added",
            sid,
            actor_id=ADMIN_USER_ID,
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
        sid = await db.run_async(
            db.add_supplier,
            store_username,
            channel_id=entity_id,
        )
        if entity_id and display:
            await db.run_async(db.set_supplier_display_name, str(entity_id), display)
        await db.run_async(
            db.record_audit,
            "supplier_added",
            sid,
            actor_id=ADMIN_USER_ID,
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


_PAGE_SIZE = 6


def _page_window(total: int, page: int) -> Tuple[int, int, int, bool, bool]:
    """Compute pagination indices for a zero-based ``page``.

    Returns ``(start, end, page_count, has_prev, has_next)``. Negative pages
    clamp to 0; pages past the final page clamp to the last valid page;
    ``total=0`` yields an empty first page so callers render the normal empty
    result instead of crashing. The returned ``start`` is always ``page * _PAGE_SIZE``
    for the clamped page.
    """
    if total <= 0:
        return 0, 0, 1, False, False
    page_count = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
    page = max(0, min(int(page), page_count - 1))
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    return start, end, page_count, page > 0, end < total


def _nav_row(kind: str, page: int, page_count: int) -> List[List[object]]:
    """Pagination row: [⬅️ Prev] [2/4] [➡️ Next]; empty when single-page.

    The center indicator is a self-referencing inline button so it renders as a
    static label while still satisfying Telegram's inline-button requirement."""
    if page_count <= 1:
        return []
    row = []
    if page > 0:
        row.append(Button.inline("⬅️ Prev", data=f"{kind}:page:{page - 1}"))
    row.append(Button.inline(f"{page + 1}/{page_count}", data=f"{kind}:page:{page}"))
    if page + 1 < page_count:
        row.append(Button.inline("➡️ Next", data=f"{kind}:page:{page + 1}"))
    return [row]


def _page_footer(page: int, page_count: int) -> str:
    """'Page X of Y' for multi-page lists; '' for a single page."""
    if page_count <= 1:
        return ""
    return f"Page {page + 1} of {page_count}"


def _sources_menu_text(suppliers: List[dict], page: int = 0) -> str:
    if not suppliers:
        return "No sources configured yet."
    _, _, page_count, _, _ = _page_window(len(suppliers), page)
    footer = _page_footer(page, page_count)
    msg = "Tap a source below to manage it."
    if footer:
        msg += f"\n\n{footer}"
    return msg


def _sources_buttons(suppliers: List[dict], page: int = 0) -> List[List[object]]:
    start, end, page_count, _, _ = _page_window(len(suppliers), page)
    buttons = []
    for s in suppliers[start:end]:
        icon = _source_status_icon(s)
        label = f"{icon} {_pretty_source(s.get('channel_username'), s.get('display_name'))}"
        buttons.append([Button.inline(label, data=f"sup:{s['id']}")])
    buttons.extend(_nav_row("sup", page, page_count))
    buttons.append([
        Button.inline("➕ Add Source", data="supadd"),
        Button.inline("⬅️ Back", data="menu:home"),
    ])
    return buttons


# ---- Destinations submenu (DEST-1) ----------------------------------------
def _destination_icon(d: dict) -> str:
    return "🟢" if d.get("active") else "🔴"


def _destination_label(d: dict) -> str:
    title = (d.get("title") or "").strip()
    ref = str(d.get("chat_id") or "")
    if title:
        return title
    if ref.startswith("@"):
        return ref
    if ref.lstrip("-").isdigit():
        return f"chat {ref}"
    return ref or "?"


def _destinations_menu_text(destinations: List[dict], page: int = 0) -> str:
    if not destinations:
        return (
            "No destinations configured — \n\nTap **➕ Add Destination** to start forwarding "
            "posts there too."
        )
    _, _, page_count, _, _ = _page_window(len(destinations), page)
    footer = _page_footer(page, page_count)
    msg = "\nTap a destination below to manage it.\n\n"
    if footer:
        msg += footer + "\n"
    return msg


def _destinations_buttons(destinations: List[dict], page: int = 0) -> List[List[object]]:
    start, end, page_count, _, _ = _page_window(len(destinations), page)
    buttons = []
    for d in destinations[start:end]:
        icon = _destination_icon(d)
        label = f"{icon} {_destination_label(d)}"
        buttons.append([Button.inline(label, data=f"dest:{d['id']}")])
    buttons.extend(_nav_row("dest", page, page_count))
    buttons.append([
        Button.inline("➕ Add Destination", data="destadd"),
        Button.inline("⬅️ Back", data="menu:home"),
    ])
    return buttons


async def _edit_destination_menu(event, d: dict) -> None:
    """Render the destination detail menu (also used after an enable/disable tap)."""
    did = d["id"]
    icon = _destination_icon(d)
    label = _destination_label(d)
    toggle_label = "⏸ Pause " if d["active"] else "▶ Resume"
    await _message_delete_send(
        event,
        f"{icon} **{label}**\n"
        f"Status: `{'Active' if d['active'] else 'Disabled'}`\n"
        f"ID: `{d['chat_id']}`\n\n"
        f"What would you like to do?",
        buttons=[
            [Button.inline(toggle_label, data=f"desttoggle:{did}")],
            [Button.inline("🗑 Delete Permanently", data=f"destdel:{did}")],
            [Button.inline("⬅️ Back", data="menu:destinations")],
        ],
    )


async def _run_add_destination_flow(event, text: str, fwd=None) -> None:
    """Resolve-first, then-persist add flow used by /adddestination and the wizard.

    A destination is a chat the bot-forwards posts TO, so resolution matters
    less than for sources (delivery just needs a peer): numeric ids and '@'
    usernames are stored as-is; an unresolvable username is confirmed explicitly
    before being saved (destaddunresolved) instead of guessing silently.
    """
    if fwd is not None:
        raw_chat_id = _channel_id_from_fwd(fwd)
        if raw_chat_id is None:
            await event.reply(
                "That forward isn't from a channel/group I can identify. "
                "Forward a post **made by the group** (not a message you typed "
                "yourself), or send a username / numeric ID instead.",
                buttons=_home_keyboard(),
            )
            return
        chat_ref = db.normalize_channel_id(raw_chat_id)
        entity = None
        if user_client_ref and user_client_ref.is_connected():
            try:
                entity = await user_client_ref.get_entity(chat_ref)
            except Exception:
                try:
                    await user_client_ref.get_dialogs(limit=50)
                    entity = await user_client_ref.get_entity(chat_ref)
                except Exception:
                    entity = None
        display = _entity_display(entity, fallback=f"chat {chat_ref}")
        await db.run_async(db.add_destination, chat_ref, display, True)
        await db.run_async(
            db.record_audit,
            "destination_added",
            None,
            actor_id=ADMIN_USER_ID,
            detail=f"forwarded post -> {display} (id {chat_ref})",
        )
        await event.reply(
            f"✅ **Destination added by forward**: `{display}` (ID `{chat_ref}`)\n"
            f"Every bot post published from now on will be forwarded here.",
            buttons=_home_keyboard(),
        )
        return

    username, numeric = _supplier_ref_from_text(text)
    raw_text = (text or "").strip()
    if not username and not raw_text.startswith("http"):
        await event.reply(
            "I couldn't read a chat reference from that. Send a group username "
            "(e.g. `@mygroup`), a numeric ID (e.g. `-1001234567890`), or forward "
            "a message from the group.",
            buttons=_home_keyboard(),
        )
        return

    entity = None
    if user_client_ref and user_client_ref.is_connected():
        reference = int(numeric) if numeric is not None else (username or raw_text)
        try:
            entity = await user_client_ref.get_entity(reference)
        except Exception as exc:
            logger.warning("Could not resolve destination reference %r: %s", reference, exc)
            try:
                await user_client_ref.get_dialogs(limit=50)
                entity = await user_client_ref.get_entity(reference)
            except Exception:
                pass

    if entity is not None:
        entity_id = db.normalize_channel_id(entity)
        entity_username = (
            (getattr(entity, "username", None) or "").strip().lstrip("@") or None
        )
        display = _entity_display(entity, fallback=username or raw_text)
        store_ref = (
            f"@{entity_username.lower()}"
            if entity_username
            else (str(entity_id) if entity_id else username or raw_text)
        )
        await db.run_async(db.add_destination, store_ref, display, True)
        await db.run_async(
            db.record_audit,
            "destination_added",
            None,
            actor_id=ADMIN_USER_ID,
            detail=f"{text} -> {display}",
        )
        await event.reply(
            f"✅ **Destination added**: `{display}`\n"
            f"Every bot post published from now on will be forwarded here.",
            buttons=_home_keyboard(),
        )
        return

    if numeric is not None:
        marked_id = db.normalize_channel_id(numeric)
        display = f"chat {marked_id}"
        await db.run_async(db.add_destination, marked_id, display, True)
        await db.run_async(
            db.record_audit,
            "destination_added",
            None,
            actor_id=ADMIN_USER_ID,
            detail=f"{marked_id} (numeric) -> {display}",
        )
        await event.reply(
            f"✅ **Destination added**: `{display}` (ID `{marked_id}`)\n"
            f"Every bot post published from now on will be forwarded here.",
            buttons=_home_keyboard(),
        )
        return

    _wizard_state[ADMIN_USER_ID] = {"step": "adddest_confirm_unresolved", "raw": text}
    await event.reply(
        f"I couldn't verify `{text}` as a chat I can send to. It may be a private "
        f"group I'm not a member of.\n\n"
        f"• **Forward a message FROM the group** and I'll grab its exact ID, or\n"
        f"• Add it anyway and I'll retry delivery on every post (failures show "
        f"up in the queue).",
        buttons=[
            [Button.inline("✅ Add anyway (retry later)", data="destaddunresolved")],
            [Button.inline("❌ Cancel", data="wiz:cancel")],
        ],
    )


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
        ],
        [
            Button.inline("📋 Sources", data="menu:sources"),
            Button.inline(DESTINATIONS_BTN, data="menu:destinations"),
            Button.inline(pause_label, data="home:toggle"),
        ],
        [
            Button.inline("🚫 Skipped", data="home:skipped"),
            Button.inline("✅ Published", data="home:published"),
        ],
        [
            Button.inline(_asleep_label(), data="home:asleep"),
            Button.inline("❓ Help", data="home:help"),
        ],
    ]


def _help_text() -> str:
    return (
        "🤖 **Telegram Monitor Admin Bot**\n\n"
        "Everything is one tap — no commands to remember:\n"
        "• **📊 Status** — today's report\n"
        "• **⏳ Pending** — approve / preview / edit new listings\n"
        "• **📋 Sources** — add, manage, remove sources\n"
        f"• **{DESTINATIONS_BTN}** — add groups to forward copies of every bot post to\n"
        "• **✅ Published** — every post with its **#Post number** + channel & source links\n"
        "• ** /post 12** — jump straight to post #12\n"
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
    supplier_text = "\n\n".join(supplier_lines)

    reason_lines = [f"• {r}: `{c}`" for r, c in stats["skip_reasons"].items()]
    if not reason_lines:
        reason_lines = ["• None"]
    reason_text = "\n\n".join(reason_lines)

    msg = (
        "📊 **Today's Activity Report**\n\n"
        f"• Active Suppliers: `{stats['active_suppliers']}`\n"
        f"• Total Processed: `{stats['total_processed']}`\n"
        f"• Published: `{stats['published']}`\n"
        f"• Pending Approval: `{stats['pending']}`\n"
        f"• Total Skipped: `{stats['total_skipped']}`\n\n"
        f"**By Supplier**\n{supplier_text}\n\n"
        f"**Skip Breakdown**\n{reason_text}"
    )
    if db.is_paused():
        msg = "⏸ **PAUSED — automatic publishing is stopped**\n(manual Approve taps still publish)\n\n" + msg
    return msg


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


def _skipped_digest(
    skips: List[dict], page: int = 0, total: Optional[int] = None
) -> Tuple[str, List[List[object]]]:
    """Buttons-only recent-skips list.

    The body is intentionally short: each Re-review button label carries
    reason -- supplier -- age ('duplicate -- @kycgroupke -- 2h ago'). Skips
    with no linked listing can't be reopened, so those drop to a one-line
    count instead of a dead text entry."""
    reopenable = [k for k in skips if k.get("listing_id")]
    buttons = []
    for k in reopenable:
        reason = (k.get("reason") or "unknown").replace("_", " ")
        src = _pretty_source(k.get("channel_username"), k.get("display_name")) or "?"
        age = _relative_time(k.get("timestamp"))
        label = f"{reason} -- {src}"
        if age:
            label += f" -- {age}"
        buttons.append([Button.inline(label, data=f"reskip:{k['skip_id']}")])
    text = "🚫 **Skipped posts** — tap a button to open a post:\n"
    if reopenable:
        dropped = len(skips) - len(reopenable)
        if dropped:
            text += f"\n_({dropped} more recent skip(s) not re-reviewable — no listing linked.)_"
    else:
        text += "\n_None of the recent skips can be re-opened._"
    if total is not None:
        total = max(int(total), len(skips))
        _, _, page_count, _, _ = _page_window(total, page)
        footer = _page_footer(page, page_count)
        if footer:
            text += f"\n\n{footer}"
        buttons.extend(_nav_row("skip", page, page_count))
    buttons.extend(_home_button_row())
    return text, buttons


def _published_digest(
    rows: List[dict], page: int = 0, total: Optional[int] = None
) -> Tuple[str, List[List[object]]]:
    """Buttons-only published list: per post ONE row of two no-emoji URL buttons.

    Button A links the post's #number to the published post in our channel;
    Button B links the source group/channel name to the original source post.
    No text lines: the button labels carry all the meaning."""
    text = " ✅ **Published posts** — tap a button to open a post:\n"
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
    if total is not None:
        total = max(int(total), len(rows))
        _, _, page_count, _, _ = _page_window(total, page)
        footer = _page_footer(page, page_count)
        if footer:
            text += f"\n\n{footer}"
        buttons.extend(_nav_row("pub", page, page_count))
    buttons.append([Button.inline("Search Post", data="published:search")])
    buttons.extend(_home_button_row())
    return text, buttons


def _review_position_label(remaining: int) -> str:
    """Queue position for the next card: "2/4" when the inbox session's total is
    known and the math still holds, else "N remaining" (e.g. a new listing
    arrived mid-session, or no /pending session is active)."""
    total = _review_session_total
    if total is None or total < 1:
        return f"{remaining} remaining"
    current = _review_session_done + 1
    if current > total:
        return f"{remaining} remaining"
    return f"{current}/{total}"


async def _advance_review(bot: TelegramClient) -> None:
    """Inbox flow: after a review decision, immediately surface the next pending
    listing's review card (or a queue-finished notice when the queue is empty).
    The next card is fetched from the database — never assumed to be the next
    sequential id."""
    global _review_session_done, _review_session_total
    next_listing = db.get_pending_listings(limit=1)
    if next_listing:
        _review_session_done += 1
        remaining = db.count_pending_listings()
        await send_approval_prompt(
            bot,
            ADMIN_USER_ID,
            next_listing[0],
            as_next=True,
            queue_pos=_review_position_label(remaining),
        )
    else:
        _review_session_total = 0
        _review_session_done = 0
        try:
            await bot.send_message(
                ADMIN_USER_ID,
                "✅ No pending listings right now.",
            )
        except Exception:
            logger.exception("Failed to send review-queue-finished notice")


async def _send_pending_page(event, bot, page: int = 0) -> None:
    """Inbox entry: delete the tapped message and surface ONE pending listing
    card whose header carries its position in the queue ('1/N').

    After the admin acts on it (Approve / Skip / Edit → Save) ``_advance_review``
    picks up and shows the next card automatically — there is no multi-card dump
    and no separate count banner anymore.

    The ``page`` argument is accepted for API compatibility with ``pend:page:N``
    callbacks but is ignored; the inbox always starts from the oldest item.
    """
    global _review_session_total, _review_session_done
    total = db.count_pending_listings()
    pending = db.get_pending_listings(limit=1, offset=0)
    if not pending:
        await event.reply("✅ No listings pending approval right now.")
        return
    _review_session_total = total
    _review_session_done = 0
    # Remove the tapped menu entry (so it's not lost in the chat) and send the
    # single review card below it — the header carries the queue position.
    try:
        await event.delete()
    except Exception:
        pass
    await send_approval_prompt(
        bot, ADMIN_USER_ID, pending[0], as_next=False, queue_pos=f"1/{total}"
    )


def _post_card(post: dict) -> Tuple[str, List[List[object]]]:
    """Render one published-post lookup card (shared by /post and the Published
    screen's 'Search Post' button)."""
    post_number = post.get("post_number")
    platform = (post.get("platform_name") or post.get("game_name") or "?").title()
    supplier = _pretty_source(post.get("supplier_username"), post.get("supplier_display_name"))
    if not supplier or supplier == "?":
        supplier = f"channel {post.get('supplier_channel_id')}"
    lines = [
        f"**Post #{post_number}**",
        "━━━━━━━━━",
        f"Platform : {platform}",
        f"Supplier : {supplier}",
    ]
    buttons = []
    src_url = _source_url(post)
    if src_url:
        buttons.append([Button.url("View in Buyer channel", src_url)])
    buttons.append([
        Button.inline("⬅️ Back", data="home:published"),
        Button.inline("🏠 Home", data="menu:home"),
    ])
    return "\n".join(lines), buttons


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
        await db.run_async(db.set_paused, not paused)
        state_label = "⏸ **Paused**" if not paused else "▶ **Resumed**"
        await event.reply(
            f"{state_label}. New listings are captured, "
            f"Automatic publishing is {'stopped' if not paused else 'running'}. "
            f"**Approve** to publish now.",
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )

    @bot.on(events.NewMessage(pattern=r"^(?:💤 I'm Asleep|☀️ I'm Awake)$"))
    async def handle_asleep_toggle(event):
        if not await check_admin(event):
            return
        asleep = db.is_buyer_asleep()
        await db.run_async(db.set_buyer_asleep, not asleep)
        if not asleep:
            state_label = (
                "💤 **Asleep** — every new post now carries the footer\n"
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

    @bot.on(events.NewMessage(pattern=r"^/adddestination(?:\s+(.+))?"))
    async def handle_add_destination(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "**Add a destination** — a group where forwarded copies of every "
                "bot post should land. Any of these work:\n"
                "• Group username: `@mygroup`\n"
                "• Numeric ID: `-1001234567890`\n"
                "• **Forward a message FROM the group** — best for private groups "
                "with no username (I grab its exact ID automatically).\n\n"
                "Example: `/adddestination @mygroup`",
                buttons=_home_keyboard(),
            )
            return
        await _run_add_destination_flow(event, arg)

    @bot.on(events.NewMessage(pattern=DESTINATIONS_ROUTE_RE))
    async def handle_destinations(event):
        if not await check_admin(event):
            return
        destinations = db.list_destinations(active_only=False)
        await event.reply(
            _destinations_menu_text(destinations),
            buttons=_destinations_buttons(destinations),
            parse_mode=None,
        )

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
        await db.run_async(db.delete_supplier, s["id"])
        await db.run_async(
            db.record_audit,
            "supplier_deleted",
            None,
            actor_id=event.sender_id,
            detail=f"@{arg.lstrip('@')}",
        )
        await event.reply(f"🗑 Supplier **@{arg.lstrip('@')}** permanently deleted.", buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^/dedupe_suppliers$"))
    async def handle_dedupe_suppliers(event):
        if not await check_admin(event):
            return
        summary = await db.run_async(db.dedupe_suppliers)
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
            await db.run_async(
                db.record_audit,
                "supplier_dedupe",
                None,
                actor_id=event.sender_id,
                detail=str(m),
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
        await _send_pending_page(event, bot, 0)

    @bot.on(events.NewMessage(pattern=r"^(?:/skipped|🚫 Skipped)"))
    async def handle_skipped(event):
        if not await check_admin(event):
            return
        skips = db.get_skipped_listings(limit=_PAGE_SIZE, offset=0)
        if not skips:
            await event.reply("✅ No skipped messages logged.", buttons=_home_keyboard())
            return

        text, buttons = _skipped_digest(skips, 0, db.count_skipped_listings())
        await event.reply(text, buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^(?:/published|✅ Published)"))
    async def handle_published(event):
        if not await check_admin(event):
            return
        rows = db.get_published_listings(limit=_PAGE_SIZE, offset=0)
        if not rows:
            await event.reply("📜 No published posts yet.", buttons=_home_keyboard())
            return

        text, buttons = _published_digest(rows, 0, db.count_published_listings())
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
        text, buttons = _post_card(post)
        await event.reply(text, buttons=buttons, parse_mode="markdown")

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
                buttons=_listing_action_buttons(listing),
            )
        except Exception as exc:
            logger.warning("Preview too long to send for listing #%s: %s", arg, exc)
            await event.reply(
                f"⚠️ Preview for **#{arg}** is too long to display inline. "
                f"Use `/pending` to find it.",
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
            reopened = await db.run_async(db.reopen_skipped, skip_id)
            if not reopened:
                await event.answer("Skip not found or already processed.", alert=True)
                return
            await db.run_async(
                db.record_audit,
                "skipped_reopen",
                reopened["id"],
                actor_id=event.sender_id,
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

        if data_str == "menu:destinations":
            destinations = db.list_destinations(active_only=False)
            text = _destinations_menu_text(destinations)
            await _message_delete_send(
                event, text, buttons=_destinations_buttons(destinations), parse_mode=None
            )
            return

        if data_str.startswith("home:"):
            home_action = data_str.split(":", 1)[1]
            if home_action == "status":
                await _message_delete_send(event, _status_report_text(), buttons=_home_button_row())
                return
            if home_action == "pending":
                await _send_pending_page(event, bot, 0)
                return
            if home_action == "help":
                await _message_delete_send(event, _help_text(), buttons=_home_button_row())
                return
            if home_action == "toggle":
                paused = db.is_paused()
                await db.run_async(db.set_paused, not paused)
                await event.answer(
                    "⏸ Automatic publishing paused" if not paused else "▶ Automatic publishing resumed"
                )
                await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
                return
            if home_action == "asleep":
                asleep = db.is_buyer_asleep()
                await db.run_async(db.set_buyer_asleep, not asleep)
                await event.answer(
                    "💤 Asleep — footer added to new posts" if not asleep
                    else "☀️ Awake — footer removed"
                )
                await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
                return
            if home_action == "skipped":
                skips = db.get_skipped_listings(limit=_PAGE_SIZE, offset=0)
                if not skips:
                    await _message_delete_send(
                        event,
                        "✅ No skipped messages logged.",
                        buttons=_home_button_row(),
                    )
                    return
                text, buttons = _skipped_digest(skips, 0, db.count_skipped_listings())
                await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
                return
            if home_action == "published":
                rows = db.get_published_listings(limit=_PAGE_SIZE, offset=0)
                if not rows:
                    await _message_delete_send(
                        event,
                        "📜 No published posts yet.",
                        buttons=_home_button_row(),
                    )
                    return
                text, buttons = _published_digest(rows, 0, db.count_published_listings())
                await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
                return
            return

        if data_str == "published:search":
            _wizard_state[ADMIN_USER_ID] = {"step": "post_search"}
            try:
                sent = await event.client.send_message(
                    ADMIN_USER_ID,
                    "Search a published post — send the **post number** "
                    "(the `#N` on each published card), e.g. `12`.",
                    buttons=[Button.inline("🚫 Cancel", data="wiz:cancel")],
                    parse_mode="markdown",
                )
                prompt_id = getattr(sent, "id", None)
                if prompt_id:
                    _wizard_state[ADMIN_USER_ID]["prompt_message_id"] = prompt_id
            except Exception:
                logger.exception("Failed to send post search prompt")
            try:
                await event.delete()
            except Exception:
                pass
            return

        if data_str == "wiz:cancel":
            # Dropping the wizard must ALSO drop any half-typed edit draft:
            # otherwise the stale text silently resurrects on a later Approve
            # of the same listing (wizard-draft leak).
            wiz = _wizard_state.pop(ADMIN_USER_ID, None)
            edit_cancelled = bool(wiz and wiz.get("step") == "edit" and wiz.get("listing_id"))
            if edit_cancelled:
                _drafts.pop(wiz["listing_id"], None)
            await event.answer("Cancelled")
            try:
                await event.delete()
            except Exception:
                pass
            if edit_cancelled:
                try:
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        "✏️ Edit cancelled.",
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
            return

        # ---- List pagination ----------------------------------------------
        supage_match = re.match(r"^sup:page:(-?\d+)$", data_str)
        if supage_match:
            raw_page = int(supage_match.group(1))
            suppliers = db.list_suppliers(active_only=False)
            start, _, _, _, _ = _page_window(len(suppliers), raw_page)
            page = start // _PAGE_SIZE
            await _message_delete_send(
                event,
                _sources_menu_text(suppliers, page),
                buttons=_sources_buttons(suppliers, page),
                parse_mode=None,
            )
            return

        destpage_match = re.match(r"^dest:page:(-?\d+)$", data_str)
        if destpage_match:
            raw_page = int(destpage_match.group(1))
            destinations = db.list_destinations(active_only=False)
            start, _, _, _, _ = _page_window(len(destinations), raw_page)
            page = start // _PAGE_SIZE
            await _message_delete_send(
                event,
                _destinations_menu_text(destinations, page),
                buttons=_destinations_buttons(destinations, page),
                parse_mode=None,
            )
            return

        skippage_match = re.match(r"^skip:page:(-?\d+)$", data_str)
        if skippage_match:
            raw_page = int(skippage_match.group(1))
            total = db.count_skipped_listings()
            start, _, _, _, _ = _page_window(total, raw_page)
            page = start // _PAGE_SIZE
            skips = db.get_skipped_listings(limit=_PAGE_SIZE, offset=start)
            text, buttons = _skipped_digest(skips, page, total)
            await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
            return

        pubpage_match = re.match(r"^pub:page:(-?\d+)$", data_str)
        if pubpage_match:
            raw_page = int(pubpage_match.group(1))
            total = db.count_published_listings()
            start, _, _, _, _ = _page_window(total, raw_page)
            page = start // _PAGE_SIZE
            rows = db.get_published_listings(limit=_PAGE_SIZE, offset=start)
            text, buttons = _published_digest(rows, page, total)
            await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
            return

        pendpage_match = re.match(r"^pend:page:(-?\d+)$", data_str)
        if pendpage_match:
            raw_page = int(pendpage_match.group(1))
            await _send_pending_page(event, bot, raw_page)
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
            await db.run_async(db.set_supplier_active, s["channel_username"], not s["active"])
            await db.run_async(
                db.record_audit,
                "supplier_toggle",
                None,
                actor_id=ADMIN_USER_ID,
                detail=str(sid),
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
            await db.run_async(db.delete_supplier, sid)
            await db.run_async(
                db.record_audit,
                "supplier_deleted",
                None,
                actor_id=ADMIN_USER_ID,
                detail=str(sid),
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

        # ---- Destinations submenu ------------------------------------------
        dest_match = re.match(r"^dest:(\d+)$", data_str)
        if dest_match:
            did = int(dest_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer("Destination not found.", alert=True)
                return
            await _edit_destination_menu(event, d)
            return

        desttoggle_match = re.match(r"^desttoggle:(\d+)$", data_str)
        if desttoggle_match:
            did = int(desttoggle_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer("Destination not found.", alert=True)
                return
            await db.run_async(db.set_destination_active, did, not d["active"])
            await db.run_async(
                db.record_audit,
                "destination_toggle",
                None,
                actor_id=ADMIN_USER_ID,
                detail=str(did),
            )
            refreshed = db.get_destination_by_id(did)
            if refreshed:
                await _edit_destination_menu(event, refreshed)
            return

        destdel_match = re.match(r"^destdel:(\d+)$", data_str)
        if destdel_match:
            did = int(destdel_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer("Destination not found.", alert=True)
                return
            await _message_delete_send(
                event,
                f"🗑 **Delete {_destination_label(d)} permanently?**\n"
                f"This removes the destination and stops forwarding posts to it.\n"
                f"Existing forward history stays.\n"
                f"This cannot be undone.",
                buttons=[
                    [Button.inline("✅ Yes, Delete Forever", data=f"destdelyes:{did}")],
                    [Button.inline("❌ Cancel", data="wiz:cancel")],
                ],
            )
            return

        destdelyes_match = re.match(r"^destdelyes:(\d+)$", data_str)
        if destdelyes_match:
            did = int(destdelyes_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer("Destination not found.", alert=True)
                return
            await db.run_async(db.delete_destination, did)
            await db.run_async(
                db.record_audit,
                "destination_deleted",
                None,
                actor_id=ADMIN_USER_ID,
                detail=str(did),
            )
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    f"🗑 **Destination permanently deleted.**\n"
                    f"{_destination_label(d)} is gone. History is kept.",
                    parse_mode="markdown",
                )
            except Exception:
                pass
            destinations = db.list_destinations(active_only=False)
            await event.client.send_message(
                ADMIN_USER_ID,
                _destinations_menu_text(destinations),
                buttons=_destinations_buttons(destinations),
                parse_mode=None,
            )
            return

        if data_str == "destaddunresolved":
            state = _wizard_state.get(ADMIN_USER_ID) or {}
            raw = (state.get("raw") or "").strip()
            _wizard_state.pop(ADMIN_USER_ID, None)
            if not raw:
                await event.answer("Nothing to add.", alert=True)
                return
            try:
                did = await db.run_async(db.add_destination, raw, raw, True)
            except ValueError as exc:
                await _message_delete_send(
                    event,
                    f"⚠️ **Could not add destination**: {exc}\n"
                    f"Use a group username (`@mygroup`) or numeric ID "
                    f"(`-1001234567890`).",
                    buttons=_home_keyboard(),
                )
                return
            await db.run_async(
                db.record_audit,
                "destination_added",
                None,
                actor_id=ADMIN_USER_ID,
                detail=f"{raw} (unverified) -> {raw}",
            )
            await _message_delete_send(
                event,
                f"⚠️ **Destination added (unverified)**: `{raw}`\n"
                f"I couldn't confirm I can send to it yet — delivery will be "
                f"retried on every post until it succeeds, and failures show up "
                f"in the queue. Make sure the bot account is a member.",
                buttons=_home_keyboard(),
            )
            return

        if data_str == "reseed:yes":
            try:
                raw = os.environ.get("SOURCE_CHANNELS", "")
                channels = [ch.strip() for ch in raw.split(",") if ch.strip()]
                await db.run_async(db.clear_env_seed_completed)
                n = await db.run_async(db.seed_suppliers_from_env, channels)
                await db.run_async(db.mark_env_seed_completed)
                await db.run_async(
                    db.record_audit,
                    "env_reseed",
                    None,
                    actor_id=ADMIN_USER_ID,
                    detail=f"re-seeded {n} supplier(s)",
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
            sid = await db.run_async(
                db.add_supplier,
                username,
                channel_id=None,
            )
            await db.run_async(
                db.record_audit,
                "supplier_added_unresolved",
                sid,
                actor_id=ADMIN_USER_ID,
                detail=raw,
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

        if data_str == "destadd":
            _wizard_state[ADMIN_USER_ID] = {"step": "adddest"}
            await _message_delete_send(
                event,
                "✏️ **Add a destination** — a group where forwarded copies of every "
                "bot post should land. Any of these work:\n"
                "• Group username: `@mygroup`\n"
                "• Numeric ID: `-1001234567890`\n"
                "• **Forward a message FROM the group** — best for private groups "
                "with no username (I'll grab its exact ID automatically).\n\n"
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
            # Send the replacement preview FIRST so the original prompt card is
            # never removed without a confirmed replacement in its place.
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    f"📄 **Preview of Listing #{listing_id}**\n"
                    f"Source: {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))} · Status: `{listing['status']}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{preview_text}",
                    buttons=_listing_action_buttons(listing),
                    parse_mode="markdown",
                )
            except Exception:
                logger.exception("Failed to send preview message for listing #%s", listing_id)
                await event.answer("Could not send the preview.", alert=True)
                return
            # Only now is the tapped card removed.
            try:
                await event.delete()
            except Exception:
                pass
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

        match = re.match(r"^(approve|skip):(\d+)$", data_str)
        if not match:
            await event.answer("Unknown action", alert=True)
            return

        action, listing_id_str = match.groups()
        listing_id = int(listing_id_str)
        listing = db.get_listing_by_id(listing_id)

        if not listing:
            await event.answer("Listing not found in database.", alert=True)
            return

        # Approve / Skip only valid on pending listings (never on already-approved,
        # preventing double-publish races on re-taps).
        if not listing_is_editable(listing["status"]):
            await event.answer(
                f"Already processed (status: {listing['status']})", alert=True
            )
            # The listing was already handled by another action, a second tap, or
            # the worker — move the inbox to the next pending listing.
            await _advance_review(bot)
            return

        if action == "skip":
            # Skip: the admin decided it is not worth publishing right now; it
            # stays re-reviewable from /skipped (status skipped_admin is in the
            # reopenable set).
            _drafts.pop(listing_id, None)
            await db.run_async(db.update_listing_status, listing_id, "skipped_admin")
            await db.run_async(
                db.record_audit, "skipped_admin", listing_id, actor_id=event.sender_id
            )
            await db.run_async(
                db.log_skip,
                listing.get("supplier_id"),
                listing.get("source_message_id"),
                "admin_skip",
                listing.get("raw_text") or listing.get("clean_text") or "",
            )
            await event.answer("⏭️ Skipped")
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    f"Listing #{listing_id} skipped for now.",
                    parse_mode="markdown",
                )
            except Exception:
                pass
            await _advance_review(bot)
            return

        if action == "approve":
            # Re-check the listing's CURRENT status immediately before applying any
            # draft. The listing loaded above is a snapshot taken when the callback
            # arrived; if it has since moved out of the editable set (published,
            # skipped, requeued) the draft must NOT be silently applied to
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
                await _advance_review(bot)
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

            post_number = await db.run_async(db.next_post_number)
            republished_text, entities = parser.build_ai_message(
                content_lines=content_lines,
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
                #
                # F3: atomic claim BEFORE the external send. Only the single
                # winner may publish; a second Approve tap, a second worker, or a
                # crash-retry sees status 'publishing' and must not send again.
                claimed = await db.run_async(
                    db.claim_listing_for_publish, listing_id, post_number
                )
                if not claimed:
                    try:
                        await event.client.send_message(
                            ADMIN_USER_ID,
                            f"⚠️ Listing #{listing_id} already being published elsewhere — nothing duplicate sent.",
                            parse_mode="markdown",
                        )
                    except Exception:
                        pass
                    await _advance_review(bot)
                    return
                await publish_guard.throttle()
                published_msg_id = None
                dest_peer = db.to_peer_reference(DEST_CHANNEL)
                try:
                    sent_msg = await publish_guard.run_with_floodwait_retry(
                        lambda text=republished_text, ent=entities: user_client_ref.send_message(
                            dest_peer, text,
                            formatting_entities=ent
                        ),
                        f"approve send listing #{listing_id}",
                        max_total_sleep=APPROVE_FLOODWAIT_BUDGET_SECONDS,
                    )
                    published_msg_id = sent_msg.id
                except Exception as e:
                    logger.exception("Send failed for listing #%s via user_client: %s", listing_id, e)
                    # Ambiguous failure: the message may actually have landed even
                    # though the response was lost. Confirm against the channel
                    # before re-queueing — otherwise the worker republishes it.
                    confirmed_id = await publish_guard.confirm_message_on_destination(
                        user_client_ref, dest_peer, republished_text
                    )
                    if confirmed_id is not None:
                        logger.warning(
                            "Approve send for listing #%s reported %r but the message "
                            "is on the destination (msg id %s) — recording as published, "
                            "NOT re-queueing.",
                            listing_id,
                            e,
                            confirmed_id,
                        )
                        published_msg_id = confirmed_id
                    else:
                        # Provably not on the destination — release the claim and
                        # re-queue for the worker.
                        await db.run_async(db.release_publish_claim, listing_id, "approved")
                        await db.run_async(
                            db.record_audit,
                            "approved_queued",
                            listing_id,
                            actor_id=event.sender_id,
                            detail=str(e)[:200],
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
                    await db.run_async(
                        db.update_listing_status,
                        listing_id=listing_id,
                        status="published",
                        published_message_id=published_msg_id,
                        post_number=post_number,
                    )
                    await db.run_async(
                        db.record_audit,
                        "published_admin",
                        listing_id,
                        actor_id=event.sender_id,
                        detail=published_msg_id,
                    )
                    await db.run_async(
                        db.queue_forwarding, listing_id, DEST_CHANNEL, published_msg_id
                    )
                except Exception as exc:
                    logger.exception(
                        "Listing #%s WAS published (msg id %s) but bookkeeping failed: %s",
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
                        await db.run_async(
                            db.queue_forwarding, listing_id, DEST_CHANNEL, published_msg_id
                        )
                    except Exception:
                        logger.exception("Could not record published state for listing #%s", listing_id)

                try:
                    confirmation = f"✅ Listing #{listing_id} Published! · Post #{post_number}"
                    approved_view = dict(listing)
                    approved_view["published_message_id"] = published_msg_id
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        confirmation,
                        buttons=_channel_view_buttons(approved_view),
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
                await _advance_review(bot)
                return
            else:
                await db.run_async(db.update_listing_status, listing_id, "approved")
                await db.run_async(
                    db.record_audit,
                    "approved",
                    listing_id,
                    actor_id=event.sender_id,
                    detail="queued for worker publish",
                )
                try:
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        f"✅ Listing #{listing_id} queued for republish.",
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
                await _advance_review(bot)

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

        if state and state.get("step") == "adddest" and fwd is not None:
            _wizard_state.pop(ADMIN_USER_ID, None)
            await _run_add_destination_flow(event, None, fwd=fwd)
            return

        text = event.text
        if not text:
            return
        text = text.strip()
        if text.startswith("/"):
            return
        # Menu taps must not be swallowed while a wizard is waiting
        if text in ("📊 Status", "⏳ Pending", "📋 Sources", DESTINATIONS_BTN,
                    "⏸ All Stop", "▶ All Start", "❓ Help", "✅ Published",
                    "💤 I'm Asleep", "☀️ I'm Awake"):
            _wizard_state.pop(ADMIN_USER_ID, None)
            return

        if not state:
            return
        prompt_message_id = state.get("prompt_message_id")
        _wizard_state.pop(ADMIN_USER_ID, None)

        if state["step"] == "add":
            await _run_add_supplier_flow(event, text)
            return

        if state["step"] == "adddest":
            await _run_add_destination_flow(event, text)
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
                buttons=_listing_action_buttons(listing),
                parse_mode="markdown",
            )
            return

        if state["step"] == "post_search":
            if not text.isdigit():
                await event.reply(
                    f"`{text}` isn't a post number — send a number like `12` "
                    f"(or `/post 12`).",
                    buttons=_home_keyboard(),
                    parse_mode="markdown",
                )
                return
            post = db.get_post_by_number(int(text))
            if not post:
                await event.reply(
                    f"❌ No published post with number `#{text}`.",
                    buttons=_home_keyboard(),
                    parse_mode="markdown",
                )
                return
            if prompt_message_id:
                try:
                    await bot.delete_messages(ADMIN_USER_ID, [prompt_message_id])
                except Exception:
                    pass
            card, buttons = _post_card(post)
            await event.reply(card, buttons=buttons, parse_mode="markdown")
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
                BotCommand(command="status", description=" Today's stats report"),
                BotCommand(command="pending", description="Pending approval listings"),
                BotCommand(command="skipped", description="Recently skipped messages"),
                BotCommand(command="sources", description=" Manage monitored sources"),
                BotCommand(command="published", description="Published posts & channel links"),
                BotCommand(command="post", description=" Look up a post by its number: /post 12"),
                BotCommand(command="help", description=" Show buttons and shortcuts"),
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