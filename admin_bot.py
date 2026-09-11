"""Admin Bot for managing suppliers, checking status, and approving ambiguous listings."""

import asyncio
import logging
import os
import re
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events

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
    orig_p = listing.get("original_price")
    our_p = listing.get("our_price")
    review_reason = listing.get("_review_reason", "")
    ai_preview = (listing.get("clean_text") or "")[:400]
    raw_preview = (listing.get("raw_text") or "")[:400]

    reason_label = {
        "no_price_dm_signal": "DM/Contact Signal (No Price)",
        "unknown_platform": "Platform Not Identified",
        "ai_unavailable": "AI Unavailable — Manual Review",
        "ai_blocked_review": "⚠️ AI Flagged Content — Verify",
        "price_out_of_bounds": "⚠️ Suspicious Price — Verify",
    }.get(review_reason, "Manual Review")
    header = f"📬 {reason_label} — Listing #{listing_id}"
    if orig_p is not None and our_p is not None:
        price_info = f"Original: ${parser._format_price(orig_p)}  →  Ours: ${parser._format_price(our_p)}"
    elif orig_p is not None:
        price_info = f"Price: ${parser._format_price(orig_p)}"
    else:
        price_info = "Price: not listed"

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
    price = listing.get("our_price")
    price_s = ("$" + str(price).rstrip("0").rstrip(".")) if price else "—"
    post_disp = f"#{post_number}" if post_number is not None else f"Listing #{listing_id}"

    text = (
        f"✅ **Auto-Published — Post {post_disp}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{platform.title() if platform else 'Platform ?'}\n"
        f"Price    : {price_s}\n"
        f"Supplier : {_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name'))}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + (f"Find it later with `/post {post_number}`." if post_number is not None else "")
    )

    buttons = []
    src_url = _source_url(listing)
    if src_url:
        buttons.append([Button.url("📥 View in source channel", src_url)])

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode="markdown")
        logger.info("Sent published alert for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send published alert to admin for listing #%s", listing_id)


def _format_listing(n: int, l: dict) -> str:
    """Single-line listing summary for /pending, /failed, /preview lists."""
    platform = l.get("platform_name") or l.get("game_name") or "?"
    orig_p = l.get("original_price")
    our_p = l.get("our_price")
    price = f"${our_p}" if our_p is not None else ("$—" if orig_p is None else f"orig ${orig_p}")
    created = (l.get("created_at") or "")[:16].replace("T", " ")
    return (
        f"{n}. **#{l['id']}** | {platform} | {price} | "
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
    our_price = listing.get("our_price")
    platform = listing.get("platform_name") or listing.get("game_name")
    intent = listing.get("intent") or "neutral"

    if our_price and our_price > 0:
        preview_text, _ = parser.build_ai_message(
            content_lines=content_lines,
            our_price=our_price,
            platform=platform,
            contact_username=CONTACT_USERNAME,
            intent=intent,
            header_word=listing.get("header_word"),
            listing_seed=listing["id"],
            post_number=listing.get("post_number"),
        )
    else:
        preview_text, _ = parser.build_ai_message(
            content_lines=content_lines,
            our_price=None,
            platform=platform,
            contact_username=CONTACT_USERNAME,
            intent=intent,
            header_word=listing.get("header_word"),
            listing_seed=listing["id"],
            post_number=listing.get("post_number"),
        )
    return preview_text


def _listing_with_draft(listing: dict, draft: str) -> dict:
    """Copy of a listing whose content/price are taken from an admin draft."""
    updated = dict(listing)
    updated["clean_text"] = draft
    draft_price, _ = parser.extract_price(draft)
    if draft_price and draft_price > 0:
        updated["our_price"] = int(round(draft_price))
    return updated


# Home reply keyboard — persistent 1-tap UI. Label reflects current pause state.
def _home_keyboard() -> List[List[object]]:
    pause_label = "▶ All Start" if db.is_paused() else "⏸ All Stop"
    return [
        [Button.text("📊 Status", resize=True), Button.text("⏳ Pending", resize=True), Button.text("⚠️ Failed", resize=True)],
        [Button.text("📋 Sources", resize=True), Button.text(pause_label, resize=True), Button.text("❓ Help", resize=True)],
        [Button.text("📜 Published", resize=True)],
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


# Wizard state: sender_id -> {"step": "add" | "rule" | "edit", ...}
_wizard_state: Dict[int, dict] = {}
# Listing drafts: listing_id -> content lines typed by the admin via ✏️ Edit.
_drafts: Dict[int, str] = {}

# Matches pasted emoji-pack links like https://t.me/addemoji/SkullEmoji
EMOJI_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/addemoji/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)

EMOJI_ROLE_LABELS = {
    "fire": "🔥 Header (WTB / WANTED)",
    "lightning": "⚡ Header (WTB / WANTED)",
    "moneybag": "🤑 Price line",
    "phone": "📞 Contact line",
}


def _normalize_pack_shortname(text: str) -> Optional[str]:
    """Turn a pack link or bare shortname into a shortname (e.g. 'SkullEmoji')."""
    text = text.strip()
    m = re.match(
        r"(?:(?:https?://)?(?:t\.me|telegram\.me)/addemoji/)?([A-Za-z0-9_]+)$",
        text,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def _emoji_mapping_text() -> str:
    lines = ["Current mapping (defaults in use unless noted):"]
    cfg = db.get_emoji_configs()
    for role, (_, ph, _expected) in parser.EMOJI_ROLES.items():
        label = EMOJI_ROLE_LABELS.get(role, role)
        cur = cfg.get(role)
        if cur:
            lines.append(f"  • {label}: custom — from `{cur['source']}`")
        else:
            lines.append(f"  • {label}: default")
    return "\n".join(lines)


async def _apply_emoji_pack(client, short_name: str, source_label: str) -> str:
    """Fetch an emoji pack, map its emoji onto the message roles, persist, and reload."""
    from telethon.tl.functions.messages import GetStickerSetRequest
    from telethon.tl.types import InputStickerSetShortName

    try:
        result = await client(GetStickerSetRequest(
            stickerset=InputStickerSetShortName(short_name=short_name),
            hash=0,
        ))
    except Exception as exc:
        return f"❌ Could not fetch pack **{short_name}**: {exc}"

    pack_map: Dict[str, int] = {}
    for doc in result.documents:
        for attr in doc.attributes:
            if hasattr(attr, "alt") and attr.alt:
                pack_map[attr.alt] = doc.id

    updated: List[Tuple[str, str, int]] = []
    skipped: List[str] = []
    for role, (_default_id, _ph, expected) in parser.EMOJI_ROLES.items():
        doc_id = pack_map.get(expected)
        if doc_id:
            db.set_emoji_config(role, expected, doc_id, source=short_name)
            updated.append((role, expected, doc_id))
        else:
            skipped.append(role)

    parser.reload_emoji_config()

    lines = [f"✅ Pack **{short_name}** fetched ({len(result.documents)} emoji)."]
    if updated:
        for role, ch, doc_id in updated:
            label = EMOJI_ROLE_LABELS.get(role, role)
            lines.append(f"  • {label} → now uses `{ch}` (id {doc_id})")
    if skipped:
        lines.append("  • Not present in this pack: " + ", ".join(skipped))
        lines.append("    (keeping current/default emoji for those)")
    if not updated:
        lines.append(
            "Tip: send a pack that contains 🔥 ⚡ 🤑 📞 (or add more packs — "
            "every matching role is upgraded automatically)."
        )
    return "\n".join(lines)


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


def _sources_menu_text(suppliers: List[dict]) -> str:
    if not suppliers:
        return "📋 **Monitored Sources:**\n\nNo sources configured yet."
    lines = ["📋 **Monitored Sources:**\n"]
    for s in suppliers:
        status_icon = "🟢" if s["active"] else "🔴"
        lines.append(
            f"{status_icon} **{_pretty_source(s.get('channel_username'), s.get('display_name'))}** "
            f"(ID: `{s['channel_id'] or 'unresolved'}`) "
            f"· markup `{s['markup_multiplier']}`"
        )
    return "\n".join(lines)


def _sources_buttons(suppliers: List[dict]) -> List[List[object]]:
    buttons = []
    for s in suppliers[:12]:
        icon = "🟢" if s["active"] else "🔴"
        label = f"{icon} {_pretty_source(s.get('channel_username'), s.get('display_name'))}"
        buttons.append([Button.inline(label, data=f"sup:{s['id']}")])
    buttons.append([
        Button.inline("➕ Add Source", data="supadd"),
        Button.inline("⬅️ Back", data="menu:sources"),
    ])
    return buttons


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
        msg = (
            "🤖 **Telegram Monitor Admin Bot**\n\n"
            "Everything is one tap — no commands to remember:\n"
            "• **📊 Status** — today's report\n"
            "• **⏳ Pending** — approve / preview / edit new listings\n"
            "• **⚠️ Failed** — retry failed publishes\n"
            "• **📋 Sources** — add, manage, remove sources\n"
            "• **📜 Published** — every post with its **#Post number** + channel & source links\n"
            "• **🔢 /post 12** — jump straight to post #12\n"
            "• **⏸ All Stop / ▶ All Start** — pause or resume publishing\n\n"
            "Slash shortcuts still work if you prefer typing them."
        )
        await event.reply(msg, buttons=_home_keyboard())

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
            f"{state_label}. New listings are still captured and shown here for review, "
            f"but nothing will be published.",
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )

    @bot.on(events.NewMessage(pattern=r"^/addemoji(?:\s+(.+))?$"))
    async def handle_add_emoji(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "🎨 **Emoji packs**\n\n"
                "Send any emoji-pack link, e.g. `https://t.me/addemoji/SkullEmoji`, "
                "or just paste the link directly — no command needed.\n"
                "I'll fetch the pack and upgrade the matching roles "
                "(🔥 ⚡ 🤑 📞 ⭐) automatically. Send as many packs as you like.\n\n"
                + _emoji_mapping_text(),
                buttons=_home_keyboard(),
                parse_mode="markdown",
            )
            return
        short_name = _normalize_pack_shortname(arg)
        if not short_name:
            await event.reply(
                "Couldn't read a pack name from that. Send a link like `t.me/addemoji/Name`.",
                buttons=_home_keyboard(),
            )
            return
        client = user_client_ref if user_client_ref and user_client_ref.is_connected() else bot
        await event.reply("⏳ Fetching pack...", buttons=_home_keyboard())
        summary = await _apply_emoji_pack(client, short_name, "command")
        db.record_audit("addemoji", None, actor_id=event.sender_id, detail=short_name)
        await event.reply(summary, buttons=_home_keyboard(), parse_mode="markdown")

    @bot.on(events.NewMessage())
    async def handle_pack_link(event):
        if event.sender_id != ADMIN_USER_ID or not event.text:
            return
        if event.text.strip().startswith("/"):
            return
        m = EMOJI_LINK_RE.search(event.text)
        if not m:
            return
        short_name = m.group(1)
        client = user_client_ref if user_client_ref and user_client_ref.is_connected() else bot
        await event.reply("⏳ Fetching pack...", buttons=_home_keyboard())
        summary = await _apply_emoji_pack(client, short_name, "link")
        db.record_audit("addemoji", None, actor_id=event.sender_id, detail=short_name)
        await event.reply(summary, buttons=_home_keyboard(), parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^(?:/emojistatus|🎨 Emoji)"))
    async def handle_emoji_status(event):
        if not await check_admin(event):
            return
        await event.reply(_emoji_mapping_text(), buttons=_home_keyboard(), parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/addsupplier(?:\s+(.+))?"))
    async def handle_add_supplier(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "Usage: `/addsupplier <channel_or_group>`\nExample: `/addsupplier @kycgroupke` or `/addsupplier -1001234567890`",
                buttons=_home_keyboard(),
            )
            return

        channel_id = None
        if arg.lstrip("-").isdigit():
            channel_id = int(arg)
            username = str(channel_id)
        else:
            username = arg.lstrip("@")
            # Try to resolve channel_id via user client if connected
            if user_client_ref and user_client_ref.is_connected():
                try:
                    entity = await user_client_ref.get_entity(arg)
                    channel_id = getattr(entity, "id", None)
                except Exception as e:
                    logger.warning("Could not immediately resolve entity %s: %s", arg, e)

        db.add_supplier(username, channel_id=channel_id, markup_multiplier=DEFAULT_MULTIPLIER)
        await event.reply(
            f"✅ Supplier **{arg}** added and activated!\n"
            f"Default multiplier: `{DEFAULT_MULTIPLIER}`",
            buttons=_home_keyboard(),
        )

    @bot.on(events.NewMessage(pattern=r"^/removesupplier(?:\s+(.+))?"))
    async def handle_remove_supplier(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                "Usage: `/removesupplier <channel_username>`\nExample: `/removesupplier @kycgroupke`",
                buttons=_home_keyboard(),
            )
            return

        username = arg.lstrip("@")
        ok = db.remove_supplier(username)
        if ok:
            await event.reply(f"⏹ Supplier **@{username}** deactivated.", buttons=_home_keyboard())
        else:
            await event.reply(f"❌ Supplier **@{username}** not found.", buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^/rule(?:\s+(.+))?"))
    async def handle_rule(event):
        if not await check_admin(event):
            return
        raw = (event.pattern_match.group(1) or "").strip()
        parts = raw.split()
        if len(parts) != 2:
            await event.reply(
                "Usage: `/rule <channel_username> <multiplier>`\nExample: `/rule @kycgroupke 0.70`",
                buttons=_home_keyboard(),
            )
            return

        channel_user, mult_str = parts[0].lstrip("@"), parts[1]
        try:
            mult = float(mult_str)
            if mult <= 0 or mult > 5.0:
                await event.reply(
                    "Multiplier should be a realistic positive number (e.g. 0.75).",
                    buttons=_home_keyboard(),
                )
                return
        except ValueError:
            await event.reply(
                "Invalid multiplier format. Please provide a decimal number (e.g. 0.75).",
                buttons=_home_keyboard(),
            )
            return

        ok = db.set_supplier_rule(channel_user, mult)
        if ok:
            await event.reply(
                f"✅ Markup multiplier for **@{channel_user}** set to `{mult}`.",
                buttons=_home_keyboard(),
            )
        else:
            await event.reply(
                f"❌ Supplier **@{channel_user}** not found.",
                buttons=_home_keyboard(),
            )

    @bot.on(events.NewMessage(pattern=r"^(?:/status|📊 Status)"))
    async def handle_status(event):
        if not await check_admin(event):
            return
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
            msg = "⏸ **PAUSED — publishing is stopped**\n\n" + msg
        await event.reply(msg, buttons=_home_keyboard())

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
        buttons.append([Button.inline("🏠 Home", data="menu:home")])
        await event.reply("\n".join(lines), buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^(?:/published|📜 Published)"))
    async def handle_published(event):
        if not await check_admin(event):
            return
        rows = db.get_published_listings(limit=10)
        if not rows:
            await event.reply("📜 No published posts yet.", buttons=_home_keyboard())
            return

        lines = ["📜 **Published posts** — each has a **#Post number** to reference it:\n"]
        buttons = []
        for i, p in enumerate(rows):
            platform = (p.get("platform_name") or p.get("game_name") or "?").title()
            price = p.get("our_price")
            price_s = "$" + str(price).rstrip("0").rstrip(".") if price else ""
            supplier = p.get("supplier_username")
            supplier_chat = p.get("supplier_channel_id")
            src_disp = _pretty_source(supplier, p.get("supplier_display_name"))
            if not src_disp or src_disp == "?":
                src_disp = f"channel {supplier_chat}"
            created = (p.get("published_at") or p.get("created_at") or "")[:10]
            post_num = p.get("post_number")
            num_disp = f"#{post_num}" if post_num is not None else "—"
            label = platform if platform != "?" else "Listing"
            if price_s:
                label = f"{platform} · {price_s}"
            src_url = _source_url(p)
            entry = f"**{num_disp}** · {label} — {src_disp} · {created}"
            if src_url:
                lines.append(f"{i + 1}. {entry}")
                buttons.append([Button.url(f"🔢 {num_disp} 📥 {label}", src_url)])
            else:
                lines.append(f"{i + 1}. {entry}")
        buttons.append([Button.inline("🏠 Home", data="menu:home")])
        await event.reply("\n".join(lines), buttons=buttons, parse_mode="markdown")

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
        price = post.get("our_price")
        price_s = ("$" + str(price).rstrip("0").rstrip(".")) if price else "—"
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
            f"Price    : {price_s}",
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

        # ---- Pure navigation -----------------------------------------------
        if data_str == "menu:home":
            await event.answer("🏠 Home")
            await event.client.send_message(
                ADMIN_USER_ID,
                "🏠 **Home** — tap a button below.",
                buttons=_home_keyboard(),
            )
            return

        if data_str == "menu:sources":
            suppliers = db.list_suppliers(active_only=False)
            text = _sources_menu_text(suppliers)
            if not suppliers:
                text += "\nTap **➕ Add Source** to configure your first one."
            try:
                await event.edit(text, buttons=_sources_buttons(suppliers), parse_mode=None)
            except Exception:
                await event.answer("Sources refreshed", alert=True)
            return

        if data_str == "wiz:cancel":
            _wizard_state.pop(ADMIN_USER_ID, None)
            try:
                await event.edit("❌ Cancelled.", buttons=None)
            except Exception:
                await event.answer("Cancelled", alert=True)
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
            icon = "🟢" if s["active"] else "🔴"
            handle = _pretty_source(s.get("channel_username"), s.get("display_name"))
            toggle_label = "⏸ Pause" if s["active"] else "▶ Resume"
            await event.edit(
                f"{icon} **{handle}**\n"
                f"Status: `{'Active' if s['active'] else 'Paused'}`\n"
                f"Markup: `{s['markup_multiplier']}`\n"
                f"ID: `{s['channel_id'] or 'unresolved'}`\n\n"
                f"What would you like to do?",
                buttons=[
                    [Button.inline(toggle_label, data=f"suptoggle:{sid}")],
                    [Button.inline("⚙️ Set Multiplier", data=f"suprule:{sid}")],
                    [Button.inline("🗑 Remove Source", data=f"supremove:{sid}")],
                    [Button.inline("⬅️ Back", data="menu:home")],
                ],
            )
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
            # Re-render the same management menu (refresh status icon/label)
            refreshed = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if refreshed:
                icon = "🟢" if refreshed["active"] else "🔴"
                handle = _pretty_source(refreshed.get("channel_username"), refreshed.get("display_name"))
                toggle_label = "⏸ Pause" if refreshed["active"] else "▶ Resume"
                await event.edit(
                    f"{icon} **{handle}**\n"
                    f"Status: `{'Active' if refreshed['active'] else 'Paused'}`\n"
                    f"Markup: `{refreshed['markup_multiplier']}`\n"
                    f"ID: `{refreshed['channel_id'] or 'unresolved'}`\n\n"
                    f"What would you like to do?",
                    buttons=[
                        [Button.inline(toggle_label, data=f"suptoggle:{sid}")],
                        [Button.inline("⚙️ Set Multiplier", data=f"suprule:{sid}")],
                        [Button.inline("🗑 Remove Source", data=f"supremove:{sid}")],
                        [Button.inline("⬅️ Back", data="menu:home")],
                    ],
                )
            return

        suprule_match = re.match(r"^suprule:(\d+)$", data_str)
        if suprule_match:
            sid = int(suprule_match.group(1))
            s = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            _wizard_state[ADMIN_USER_ID] = {"step": "rule", "supplier_id": sid}
            handle = _pretty_source(s.get("channel_username"), s.get("display_name"))
            await event.edit(
                f"✏️ **Set multiplier for {handle}**\n"
                f"Current: `{s['markup_multiplier']}`\n\n"
                f"Send the new multiplier as a single reply, e.g. `0.80`.",
                buttons=[Button.inline("🚫 Cancel", data="wiz:cancel")],
            )
            return

        supremove_match = re.match(r"^supremove:(\d+)$", data_str)
        if supremove_match:
            sid = int(supremove_match.group(1))
            s = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            handle = _pretty_source(s.get("channel_username"), s.get("display_name"))
            await event.edit(
                f"🗑 **Remove {handle}?**\nThis deactivates the source and stops monitoring it.",
                buttons=[
                    [Button.inline("✅ Yes, Remove", data=f"rem:yes:{sid}")],
                    [Button.inline("❌ Cancel", data="wiz:cancel")],
                ],
            )
            return

        rem_match = re.match(r"^rem:yes:(\d+)$", data_str)
        if rem_match:
            sid = int(rem_match.group(1))
            s = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if not s:
                await event.answer("Source not found.", alert=True)
                return
            db.set_supplier_active(s["channel_username"], False)
            db.record_audit(
                "supplier_removed", None, actor_id=ADMIN_USER_ID, detail=str(sid)
            )
            await event.edit(
                f"🗑 Source **{_pretty_source(s.get('channel_username'), s.get('display_name'))}** removed.",
                buttons=None,
            )
            suppliers = db.list_suppliers(active_only=False)
            await event.client.send_message(
                ADMIN_USER_ID,
                _sources_menu_text(suppliers),
                buttons=_sources_buttons(suppliers),
                parse_mode=None,
            )
            return

        if data_str == "supadd":
            _wizard_state[ADMIN_USER_ID] = {"step": "add"}
            await event.edit(
                "✏️ **Add a source** — send the channel username or numeric ID.\n"
                "Example: `@kycgroupke` or `-1001234567890`",
                buttons=[Button.inline("🚫 Cancel", data="wiz:cancel")],
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
            # Send a fresh message instead of editing the prompt in place:
            # Telethon edits of the prompt text are rejected by Telegram for long
            # posts ("invalid entity bounds"), which made the button look dead.
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
            if listing["status"] not in ("pending_approval", "pending_review"):
                await event.answer(f"Cannot edit — status is {listing['status']}", alert=True)
                return
            listing_id = listing["id"]
            _wizard_state[ADMIN_USER_ID] = {"step": "edit", "listing_id": listing_id}
            _drafts.pop(listing_id, None)
            price_now = listing.get("our_price")
            price_info = f"${price_now}" if price_now else "not set yet"
            await event.edit(
                f"✏️ **Edit Listing #{listing_id}**\n"
                f"Current price for this post: `{price_info}`\n\n"
                f"Send the corrected content lines as a plain message.\n"
                f"• Price & contact are added automatically.\n"
                f"• If your text includes a price line (e.g. `PRICE 500$`), "
                f"that price is used instead.\n\n"
                f"Then I'll show you the new preview before publishing.",
                buttons=[Button.inline("🚫 Cancel", data="wiz:cancel")],
            )
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
                    await event.edit(f"🔁 Listing #{listing_id} re-queued for publishing.")
                except Exception:
                    await event.answer(f"Re-queued #{listing_id}", alert=True)
            else:
                await event.answer("Re-queue failed.", alert=True)
            return

        # Approve / Reject only valid on pending listings (never on already-approved,
        # preventing double-publish races on re-taps).
        allowed_statuses = ("pending_approval", "pending_review")
        if listing["status"] not in allowed_statuses:
            await event.answer(
                f"Already processed (status: {listing['status']})", alert=True
            )
            return

        if action == "reject":
            _drafts.pop(listing_id, None)
            db.update_listing_status(listing_id, "rejected")
            db.record_audit("rejected", listing_id, actor_id=event.sender_id)
            try:
                await event.edit(f"❌ Listing #{listing_id} rejected and dismissed.")
            except Exception:
                await event.answer(f"❌ Rejected #{listing_id}", alert=True)
            return

        if action == "approve":
            # A draft (from ✏️ Edit) replaces the source content, and its price
            # line becomes the listing price if present.
            draft = _drafts.pop(listing_id, None)
            content_text = draft or listing.get("clean_text") or listing.get("raw_text") or ""
            content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
            our_price = listing.get("our_price")
            platform_name = listing.get("platform_name") or listing.get("game_name")
            intent = listing.get("intent") or "neutral"
            if draft:
                draft_price, _ = parser.extract_price(draft)
                if draft_price and draft_price > 0:
                    our_price = int(round(draft_price))
                    db.update_listing_fields(listing_id, our_price=our_price)
                    listing = {**listing, "our_price": our_price}

            await event.answer("Processing...")
            if db.is_paused():
                await event.edit(
                    f"⏸ **Paused** — publishing is stopped.\n"
                    f"Tap **▶ All Start** to resume, then approve again."
                )
                return

            post_number = db.next_post_number()
            republished_text, entities = parser.build_ai_message(
                content_lines=content_lines,
                our_price=our_price if (our_price is not None and our_price > 0) else None,
                platform=platform_name,
                contact_username=CONTACT_USERNAME,
                intent=intent,
                header_word=listing.get("header_word"),
                listing_seed=listing_id,
                post_number=post_number,
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
                        await event.edit(
                            f"⚠️ Listing #{listing_id} approved but send failed.\n"
                            f"Queued for retry. Error: {e}"
                        )
                    except Exception:
                        pass
                    return

                # Message IS on the channel now — under no circumstance re-queue it.
                try:
                    db.update_listing_status(
                        listing_id=listing_id,
                        status="published",
                        our_price=our_price,
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

                price_display = f"${parser._format_price(our_price)}" if our_price is not None else "No price (manual)"
                try:
                    confirmation = (
                        f"✅ **Listing #{listing_id} Published!**\n"
                        f"📌 **Post #{post_number}**\n"
                        f"Channel: {DEST_CHANNEL}\n"
                        f"Price: `{price_display}`"
                    )
                    await event.edit(confirmation)
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
                    await event.edit(
                        f"✅ Listing #{listing_id} marked approved.\n"
                        f"Will be dispatched to {DEST_CHANNEL} by the republisher.\n"
                        f"(User listener not connected — sent via the worker queue.)"
                    )
                except Exception:
                    pass

    @bot.on(events.NewMessage())
    async def handle_wizard_input(event):
        """Fallback: complete Add Source / Set Multiplier wizards by plain-text reply."""
        if event.sender_id != ADMIN_USER_ID or not event.text:
            return
        text = event.text.strip()
        if text.startswith("/"):
            return
        # Pasting an emoji-pack link must never be consumed by a wizard
        if EMOJI_LINK_RE.search(text):
            return
        # Menu taps must not be swallowed while a wizard is waiting
        if text in ("📊 Status", "⏳ Pending", "⚠️ Failed", "📋 Sources",
                    "⏸ All Stop", "▶ All Start", "❓ Help", "📜 Published"):
            _wizard_state.pop(ADMIN_USER_ID, None)
            return

        state = _wizard_state.get(ADMIN_USER_ID)
        if not state:
            return
        _wizard_state.pop(ADMIN_USER_ID, None)

        if state["step"] == "add":
            arg = text
            channel_id = None
            if arg.lstrip("-").isdigit():
                channel_id = int(arg)
                username = str(channel_id)
            else:
                username = arg.lstrip("@")
                if user_client_ref and user_client_ref.is_connected():
                    try:
                        entity = await user_client_ref.get_entity(arg)
                        channel_id = getattr(entity, "id", None)
                    except Exception as e:
                        logger.warning("Could not immediately resolve entity %s: %s", arg, e)
            db.add_supplier(username, channel_id=channel_id, markup_multiplier=DEFAULT_MULTIPLIER)
            db.record_audit("supplier_added", None, actor_id=ADMIN_USER_ID, detail=username)
            await event.reply(
                f"✅ Source **{arg}** added and activated!\n"
                f"Default multiplier: `{DEFAULT_MULTIPLIER}`",
                buttons=_home_keyboard(),
            )
            return

        if state["step"] == "edit":
            listing_id = state.get("listing_id")
            listing = db.get_listing_by_id(listing_id)
            if not listing:
                await event.reply("❌ Listing not found.", buttons=_home_keyboard())
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
            price_note = ""
            draft_price, _ = parser.extract_price(draft)
            if draft_price and draft_price > 0:
                price_note = f"\nPrice taken from your text: `${int(round(draft_price))}`"
            await event.reply(
                f"✏️ **Draft for Listing #{listing_id}** — review below, "
                f"then Approve or Edit again.{price_note}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{preview_text}",
                buttons=_listing_action_buttons(listing_id, listing["status"]),
                parse_mode="markdown",
            )
            return

        if state["step"] == "rule":
            sid = state.get("supplier_id")
            s = next(
                (x for x in db.list_suppliers(active_only=False) if x["id"] == sid),
                None,
            )
            if not s:
                await event.reply("❌ Source not found.", buttons=_home_keyboard())
                return
            try:
                mult = float(text)
                if mult <= 0 or mult > 5.0:
                    await event.reply(
                        "Multiplier should be a realistic positive number (e.g. 0.75).",
                        buttons=_home_keyboard(),
                    )
                    return
            except ValueError:
                await event.reply(
                    "Invalid multiplier. Send a decimal number, e.g. `0.80`.",
                    buttons=_home_keyboard(),
                )
                return
            db.set_supplier_rule(s["channel_username"], mult)
            db.record_audit("supplier_rule", None, actor_id=ADMIN_USER_ID, detail=str(mult))
            await event.reply(
                f"✅ Markup multiplier for **{_pretty_source(s.get('channel_username'), s.get('display_name'))}** set to `{mult}`.",
                buttons=_home_keyboard(),
            )
            return


async def create_admin_bot_client() -> TelegramClient:
    """Initialize and start the Telethon bot client."""
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
                BotCommand(command="sources", description="📋 Manage monitored sources"),
                BotCommand(command="published", description="📜 Published posts & channel links"),
                BotCommand(command="post", description="🔢 Look up a post by its number: /post 12"),
                BotCommand(command="addemoji", description="🎨 Add emoji pack: /addemoji <link>"),
                BotCommand(command="help", description="❓ Show buttons and shortcuts"),
            ]
        ))
    except Exception as e:
        logger.warning("Could not set bot commands menu: %s", e)

    logger.info("Admin bot started successfully.")
    return bot


async def main():
    """Standalone entry point for admin_bot.py."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    bot = await create_admin_bot_client()
    logger.info("Admin bot listening for commands from ADMIN_USER_ID %s...", ADMIN_USER_ID)
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())