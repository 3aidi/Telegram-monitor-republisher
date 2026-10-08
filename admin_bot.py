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
import texts

load_dotenv()

logger = logging.getLogger("admin_bot")

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0)
DEST_CHANNEL = db.get_dest_channel() or os.environ.get("DEST_CHANNEL", "")
CONTACT_USERNAME = os.environ.get("CONTACT_USERNAME", "")


def sync_dest_channel_to_env(channel: str, env_path: Optional[str] = None) -> bool:
    """Update or append DEST_CHANNEL in .env file if it exists."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        if not os.path.isfile(env_path):
            return False
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            if re.match(r"^\s*DEST_CHANNEL\s*=", line):
                new_lines.append(f"DEST_CHANNEL={channel}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"\nDEST_CHANNEL={channel}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return True
    except Exception as exc:
        logger.warning("Could not sync DEST_CHANNEL to %s: %s", env_path, exc)
        return False


def get_dest_channel() -> str:
    """Return the currently configured main destination channel."""
    global DEST_CHANNEL
    return DEST_CHANNEL or db.get_dest_channel() or os.environ.get("DEST_CHANNEL", "")


def set_dest_channel(channel: str, update_db: bool = True) -> str:
    """Update main destination channel in-memory, in db, and in .env."""
    global DEST_CHANNEL
    clean = str(channel).strip()
    DEST_CHANNEL = clean
    os.environ["DEST_CHANNEL"] = clean
    if update_db:
        try:
            db.set_dest_channel(clean)
        except Exception as e:
            logger.warning("Could not persist destination channel to db: %s", e)
    try:
        import main as main_mod
        if getattr(main_mod, "DEST_CHANNEL", None) != clean:
            main_mod.DEST_CHANNEL = clean
    except Exception:
        pass
    sync_dest_channel_to_env(clean)
    return clean

# Statuses in which a listing may still be edited / approved / skipped. Once a
# listing leaves this set (published, failed, skipped...) any in-flight admin
# action (edit wizard, Approve tap) must be refused, not silently applied.
EDITABLE_LISTING_STATUSES = ("pending_approval", "pending_review")

# Same dedup window main.py publishes with. Duplicated here (rather than
# imported) because admin_bot is a separate entry point that reads its config
# from the environment directly; the two MUST agree or an approve could apply a
# different duplicate window than the pipeline that queued the listing.
DEDUP_HOURS = int(os.environ.get("DEDUP_HOURS", "8") or 8)

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
forward_client_ref: Optional[TelegramClient] = None


def set_user_client(client: TelegramClient) -> None:
    """Set reference to the Telethon user client for immediate publishing on approval."""
    global user_client_ref
    user_client_ref = client


def set_forward_client(client: TelegramClient) -> None:
    """Set reference to the forward-dedicated Telethon client."""
    global forward_client_ref
    forward_client_ref = client


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

    reason_label = texts.REASONS.get(review_reason, texts.REASON_DEFAULT)

    if queue_pos:
        if as_next:
            header = texts.REVIEW_HEADER_NEXT_POS.format(pos=queue_pos, id=listing_id)
        else:
            header = texts.REVIEW_HEADER_POS.format(pos=queue_pos, reason=reason_label, id=listing_id)
    elif as_next:
        header = texts.REVIEW_HEADER_NEXT.format(id=listing_id)
    else:
        header = texts.REVIEW_HEADER.format(reason=reason_label, id=listing_id)
    price_info = texts.REVIEW_PRICE

    platform_display = platform.title() if platform and platform != "Unknown" else texts.REVIEW_PLATFORM_NONE

    content_section = ai_preview if ai_preview else raw_preview

    text = texts.REVIEW_CARD.format(
        header=header,
        source=_pretty_source(listing.get("supplier_username"), listing.get("supplier_display_name")),
        platform=platform_display,
        price=price_info,
        content=content_section,
        actions=texts.REVIEW_ACTIONS,
    )

    buttons = [
        [
            Button.inline(texts.BTN_PREVIEW, data=f"preview:{listing_id}"),
            Button.inline(texts.BTN_EDIT,    data=f"edit:{listing_id}"),
            Button.inline(texts.BTN_APPROVE, data=f"approve:{listing_id}"),
            Button.inline(texts.BTN_SKIP,    data=f"skip:{listing_id}"),
        ]
    ]
    buttons.extend(_channel_view_buttons(listing))

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode=None)
        logger.info("Sent approval prompt for listing #%s to admin %s", listing_id, admin_id)
    except Exception:
        logger.exception("Failed to send approval prompt to admin for listing #%s", listing_id)


async def send_review_notification(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
) -> None:

    listing_id = listing["id"]
    src = _pretty_source(
        listing.get("supplier_username"), listing.get("supplier_display_name")
    )
    src_part = texts.REVIEW_NOTICE_SOURCE.format(source=src) if src and src != "?" else ""
    text = texts.REVIEW_NOTICE.format(id=listing_id, source=src_part)

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
    post_disp = texts.PUBLISHED_POST.format(n=post_number) if post_number is not None else texts.PUBLISHED_LISTING.format(id=listing_id)
    platform_disp = platform.title() if platform else texts.PLATFORM_UNKNOWN

    text = texts.PUBLISHED_ALERT.format(
        post=post_disp,
        platform=platform_disp,
        source=_pretty_source(listing.get("supplier_username"), listing.get("supplier_display_name")),
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
    return texts.FORMAT_LISTING.format(
        n=n,
        id=l["id"],
        platform=platform,
        source=_pretty_source(l.get("supplier_username"), l.get("supplier_display_name")),
        status=l.get("status"),
        created=created,
    )


async def _header_emoji_for_listing(listing_id: int) -> list:
    """The listing's custom-emoji header as ``[(alt, document_id), ...]``.

    Delegates to db.resolve_header_for_listing, which draws one at random from
    the admin's pool the FIRST time a listing is rendered and pins it to the
    listing. That is what makes a preview trustworthy: /preview, the 👁️ button
    and the eventual publish all resolve to the same header. Returns [] while no
    header is configured, which renders the post with no header line.

    A first call performs a DB write (the pin), so it runs through
    db.run_async like every other write from async code (db.py ASYNC-1).
    """
    def _resolve() -> list:
        header_id = db.resolve_header_for_listing(listing_id)
        if header_id is None:
            return []
        header = db.get_header(header_id)
        if not header:
            return []
        return [(part["alt"], part["doc_id"]) for part in header["emoji"]]

    return await db.run_async(_resolve)


async def _build_preview_text(listing: dict) -> str:
    """Render the exact formatted post for a listing (used by /preview and 👁️ Preview button).

    Uses the stored AI-rewritten body (clean_text) directly — no re-AI call, so the
    button answers instantly and previews always match what will be published.
    """
    content_text = listing.get("clean_text") or listing.get("raw_text") or ""
    content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
    platform = listing.get("platform_name") or listing.get("game_name")

    preview_text, _ = parser.build_ai_message(
        content_lines=content_lines,
        platform=platform,
        contact_username=CONTACT_USERNAME,
        header_emoji=await _header_emoji_for_listing(listing["id"]),
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
    src = _pretty_source(k.get("channel_username"), k.get("display_name")) or "?"
    listing_part = f" — Listing #{k['listing_id']}" if k.get("listing_id") else ""
    reason = k.get("reason", "unknown")
    text = (
        f"⏳ Skipped — {reason} — {src}{listing_part}"
    )
    buttons = [[Button.inline(texts.BTN_REREVIEW, data=f"reskip:{k['skip_id']}")]]
    src_url = _source_url({
        "supplier_username": k.get("channel_username"),
        "supplier_channel_id": None,
        "source_message_id": k.get("message_id"),
    })
    if src_url:
        buttons[0].append(Button.url("View in Buyer channel", src_url))
    return text, buttons


# Human labels for the skip reasons the pipeline can report.
_SKIP_REASON_LABELS = {
    "duplicate": "duplicate",
    "chatter": "chatter",
    "no_content": "no_content",
    "not_a_listing": "not_a_listing",
    "admin_skip": "admin_skip",
}


async def send_skipped_alert(
    bot_client: TelegramClient,
    admin_id: int,
    listing: dict,
    reason: str,
) -> None:
    """Alert the admin that an inbound message was dropped.

    main.py has called this on every skip since long before the function existed,
    so each call raised AttributeError and was swallowed by the caller's bare
    ``except`` — the admin was never told a duplicate had been dropped. This is
    that missing function.

    Duplicates are GROUPED per burst. When the quarantine collapses five identical
    re-posts it calls this five times; each call looks up the other copies that
    lost to the same winner, and only the FIRST one (the lowest suppressed id)
    speaks. The result names the winner and every id folded into it, so a burst
    arrives as a single "kept #1, dropped #2 #3 #4 #5" card instead of five
    separate notifications — which is what made the review queue look flooded.
    Every other reason alerts individually, one line, as the pipeline intends.
    """
    listing_id = listing.get("id")
    winner_id = listing.get("duplicate_of")

    if reason == "duplicate" and winner_id:
        suppressed = [
            sid
            for sid in db.get_duplicate_suppressed_ids(winner_id)
            if sid != listing_id
        ]
        # Only the lowest-id member of the burst speaks for the group, so N twins
        # produce exactly one alert.
        if suppressed and listing_id != min([listing_id] + suppressed):
            return
        dropped = sorted(suppressed + [listing_id])
        dropped_text = " ".join(f"#{i}" for i in dropped if i is not None)
        src = _pretty_source(
            listing.get("supplier_username"), listing.get("supplier_display_name")
        )
        text = texts.DUP_BURST.format(
            winner=winner_id,
            source=src,
            count=len(dropped),
            suffix="y" if len(dropped) == 1 else "ies",
            dropped=dropped_text,
        )
    else:
        label = _SKIP_REASON_LABELS.get(reason, reason)
        src = _pretty_source(
            listing.get("supplier_username"), listing.get("supplier_display_name")
        )
        src_part = texts.SKIP_ALERT_SOURCE.format(source=src) if src and src != "?" else ""
        text = texts.SKIP_ALERT.format(reason=label, id=listing_id, source=src_part)

    buttons = None
    if reason != "duplicate" and listing_id:
        row_buttons = []
        try:
            skips = db.get_skipped_listings(limit=10)
            for s in skips:
                if s.get("listing_id") == listing_id or (
                    s.get("supplier_id") == listing.get("supplier_id")
                    and s.get("message_id") == listing.get("source_message_id")
                ):
                    row_buttons.append(
                        Button.inline(texts.BTN_REREVIEW, data=f"reskip:{s['skip_id']}")
                    )
                    break
        except Exception:
            pass

        src_url = _source_url({
            "supplier_username": listing.get("supplier_username") or listing.get("channel_username"),
            "supplier_channel_id": listing.get("supplier_channel_id") or listing.get("channel_id"),
            "source_message_id": listing.get("source_message_id"),
        })
        if src_url:
            row_buttons.append(Button.url(texts.BTN_VIEW_BUYER, src_url))
        if row_buttons:
            buttons = [row_buttons]

    try:
        await bot_client.send_message(admin_id, text, buttons=buttons, parse_mode="markdown")
    except Exception:
        logger.exception(
            "Failed to send skip alert for listing #%s (reason %s)",
            listing_id,
            reason,
        )

    # Advance the skip digest marker so the background digest does not duplicate this alert
    try:
        recent = db.get_skipped_listings(limit=1)
        if recent and recent[0].get("skip_id"):
            await db.run_async(db.set_skip_digest_marker, recent[0]["skip_id"])
    except Exception:
        pass


async def skip_digest_worker(bot: TelegramClient) -> None:
    """Per-skip DM cards for skipped messages (SKIP-1): fires at most once per
    10-minute window, only when new un-alerted skips exist since the last marker,
    sending ONE card per newly-skipped message (capped by SKIP_DIGEST_MAX_CARDS)
    so each card carries its own Re-review + source-link buttons."""
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
                if k["skip_id"] > marker
                and k.get("reason") != "admin_skip"
                and k.get("reason") != "duplicate"
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
    clean_text, else its raw_text — rendered in a monospace code block for
    1-tap copying on mobile."""
    content = (existing_draft or "").strip()
    if not content:
        content = (listing.get("clean_text") or "").strip()
    if not content:
        content = (listing.get("raw_text") or "").strip()
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        body = texts.EDIT_EMPTY
    else:
        raw_clean = "\n".join(lines).replace("```", "'''")
        body = f"📋 **Tap below to copy:**\n```{raw_clean}```"
    return texts.EDIT_PROMPT.format(id=listing["id"], body=body)


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

# ---------------------------------------------------------------------------
# Custom-emoji headers.
#
# The post header is a short run of custom emoji the admin supplies from this
# bot (currently three, spelling WTB). Headers live in a pool: /headers manages
# it, and each post draws one at random (see db.resolve_header_for_listing,
# which pins the draw to the listing so the preview always matches what
# publishes).
# ---------------------------------------------------------------------------
# Deliberately NOT on any home keyboard, and there is no /addheader: /headers is
# the ONLY entry point, and the ➕ row inside that list is what arms the capture.
# So the Home screen stays the short list of things that get touched constantly,
# and adding a header never needs a command to be remembered.
# The label is kept as a constant because the router still accepts it (a tap from
# an old message, or a keyboard that was cached client-side before the button was
# removed) and because the wizard menu-tap allowlist references it.
HEADERS_BTN = "🅰 Headers"
# Same pattern as Destinations: the label is shared by the router and the
# allowlist, so a rename on one side can't silently break the other.
HEADERS_ROUTE_RE = r"^(?:/headers|" + HEADERS_BTN + r")$"
# Exactly how many custom emoji make up one header. Validated on capture so a
# mis-sent message can never become a half-header.
HEADER_EMOJI_COUNT = 3

# The Menu button contents, as (command, description). Module-level so the list
# is testable — the request below only runs at startup, which never happens in
# tests, so an entry removed here would otherwise be invisible to them.
# Keep in sync with the NewMessage handlers: listing a command here that has no
# handler leaves a dead entry in Telegram's menu.
BOT_MENU = [
    ("status", " Today's stats report"),
    ("pending", "Pending approval listings"),
    ("skipped", "Recently skipped messages"),
    ("sources", " Manage monitored sources"),
    ("published", "Published posts & channel links"),
    ("headers", " Manage the custom-emoji WTB headers"),
    ("setchannel", "Change or view main destination channel"),
    ("post", " Look up a post by its number: /post 12"),
    ("help", " Show buttons and shortcuts"),
]


def _custom_emoji_spans(message) -> List[Dict[str, object]]:
    """Every custom emoji in a message, in typed order, with its text span.

    Each entry is ``{"alt", "doc_id", "start", "end"}`` where start/end are
    BYTE offsets into the UTF-16-LE encoding of the message text (i.e. Telegram's
    own unit x 2), which is the only unit custom-emoji entity offsets/lengths are
    expressed in.

    Telegram stores a custom emoji two ways in the same message: the glyph sits
    in the message TEXT at the entity's span, and a MessageEntityCustomEmoji
    points at the document that replaces it. The glyph is therefore read straight
    out of the text, which is the same anchor assumption the country flags and the
    price/contact emoji already rely on.

    The alt cannot be fetched from the API instead: the reply type of
    messages.getCustomEmojiDocuments is not part of Telethon 1.44's layer-227
    schema, so a request for it cannot even be deserialized. Reading the text
    avoids that entirely and needs no extra round trip.

    Encoding to UTF-16-LE before slicing is what keeps a multi-code-unit alt
    (a regional-indicator flag is 4 units, a flag sequence 14) intact rather than
    being cut in half. Results are sorted by offset so the parts come back in the
    order the admin typed them.
    """
    text = getattr(message, "text", None) or ""
    if not text:
        return []
    from telethon.tl.types import MessageEntityCustomEmoji

    utf16 = text.encode("utf-16-le")
    found: List[Dict[str, object]] = []
    for ent in getattr(message, "entities", None) or []:
        if not isinstance(ent, MessageEntityCustomEmoji):
            continue
        start = int(ent.offset) * 2
        end = start + int(ent.length) * 2
        if start < 0 or end > len(utf16) or end <= start:
            continue
        try:
            alt = utf16[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if not alt:
            continue
        found.append(
            {"alt": alt, "doc_id": int(ent.document_id), "start": start, "end": end}
        )
    found.sort(key=lambda span: span["start"])
    return found


def _extract_custom_emoji(message) -> List[Dict[str, object]]:
    """The custom emoji of a message as plain {"alt", "doc_id"} parts, in order."""
    return [
        {"alt": span["alt"], "doc_id": span["doc_id"]}
        for span in _custom_emoji_spans(message)
    ]


def _non_emoji_residue(message, spans: List[Dict[str, object]]) -> str:
    """Whatever text a message holds OUTSIDE its custom emoji, whitespace stripped.

    Used to reject a header attempt that came with words, prices or links glued
    to the emoji: the emoji alone is stored, so any other text would be silently
    thrown away, and the admin would think it was saved. Returns "" when the
    message is nothing but the custom emoji (spaces/newlines between them are
    fine), otherwise a short truncated sample for the error message.
    """
    text = getattr(message, "text", None) or ""
    if not text:
        return ""
    utf16 = text.encode("utf-16-le")
    # Blank out each custom-emoji span, then decode what's left. The filler MUST
    # itself be UTF-16 encoded: b" " is a single 0x20 byte, and two of them
    # (0x20 0x20) decode as U+2020 rather than as whitespace, which would leave
    # the emoji as mojibake in the error message. A single space character
    # encodes to the 0x20 0x00 unit and keeps the stream aligned.
    space_unit = " ".encode("utf-16-le")
    buf = bytearray(utf16)
    for span in spans:
        width = int(span["end"]) - int(span["start"])
        buf[span["start"]:span["end"]] = space_unit * (width // 2)
    try:
        residue = bytes(buf).decode("utf-16-le", errors="ignore")
    except UnicodeDecodeError:
        residue = ""
    residue = re.sub(r"\s+", " ", residue).strip()
    return residue[:60]


def _headers_menu_text(headers: List[dict]) -> str:
    """Caption for the header list. Empty state points at the Add row below it."""
    if not headers:
        return texts.HEADERS_EMPTY.format(n=HEADER_EMOJI_COUNT)
    s = "s" if len(headers) != 1 else ""
    return texts.HEADERS_MENU.format(count=len(headers), s=s)


def _headers_buttons(headers: List[dict]) -> List[List[object]]:
    """One 🗑 row per header, then Add, then the standard Home row.

    Also the footer for every capture-failure prompt: the Home keyboard no longer
    carries a Headers button, so without these rows a rejected capture would
    strand the admin with no way back to the list or to a retry.
    """
    buttons: List[List[object]] = []
    for h in headers:
        # Label by the header's own text (the alt glyphs, e.g. "WTB") so the row is
        # recognisable, with the id for the admin to refer to it by.
        buttons.append([
            Button.inline(f"🗑 #{h['id']} — {h.get('text') or '?'}", data=f"delheader:{h['id']}")
        ])
    buttons.append([Button.inline("➕ Add Header", data="headeradd")])
    buttons.extend(_home_button_row())
    return buttons


def _home_keyboard() -> List[List[object]]:
    pause_label = texts.BTN_RESUME if db.is_paused() else texts.BTN_PAUSE
    return [
        [Button.text("⏳ Pending", resize=True), Button.text("📋 Sources", resize=True)],
        [Button.text("✅ Published", resize=True), Button.text(DESTINATIONS_BTN, resize=True)],
        [Button.text("🚫 Skipped", resize=True), Button.text(pause_label, resize=True)],
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
        row.append(Button.url(texts.BTN_VIEW_BUYER, src_url))
    dest_url = _destination_url(listing)
    if dest_url:
        row.append(Button.url(texts.BTN_VIEW_DEST, dest_url))
    buttons = [row] if row else []
    if listing.get("published_message_id") and listing.get("status") != "sold":
        buttons.append([Button.inline(texts.BTN_SOLD, data=f"sold:{listing['id']}")])
    return buttons


def _asleep_label() -> str:
    """Label for the 'I'm Asleep' toggle button (mirrors the pause label)."""
    return texts.BTN_AWAKE if db.is_buyer_asleep() else texts.BTN_ASLEEP


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
                Button.inline(texts.BTN_EDIT, data=f"edit:{listing_id}"),
                Button.inline(texts.BTN_APPROVE, data=f"approve:{listing_id}"),
                Button.inline(texts.BTN_SKIP, data=f"skip:{listing_id}"),
            ]
        ]
    buttons.extend(_channel_view_buttons(listing))
    return buttons


from services.fsm import PersistentDict

# Wizard state: sender_id -> {"step": "add" | "edit", ...}
_wizard_state: Dict[int, dict] = PersistentDict(prefix="wiz:")
# Listing drafts: listing_id -> content lines typed by the admin via ✏️ Edit.
_drafts: Dict[int, str] = PersistentDict(prefix="draft:")


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
    toggle_label = texts.BTN_PAUSE_ITEM if s["active"] else texts.BTN_RESUME_ITEM
    if not s.get("channel_id"):
        status_line = texts.SOURCE_UNRESOLVED
    else:
        status_line = texts.SOURCE_STATUS.format(state=texts.STATE_ACTIVE if s["active"] else texts.STATE_PAUSED)
    await _message_delete_send(
        event,
        texts.SOURCE_DETAIL.format(
            icon=icon,
            name=handle,
            status=status_line,
            id=s['channel_id'] or texts.ID_UNRESOLVED,
        ),
        buttons=[
            [Button.inline(toggle_label, data=f"suptoggle:{sid}")],
            [Button.inline(texts.BTN_DELETE_PERM, data=f"supdel:{sid}")],
            [Button.inline(texts.BTN_BACK, data="menu:sources")],
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
                texts.SOURCE_BAD_FORWARD,
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
                texts.SOURCE_ADDED_BY_FWD_UNRESOLVED.format(id=channel_id),
                buttons=_home_keyboard(),
            )
        else:
            await event.reply(
                texts.SOURCE_ADDED_BY_FWD.format(display=display, id=channel_id),
                buttons=_home_keyboard(),
            )
        return

    username, numeric = _supplier_ref_from_text(text)
    if not username:
        await event.reply(
            texts.SOURCE_BAD_REF,
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
            texts.SOURCE_ADDED_RESOLVED.format(display=display, id=entity_id),
            buttons=_home_keyboard(),
        )
        return

    _wizard_state[ADMIN_USER_ID] = {"step": "add_confirm_unresolved", "raw": text}
    await event.reply(
        texts.SOURCE_UNRESOLVED_ASK.format(text=text[:60]),
        buttons=[
            [Button.inline(texts.BTN_ADD_ANYWAY, data="supaddunresolved")],
            [Button.inline(texts.BTN_CANCEL, data="wiz:cancel")],
        ],
        parse_mode=None,
    )


_PAGE_SIZE = 6
_DEST_PAGE_SIZE = 15


def _page_window(
    total: int, page: int, page_size: Optional[int] = None
) -> Tuple[int, int, int, bool, bool]:
    """Compute pagination indices for a zero-based ``page``.

    Returns ``(start, end, page_count, has_prev, has_next)``. Negative pages
    clamp to 0; pages past the final page clamp to the last valid page;
    ``total=0`` yields an empty first page so callers render the normal empty
    result instead of crashing. The returned ``start`` is always ``page * size``
    for the clamped page.
    """
    size = page_size or _PAGE_SIZE
    if total <= 0:
        return 0, 0, 1, False, False
    page_count = (total + size - 1) // size
    page = max(0, min(int(page), page_count - 1))
    start = page * size
    end = min(start + size, total)
    return start, end, page_count, page > 0, end < total


def _nav_row(
    kind: str, page: int, page_count: int, back_data: Optional[str] = None
) -> List[List[object]]:
    """Navigation row: [⬅️ Back] [➡️ Next] (no page indicator button).

    Back steps to the previous page; on the first page it falls back to
    ``back_data`` (the parent menu) when given, otherwise it is omitted. Next
    appears only when a further page exists. Returns [] when the row would be
    empty, so callers can always ``extend`` with the result."""
    row = []
    if page > 0:
        row.append(Button.inline(texts.BTN_BACK, data=f"{kind}:page:{page - 1}"))
    elif back_data:
        row.append(Button.inline(texts.BTN_BACK, data=back_data))
    if page + 1 < page_count:
        row.append(Button.inline(texts.BTN_NEXT, data=f"{kind}:page:{page + 1}"))
    return [row] if row else []


def _page_footer(page: int, page_count: int) -> str:
    """'Page X of Y' for multi-page lists; '' for a single page."""
    if page_count <= 1:
        return ""
    return f"Page {page + 1} of {page_count}"


def _sources_menu_text(suppliers: List[dict], page: int = 0) -> str:
    if not suppliers:
        return texts.SOURCES_EMPTY
    _, _, page_count, _, _ = _page_window(len(suppliers), page)
    footer = _page_footer(page, page_count)
    msg = texts.SOURCES_MENU
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
    buttons.extend(_nav_row("sup", page, page_count, back_data="menu:home"))
    buttons.append([Button.inline(texts.BTN_ADD_SOURCE, data="supadd")])
    return buttons


# ---- Destinations submenu (DEST-1) ----------------------------------------
def _destination_icon(d: dict) -> str:
    if not d.get("active"):
        return "🔴"
    # DEST-HEALTH: the active/not-active dot is not enough on its own. A group
    # the account was banned from still shows as "active" and still swallows a
    # doomed forward on every post, so surface delivery health next to it.
    if d.get("is_dead"):
        return "❌"
    if d.get("is_flapping"):
        return "⚠️"
    if d.get("is_throttled"):
        return "⏳"
    return "🟢"


def _destination_health_note(d: dict) -> str:
    """Short delivery-health suffix for a destination row, or '' if healthy."""
    if not d.get("active"):
        return " (disabled)"
    if d.get("is_dead"):
        return f" — not delivering ({d.get('failures', 0)}/{d.get('attempts', 0)} failed)"
    if d.get("is_flapping"):
        return f" — {d.get('fail_pct', 0):.0f}% failed"
    if d.get("is_throttled"):
        return f" — rate limited, {d.get('deferred', 0)} queued"
    return ""


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


def _destination_username(chat_ref) -> str:
    """Public @handle of a destination peer, or '' when it has no public username.

    A destination stores exactly one peer reference in chat_id: '@username' for a
    public chat/channel/group, or the marked numeric id ('-100...') for a private
    one (see db._destination_chat_ref). Only the '@' form names a chat Telegram
    can open from a link, so a numeric id deliberately yields no handle. The
    handle grammar mirrors db._destination_chat_ref, so anything that could be
    stored can be linked and a half-typed handle never becomes a dead link.
    """
    ref = str(chat_ref or "").strip()
    if not ref.startswith("@"):
        return ""
    handle = ref[1:]
    if not handle or not all(c.isalnum() or c == "_" for c in handle):
        return ""
    return handle


def destination_open_url(chat_ref) -> Optional[str]:
    """t.me URL that opens a destination chat, or None when it isn't public.

    Public rather than module-private because the destination health DM in
    main.py links the same handles.

    'https://t.me/<handle>' is the one deep link that reliably opens a public
    chat, channel or group. A private destination has no handle and therefore no
    public URL, so nothing is built for it: its 't.me/c/<id>' form only resolves
    inside a client that already holds the entity, so handing it out would add a
    button that silently fails. Callers must treat None as "show plain text".
    """
    handle = _destination_username(chat_ref)
    return f"https://t.me/{handle}" if handle else None


def _destination_open_button(chat_ref, label: str = texts.BTN_OPEN_CHAT):
    """A Button.url that opens the destination chat, or None if it is private."""
    url = destination_open_url(chat_ref)
    return Button.url(label, url) if url else None


def _destination_id_line(chat_ref) -> str:
    """The detail screen's 'ID:' line.

    A public @username is rendered as a normal clickable link, so tapping it
    opens the chat in Telegram. A numeric chat id is an internal identifier with
    no public URL, so it stays plain text — no code span (which read as
    copyable code) and no link Telegram could not open. Missing refs render as ''
    so the caller can drop the line entirely.
    """
    ref = str(chat_ref or "").strip()
    if not ref:
        return ""
    url = destination_open_url(ref)
    return f"ID: [{ref}]({url})" if url else f"ID: {ref}"


def _destination_markup(text, link_ref=None) -> str:
    """Render a destination identifier for the admin: a t.me link when public.

    Used by the one-off add/confirm messages, where an identifier is shown inline
    instead of on the detail screen. `link_ref` is the stored peer reference
    ('@handle' or a numeric id) that decides whether a link is possible, so a
    public username is tappable while a numeric id or an unrecognised reference
    stays plain text rather than becoming a link that cannot resolve.
    """
    shown = str(text or "").strip()
    if not shown:
        return ""
    url = destination_open_url(link_ref if link_ref is not None else text)
    return f"[{shown}]({url})" if url else shown


def _destinations_menu_text(destinations: List[dict], page: int = 0) -> str:
    if not destinations:
        return texts.DESTS_EMPTY
    _, _, page_count, _, _ = _page_window(len(destinations), page, page_size=_DEST_PAGE_SIZE)
    footer = _page_footer(page, page_count)
    msg = texts.DESTS_MENU
    if footer:
        msg += footer + "\n"
    return msg


def _destinations_buttons(destinations: List[dict], page: int = 0) -> List[List[object]]:
    start, end, page_count, _, _ = _page_window(len(destinations), page, page_size=_DEST_PAGE_SIZE)
    buttons = []
    for d in destinations[start:end]:
        icon = _destination_icon(d)
        label = f"{icon} {_destination_label(d)}{_destination_health_note(d)}"
        # The list is a management menu only: every row stays a single 'dest:<id>'
        # button that opens the detail screen. The chat's t.me link lives on that
        # screen (_edit_destination_menu), not here, so this list keeps one
        # predictable tap target per destination.
        buttons.append([Button.inline(label, data=f"dest:{d['id']}")])
    buttons.extend(_nav_row("dest", page, page_count, back_data="menu:home"))
    buttons.append([
        Button.inline(texts.BTN_ADD_DEST, data="destadd"),
        Button.inline(texts.BTN_MAIN_CHANNEL, data="mainchan:view"),
    ])
    return buttons


async def _edit_destination_menu(event, d: dict) -> None:
    """Render the destination detail menu (also used after an enable/disable tap)."""
    did = d["id"]
    icon = _destination_icon(d)
    label = _destination_label(d)
    toggle_label = texts.BTN_PAUSE_TRAILING if d["active"] else texts.BTN_RESUME_ITEM
    # DEST-HEALTH: show WHY a destination is not receiving, and the last error,
    # so a banned group is diagnosable without reading the journal.
    health_lines = ""
    attempts = int(d.get("attempts") or 0)
    if d.get("is_throttled"):
        # Rate limiting is the account's problem, not this group's. Say so, so
        # the admin does not disable a destination that will deliver fine.
        health_lines = texts.DEST_HEALTH_THROTTLED.format(deferred=d.get('deferred', 0))
    elif attempts:
        if d.get("is_dead"):
            verdict = texts.DEST_V_DEAD
        elif d.get("is_flapping"):
            verdict = texts.DEST_V_FLAPPING.format(pct=d.get('fail_pct', 0))
        elif d.get("fail_pct", 0) >= 25:
            verdict = texts.DEST_V_FAILING.format(pct=d.get('fail_pct', 0))
        else:
            verdict = texts.DEST_V_OK
        health_lines = texts.DEST_HEALTH_ATTEMPTS.format(
            verdict=verdict,
            successes=d.get('successes', 0),
            attempts=attempts,
        )
        if d.get("last_success_at"):
            health_lines += texts.DEST_HEALTH_LAST_OK.format(when=d['last_success_at'][:16].replace('T', ' '))
    else:
        health_lines = texts.DEST_HEALTH_NO_ATTEMPTS
    if d.get("last_error"):
        reason = str(d["last_error"]).split(" (caused by")[0][:120]
        health_lines += texts.DEST_HEALTH_LAST_ERR.format(reason=reason)
    id_line = _destination_id_line(d.get("chat_id"))
    open_chat = _destination_open_button(d.get("chat_id"))
    rows = []
    if open_chat is not None:
        rows.append([open_chat])
    rows.append([Button.inline(toggle_label, data=f"desttoggle:{did}")])
    rows.append([Button.inline(texts.BTN_DELETE_PERM, data=f"destdel:{did}")])
    rows.append([Button.inline(texts.BTN_BACK, data="menu:destinations")])
    header = texts.DEST_HEADER.format(
        icon=icon,
        name=label,
        state=texts.DEST_STATE_ON if d['active'] else texts.DEST_STATE_OFF,
    )
    if id_line:
        header += f"\n{id_line}"
    await _message_delete_send(
        event,
        texts.DEST_DETAIL_CARD.format(header=header, health=health_lines),
        buttons=rows,
    )


async def _resolve_destination_peer(target) -> Optional[object]:
    """Resolve a destination chat entity using forward_client_ref or user_client_ref."""
    clients = []
    if forward_client_ref and forward_client_ref.is_connected():
        clients.append(forward_client_ref)
    if user_client_ref and user_client_ref.is_connected() and user_client_ref not in clients:
        clients.append(user_client_ref)

    for cl in clients:
        try:
            return await cl.get_entity(target)
        except Exception:
            try:
                await cl.get_dialogs(limit=100)
                return await cl.get_entity(target)
            except Exception:
                pass
    return None


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
                texts.DEST_BAD_FORWARD,
                buttons=_home_keyboard(),
            )
            return
        chat_ref = db.normalize_channel_id(raw_chat_id)
        entity = await _resolve_destination_peer(chat_ref)
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
            texts.DEST_ADD_BY_FWD.format(
                display=_destination_markup(display),
                id=_destination_markup(chat_ref),
            ),
            buttons=_home_keyboard(),
        )
        return

    username, numeric = _supplier_ref_from_text(text)
    raw_text = (text or "").strip()
    if not username and not raw_text.startswith("http"):
        await event.reply(
            texts.DEST_BAD_REF,
            buttons=_home_keyboard(),
        )
        return

    reference = int(numeric) if numeric is not None else (username or raw_text)
    entity = await _resolve_destination_peer(reference)

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
            texts.DEST_ADDED.format(
                display=_destination_markup(store_ref if entity_username else display, store_ref)
            ),
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
            texts.DEST_ADDED.format(
                display=_destination_markup(display) + f" (ID {_destination_markup(marked_id)})"
            ),
            buttons=_home_keyboard(),
        )
        return

    _wizard_state[ADMIN_USER_ID] = {"step": "adddest_confirm_unresolved", "raw": text}
    await event.reply(
        texts.DEST_UNVERIFIED_PROMPT.format(name=_destination_markup(text)),
        buttons=[
            [Button.inline(texts.BTN_ADD_ANYWAY, data="destaddunresolved")],
            [Button.inline(texts.BTN_CANCEL_X, data="wiz:cancel")],
        ],
    )


async def _run_set_main_channel_flow(event, text: Optional[str] = None, fwd=None) -> None:
    """Validate, resolve, and persist the new main destination channel."""
    if fwd is not None:
        raw_chat_id = _channel_id_from_fwd(fwd)
        if raw_chat_id is None:
            await event.reply(
                texts.MAIN_CHANNEL_BAD_REF,
                buttons=_home_keyboard(),
                parse_mode="markdown",
            )
            return
        chat_ref = str(db.normalize_channel_id(raw_chat_id))
        entity = await _resolve_destination_peer(db.to_peer_reference(chat_ref))
        display = _entity_display(entity, fallback=f"channel {chat_ref}")
        old_channel = get_dest_channel()
        set_dest_channel(chat_ref)
        await db.run_async(
            db.record_audit,
            "main_channel_changed",
            None,
            actor_id=ADMIN_USER_ID,
            detail=f"{old_channel} -> {chat_ref} ({display})",
        )
        await event.reply(
            texts.MAIN_CHANNEL_UPDATED.format(
                display=f"{display} (`{chat_ref}`)",
                previous=old_channel or "None",
            ),
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )
        return

    raw_text = (text or "").strip()
    username, numeric = _supplier_ref_from_text(raw_text)
    if not username and numeric is None and not (raw_text.startswith("-100") and raw_text[1:].isdigit()):
        await event.reply(
            texts.MAIN_CHANNEL_BAD_REF,
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )
        return

    if numeric is not None:
        target_ref = str(db.normalize_channel_id(numeric))
    elif raw_text.startswith("-100") and raw_text[1:].isdigit():
        target_ref = raw_text
    elif username:
        target_ref = f"@{username.lstrip('@')}"
    else:
        target_ref = raw_text

    entity = await _resolve_destination_peer(db.to_peer_reference(target_ref))
    old_channel = get_dest_channel()
    set_dest_channel(target_ref)
    await db.run_async(
        db.record_audit,
        "main_channel_changed",
        None,
        actor_id=ADMIN_USER_ID,
        detail=f"{old_channel} -> {target_ref}",
    )

    if entity is not None:
        display = _entity_display(entity, fallback=target_ref)
        await event.reply(
            texts.MAIN_CHANNEL_UPDATED.format(
                display=f"{display} (`{target_ref}`)",
                previous=old_channel or "None",
            ),
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )
    else:
        await event.reply(
            texts.MAIN_CHANNEL_UPDATED_UNVERIFIED.format(
                channel=target_ref,
                previous=old_channel or "None",
            ),
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )


def _home_button_row() -> List[List[object]]:
    """Inline "back to Home" row used by every section rendered from the Home menu."""
    return [[Button.inline(texts.BTN_HOME, data="menu:home")]]


def _home_text() -> str:
    return texts.HOME_LANDING


def _home_inline_keyboard() -> List[List[object]]:
    """Inline navigation for the Home landing message (mirrors the reply keyboard)."""
    pause_label = texts.BTN_RESUME if db.is_paused() else texts.BTN_PAUSE
    return [
        [
            Button.inline("⏳ Pending", data="home:pending"),
            Button.inline("📋 Sources", data="menu:sources"),
        ],
        [
            Button.inline("✅ Published", data="home:published"),
            Button.inline(DESTINATIONS_BTN, data="menu:destinations"),
        ],
        [
            Button.inline("🚫 Skipped", data="home:skipped"),
            Button.inline(pause_label, data="home:toggle"),
        ],
    ]


def _help_text() -> str:
    return texts.HELP.format(destinations=DESTINATIONS_BTN)


def _status_report_text() -> str:
    stats = db.get_today_stats()

    supplier_lines = []
    for s in stats["supplier_breakdown"]:
        handle = _pretty_source(s.get("channel_username"), s.get("supplier_display_name"))
        supplier_lines.append(
            texts.STATUS_SUPPLIER_LINE.format(
                name=handle,
                processed=s['processed'],
                published=s['published'],
                skipped=s['skipped'],
            )
        )
    if not supplier_lines:
        supplier_lines = [texts.STATUS_NONE]
    supplier_text = "\n\n".join(supplier_lines)

    reason_lines = [texts.STATUS_REASON_LINE.format(reason=r, count=c) for r, c in stats["skip_reasons"].items()]
    if not reason_lines:
        reason_lines = [texts.STATUS_NONE]
    reason_text = "\n\n".join(reason_lines)

    curr_dest = get_dest_channel()
    main_channel_info = f"• Main Channel: `{curr_dest}`\n" if curr_dest else ""

    msg = (
        texts.STATUS_HEADER
        + main_channel_info
        + texts.STATUS_TOTALS.format(
            active=stats['active_suppliers'],
            processed=stats['total_processed'],
            published=stats['published'],
            pending=stats['pending'],
            skipped=stats['total_skipped'],
        )
        + texts.STATUS_BY_SUPPLIER.format(suppliers=supplier_text)
        + texts.STATUS_BY_REASON.format(reasons=reason_text)
    )
    if db.is_paused():
        msg = texts.STATUS_PAUSED_BANNER + msg
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
    text = texts.SKIPPED_TITLE
    if reopenable:
        dropped = len(skips) - len(reopenable)
        if dropped:
            text += texts.SKIPPED_DROPPED_INFO.format(dropped=dropped)
    else:
        text += texts.SKIPPED_NONE_OPEN
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
    text = texts.PUBLISHED_TITLE
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
        if p.get("published_message_id") and p.get("status") != "sold":
            row.append(Button.inline(texts.BTN_SOLD, data=f"sold:{p['id']}"))
        if row:
            buttons.append(row)
    if total is not None:
        total = max(int(total), len(rows))
        _, _, page_count, _, _ = _page_window(total, page)
        footer = _page_footer(page, page_count)
        if footer:
            text += f"\n\n{footer}"
        buttons.extend(_nav_row("pub", page, page_count))
    buttons.append([Button.inline(texts.BTN_SEARCH_POST, data="published:search")])
    buttons.extend(_home_button_row())
    return text, buttons


def _review_position_label(remaining: int) -> str:
    """Queue position for the next card: "2/4" when the inbox session's total is
    known and the math still holds, else "N remaining" (e.g. a new listing
    arrived mid-session, or no /pending session is active)."""
    total = _review_session_total
    if total is None or total < 1:
        return texts.REVIEW_POS_REMAINING.format(remaining=remaining)
    current = _review_session_done + 1
    if current > total:
        return texts.REVIEW_POS_REMAINING.format(remaining=remaining)
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
                texts.QUEUE_FINISHED,
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
        await event.reply(texts.NO_PENDING_PAGE)
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
    lines = texts.POST_CARD.format(
        n=post_number,
        platform=platform,
        source=supplier,
    )
    buttons = []
    link_row = []
    src_url = _source_url(post)
    if src_url:
        link_row.append(Button.url(texts.BTN_VIEW_BUYER, src_url))
    dest_url = _destination_url(post)
    if dest_url:
        link_row.append(Button.url(texts.BTN_VIEW_DEST, dest_url))
    if link_row:
        buttons.append(link_row)
    if post.get("published_message_id") and post.get("status") != "sold":
        buttons.append([Button.inline(texts.BTN_SOLD, data=f"sold:{post['id']}")])
    buttons.append([
        Button.inline(texts.BTN_BACK, data="home:published"),
        Button.inline(texts.BTN_HOME, data="menu:home"),
    ])
    return lines, buttons


async def _execute_sold(
    event,
    listing_id: Optional[int] = None,
    post_number: Optional[int] = None,
) -> None:
    """Mark a listing as sold:
    1. Replies to the published message in DEST_CHANNEL with SOLD OUT.
    2. Deletes the forwarded message from all destination groups/channels.
    3. Cancels any pending forwardings.
    4. Updates listing status to 'sold' in DB and writes audit log.
    5. Confirms action to the admin.
    """
    listing = None
    if listing_id is not None:
        listing = db.get_listing_by_id(listing_id)
    elif post_number is not None:
        listing = db.get_post_by_number(post_number)
        if not listing:
            listing = db.get_listing_by_id(post_number)

    if not listing:
        ref_label = str(post_number if post_number is not None else listing_id)
        msg = texts.SOLD_NOT_FOUND.format(post=ref_label)
        if hasattr(event, "answer"):
            await event.answer("Listing not found", alert=True)
        await event.reply(msg)
        return

    actual_id = listing["id"]
    post_num = listing.get("post_number") or actual_id

    if listing.get("status") == "sold":
        msg = texts.SOLD_ALREADY.format(post=post_num)
        if hasattr(event, "answer"):
            await event.answer("Already marked as SOLD", alert=True)
        await event.reply(msg)
        return

    published_msg_id = listing.get("published_message_id")
    if not published_msg_id:
        msg = texts.SOLD_NOT_PUBLISHED.format(post=post_num)
        if hasattr(event, "answer"):
            await event.answer("Post not published yet", alert=True)
        await event.reply(msg)
        return

    # 1. Reply to published post in main channel (DEST_CHANNEL)
    dest_channel = get_dest_channel()
    if dest_channel:
        dest_peer = db.to_peer_reference(dest_channel)
        sender = user_client_ref if (user_client_ref and user_client_ref.is_connected()) else (
            event.client if hasattr(event, "client") else None
        )
        if sender:
            try:
                import inspect
                sig = inspect.signature(sender.send_message)
                send_kwargs = {}
                if "reply_to" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    send_kwargs["reply_to"] = published_msg_id
                platform_val = listing.get("platform_name") or listing.get("game_name")
                reply_text = texts.format_sold_reply(post_num, platform_val)
                await sender.send_message(dest_peer, reply_text, **send_kwargs)
                logger.info("Replied SOLD to post #%s (msg %s) in %s: %s", post_num, published_msg_id, dest_channel, reply_text)
            except Exception as exc:
                logger.warning("Could not send SOLD reply to %s msg %s: %s", dest_channel, published_msg_id, exc)

    # 2. Cancel pending forwardings
    await db.run_async(db.cancel_pending_forwardings_for_listing, actual_id)

    # 3. Delete forwarded messages from destination groups
    fwds = await db.run_async(db.get_forwardings_for_listing, actual_id)
    del_sender = forward_client_ref if (forward_client_ref and forward_client_ref.is_connected()) else (
        user_client_ref if (user_client_ref and user_client_ref.is_connected()) else (
            event.client if hasattr(event, "client") else None
        )
    )
    deleted_count = 0
    if del_sender:
        for f in fwds:
            if f.get("status") == "forwarded":
                dest_chat = f.get("destination_chat_id")
                dest_msg_id = f.get("destination_message_id")
                if dest_chat:
                    to_peer = db.to_peer_reference(dest_chat)
                    if not dest_msg_id and hasattr(del_sender, "get_messages"):
                        try:
                            msgs = await del_sender.get_messages(to_peer, limit=25)
                            for m in msgs:
                                fwd_header = getattr(m, "fwd_from", None)
                                if fwd_header and getattr(fwd_header, "channel_post", None) == published_msg_id:
                                    dest_msg_id = getattr(m, "id", None)
                                    break
                        except Exception:
                            pass
                    if dest_msg_id and hasattr(del_sender, "delete_messages"):
                        try:
                            await del_sender.delete_messages(to_peer, [dest_msg_id])
                            deleted_count += 1
                            logger.info("Deleted forwarded msg %s from %s for sold post #%s", dest_msg_id, dest_chat, post_num)
                        except Exception as del_err:
                            logger.warning("Could not delete forwarded msg %s from %s: %s", dest_msg_id, dest_chat, del_err)

    # 4. Mark listing as sold in DB & audit log
    await db.run_async(db.mark_listing_sold, actual_id)
    await db.run_async(
        db.record_audit,
        "sold",
        actual_id,
        actor_id=event.sender_id,
        detail=f"post #{post_num} marked sold (deleted from {deleted_count} destinations)",
    )

    # 5. Admin confirmation
    done_text = texts.SOLD_DONE.format(post=post_num, deleted=deleted_count)
    if hasattr(event, "answer"):
        try:
            await event.answer("🔴 Marked as SOLD", alert=False)
        except Exception:
            pass
    try:
        await event.reply(done_text, parse_mode="markdown")
    except Exception:
        await event.client.send_message(ADMIN_USER_ID, done_text, parse_mode="markdown")


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
                texts.ACCESS_DENIED.format(user_id=event.sender_id)
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

    @bot.on(events.NewMessage(pattern=HEADERS_ROUTE_RE))
    async def handle_headers(event):
        if not await check_admin(event):
            return
        headers = db.list_headers()
        await event.reply(
            _headers_menu_text(headers),
            buttons=_headers_buttons(headers),
            parse_mode="markdown",
        )

    @bot.on(events.NewMessage(pattern=r"^(?:/pause|/resume|⏸ All Stop|▶ All Start)$"))
    async def handle_pause_toggle(event):
        if not await check_admin(event):
            return
        paused = db.is_paused()
        await db.run_async(db.set_paused, not paused)
        state_label = texts.PAUSE_ON if not paused else texts.PAUSE_OFF
        await event.reply(
            state_label,
            buttons=_home_keyboard(),
            parse_mode="markdown",
        )

    @bot.on(events.NewMessage(pattern=r"^(?:💤 I'm Asleep|☀️ I'm Awake)$"))
    async def handle_asleep_toggle(event):
        if not await check_admin(event):
            return
        asleep = db.is_buyer_asleep()
        await db.run_async(db.set_buyer_asleep, not asleep)
        state_label = texts.ASLEEP_ON if not asleep else texts.ASLEEP_OFF
        await event.reply(state_label, buttons=_home_keyboard(), parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/addsupplier(?:\s+(.+))?"))
    async def handle_add_supplier(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                texts.SOURCE_ADD_PROMPT,
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
                texts.DEST_ADD_USAGE,
                buttons=_home_keyboard(),
            )
            return
        await _run_add_destination_flow(event, arg)

    @bot.on(events.NewMessage(pattern=r"^(?:/setchannel|/setmainchannel|/mainchannel)(?:\s+(.+))?"))
    async def handle_set_main_channel(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            _wizard_state[ADMIN_USER_ID] = {"step": "set_main_channel"}
            current = get_dest_channel()
            await event.reply(
                texts.MAIN_CHANNEL_CARD.format(current=current or "(None)"),
                buttons=[[Button.inline(texts.BTN_CANCEL, data="wiz:cancel")]],
                parse_mode="markdown",
            )
            return
        await _run_set_main_channel_flow(event, arg)

    @bot.on(events.NewMessage(pattern=DESTINATIONS_ROUTE_RE))
    async def handle_destinations(event):
        if not await check_admin(event):
            return
        destinations = db.get_destination_health()
        await event.reply(
            _destinations_menu_text(destinations),
            buttons=_destinations_buttons(destinations),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/syncdestinations\b"))
    async def handle_sync_destinations(event):
        if not await check_admin(event):
            return
        cl = forward_client_ref if (forward_client_ref and forward_client_ref.is_connected()) else user_client_ref
        if not cl or not cl.is_connected():
            await event.reply(
                texts.SYNC_NO_CLIENT,
                buttons=_home_keyboard(),
            )
            return
        status_msg = await event.reply(texts.SYNC_SCANNING)
        import main as main_mod
        try:
            added = await main_mod.sync_destinations_from_forward_client(cl, bot)
            if added:
                await status_msg.edit(
                    texts.SYNC_ADDED.format(n=added),
                    buttons=_home_keyboard(),
                )
            else:
                await status_msg.edit(
                    texts.SYNC_NONE,
                    buttons=_home_keyboard(),
                )
        except Exception as exc:
            logger.exception("Error running /syncdestinations")
            await status_msg.edit(texts.SYNC_FAIL.format(error=exc), buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^/removesupplier(?:\s+(.+))?"))
    async def handle_remove_supplier(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                texts.SOURCE_REMOVE_USAGE,
                buttons=_home_keyboard(),
            )
            return

        s = db.get_supplier_by_chat(username=arg.lstrip("@"))
        if not s:
            await event.reply(texts.SOURCE_REMOVE_MISSING.format(name=arg.lstrip('@')), buttons=_home_keyboard())
            return
        await db.run_async(db.delete_supplier, s["id"])
        await db.run_async(
            db.record_audit,
            "supplier_deleted",
            None,
            actor_id=event.sender_id,
            detail=f"@{arg.lstrip('@')}",
        )
        await event.reply(texts.SOURCE_REMOVED.format(name=arg.lstrip('@')), buttons=_home_keyboard())

    @bot.on(events.NewMessage(pattern=r"^/dedupe_suppliers$"))
    async def handle_dedupe_suppliers(event):
        if not await check_admin(event):
            return
        summary = await db.run_async(db.dedupe_suppliers)
        merges = summary.get("merges", [])
        if not merges:
            await event.reply(
                texts.DEDUPE_NONE,
                buttons=_home_keyboard(),
            )
            return
        lines = [texts.DEDUPE_TITLE.format(n=summary['rows_removed'])]
        for m in merges:
            lines.append(texts.DEDUPE_LINE.format(removed=m['removed'], into=m['merged_into']))
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
        preview = ", ".join(f"`{c}`" for c in channels) if channels else texts.RESEED_NONE
        await event.reply(
            texts.RESEED_ASK.format(preview=preview),
            buttons=[
                [Button.inline(texts.BTN_RESEED_YES, data="reseed:yes")],
                [Button.inline(texts.BTN_CANCEL_X, data="wiz:cancel")],
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
            await event.reply(texts.SKIPPED_EMPTY, buttons=_home_keyboard())
            return

        text, buttons = _skipped_digest(skips, 0, db.count_skipped_listings())
        await event.reply(text, buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^(?:/published|✅ Published)"))
    async def handle_published(event):
        if not await check_admin(event):
            return
        rows = db.get_published_listings(limit=_PAGE_SIZE, offset=0)
        if not rows:
            await event.reply(texts.PUBLISHED_EMPTY, buttons=_home_keyboard())
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
                texts.POST_USAGE,
                buttons=_home_keyboard(),
            )
            return
        post = db.get_post_by_number(int(arg))
        if not post:
            await event.reply(
                texts.POST_NOT_FOUND.format(text=arg),
                buttons=_home_keyboard(),
            )
            return
        text, buttons = _post_card(post)
        await event.reply(text, buttons=buttons, parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/sold(?:[ \t]+(\d+))?"))
    async def handle_sold(event):
        if not await check_admin(event):
            return
        arg = event.pattern_match.group(1)
        if not arg:
            await event.reply(
                texts.SOLD_USAGE,
                buttons=_home_keyboard(),
            )
            return
        await _execute_sold(event, post_number=int(arg))

    @bot.on(events.NewMessage(pattern=r"^(?:/health|🩺 Health)"))
    async def handle_health(event):
        if not await check_admin(event):
            return
        user_ok = "🟢 Connected" if (user_client_ref and user_client_ref.is_connected()) else "🔴 Disconnected"
        if forward_client_ref:
            fwd_ok = "🟢 Connected" if forward_client_ref.is_connected() else "🔴 Disconnected"
        else:
            fwd_ok = "⚪ Shared / Not separate"
        bot_ok = "🟢 Connected" if bot.is_connected() else "🔴 Disconnected"
        active_srcs = len(db.list_suppliers(active_only=True))
        active_dests = len(db.list_destinations(active_only=True))
        last_inbound_raw = db.get_setting("last_inbound_at", None)
        last_inbound_disp = _relative_time(last_inbound_raw) if last_inbound_raw else "None recorded"

        report = texts.HEALTH_STATUS.format(
            user_status=user_ok,
            fwd_status=fwd_ok,
            bot_status=bot_ok,
            sources=active_srcs,
            dests=active_dests,
            last_msg=last_inbound_disp,
        )
        await event.reply(report, buttons=_home_keyboard(), parse_mode="markdown")

    @bot.on(events.NewMessage(pattern=r"^/preview(?:[ \t]+(\d+))?"))
    async def handle_preview(event):
        if not await check_admin(event):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            await event.reply(
                texts.PREVIEW_USAGE,
                buttons=_home_keyboard(),
            )
            return

        listing = db.get_listing_by_id(int(arg))
        if not listing:
            await event.reply(texts.NOT_FOUND_LISTING, buttons=_home_keyboard())
            return

        try:
            preview_text = await _build_preview_text(listing)
            await event.reply(
                texts.PREVIEW.format(id=arg, text=preview_text),
                buttons=_listing_action_buttons(listing),
            )
        except Exception as exc:
            logger.warning("Preview too long to send for listing #%s: %s", arg, exc)
            await event.reply(
                texts.PREVIEW_TOO_LONG.format(id=arg),
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
            await event.answer(texts.UNAUTHORIZED, alert=True)
            return

        data_str = event.data.decode("utf-8")

        # ---- Skipped re-review ---------------------------------------------
        if data_str.startswith("reskip:"):
            try:
                skip_id = int(data_str.split(":", 1)[1])
            except ValueError:
                await event.answer(texts.SKIP_INVALID, alert=True)
                return
            reopened = await db.run_async(db.reopen_skipped, skip_id)
            if not reopened:
                await event.answer(texts.SKIP_NOT_FOUND, alert=True)
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
            await event.answer(texts.BTN_HOME)
            await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
            return

        if data_str == "menu:sources":
            suppliers = db.list_suppliers(active_only=False)
            text = _sources_menu_text(suppliers)
            if not suppliers:
                text += texts.SOURCE_ADD_PROMPT_PLAIN
            await _message_delete_send(event, text, buttons=_sources_buttons(suppliers), parse_mode=None)
            return

        if data_str == "menu:destinations":
            destinations = db.get_destination_health()
            text = _destinations_menu_text(destinations)
            await _message_delete_send(
                event, text, buttons=_destinations_buttons(destinations), parse_mode=None
            )
            return

        # ---- Custom-emoji headers -------------------------------------------
        # "headeradd" is the ➕ row inside the /headers list. "home:headers" has
        # no button any more, but old messages still carry it and it costs
        # nothing to keep honouring them.
        if data_str in ("home:headers", "headeradd"):
            await event.answer(f"🅰 {HEADERS_BTN}")
            headers = db.list_headers()
            if data_str == "headeradd":
                _wizard_state[ADMIN_USER_ID] = {"step": "addheader"}
                await _message_delete_send(
                    event,
                    texts.HEADER_ADD_PROMPT.format(n=HEADER_EMOJI_COUNT),
                    parse_mode="markdown",
                )
                return
            await _message_delete_send(
                event, _headers_menu_text(headers), buttons=_headers_buttons(headers)
            )
            # Echo each header as real custom emoji so the admin can see it renders.
            for header in headers:
                text, entities = parser.build_header_line(
                    [(p["alt"], p["doc_id"]) for p in header["emoji"]]
                )
                try:
                    await event.client.send_message(
                        event.chat_id, text, formatting_entities=entities
                    )
                except Exception:
                    logger.exception("Could not echo header #%s", header["id"])
            return

        m = re.match(r"^delheader:(\d+)$", data_str)
        if m:
            header_id = int(m.group(1))
            header = db.get_header(header_id)
            if header is None:
                await event.answer(texts.HEADER_GONE, alert=True)
            else:
                await db.run_async(db.delete_header, header_id)
                logger.info("Deleted header #%s", header_id)
                await event.answer(texts.HEADER_DELETED_ANSWER.format(id=header_id))
                await db.run_async(
                    db.record_audit,
                    "header_deleted",
                    None,
                    actor_id=event.sender_id,
                    detail=f"header #{header_id}",
                )
            headers = db.list_headers()
            await _message_delete_send(
                event,
                texts.HEADER_DELETED.format(id=header_id) + _headers_menu_text(headers),
                buttons=_headers_buttons(headers),
                parse_mode="markdown",
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
                    texts.PAUSE_ANSWER_ON if not paused else texts.PAUSE_ANSWER_OFF
                )
                await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
                return
            if home_action == "asleep":
                asleep = db.is_buyer_asleep()
                await db.run_async(db.set_buyer_asleep, not asleep)
                await event.answer(
                    texts.ASLEEP_ANSWER_ON if not asleep else texts.ASLEEP_ANSWER_OFF
                )
                await _message_delete_send(event, _home_text(), buttons=_home_inline_keyboard())
                return
            if home_action == "skipped":
                skips = db.get_skipped_listings(limit=_PAGE_SIZE, offset=0)
                if not skips:
                    await _message_delete_send(
                        event,
                        texts.SKIPPED_EMPTY,
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
                        texts.PUBLISHED_EMPTY,
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
                    texts.SEARCH_PROMPT,
                    buttons=[Button.inline(texts.BTN_CANCEL, data="wiz:cancel")],
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
            await event.answer(texts.CANCELLED)
            try:
                await event.delete()
            except Exception:
                pass
            if edit_cancelled:
                try:
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        texts.EDIT_CANCELLED,
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
            destinations = db.get_destination_health()
            start, _, _, _, _ = _page_window(len(destinations), raw_page, page_size=_DEST_PAGE_SIZE)
            page = start // _DEST_PAGE_SIZE
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
                await event.answer(texts.SOURCE_NOT_FOUND, alert=True)
                return
            handle = _pretty_source(s.get("channel_username"), s.get("display_name"))
            await _message_delete_send(
                event,
                texts.SOURCE_DELETE_ASK.format(name=handle),
                buttons=[
                    [Button.inline(texts.BTN_DELETE_YES, data=f"del:yes:{sid}")],
                    [Button.inline(texts.BTN_CANCEL_X, data="wiz:cancel")],
                ],
            )
            return

        del_match = re.match(r"^del:yes:(\d+)$", data_str)
        if del_match:
            sid = int(del_match.group(1))
            s = db.get_supplier_by_id(sid)
            if not s:
                await event.answer(texts.SOURCE_NOT_FOUND, alert=True)
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
                    texts.SOURCE_DELETED.format(name=_pretty_source(s.get('channel_username'), s.get('display_name'))),
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
            d = db.get_destination_by_id(did, with_health=True)
            if not d:
                await event.answer(texts.DEST_NOT_FOUND, alert=True)
                return
            await _edit_destination_menu(event, d)
            return

        desttoggle_match = re.match(r"^desttoggle:(\d+)$", data_str)
        if desttoggle_match:
            did = int(desttoggle_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer(texts.DEST_NOT_FOUND, alert=True)
                return
            await db.run_async(db.set_destination_active, did, not d["active"])
            await db.run_async(
                db.record_audit,
                "destination_toggle",
                None,
                actor_id=ADMIN_USER_ID,
                detail=str(did),
            )
            refreshed = db.get_destination_by_id(did, with_health=True)
            if refreshed:
                await _edit_destination_menu(event, refreshed)
            return

        destdel_match = re.match(r"^destdel:(\d+)$", data_str)
        if destdel_match:
            did = int(destdel_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer(texts.DEST_NOT_FOUND, alert=True)
                return
            await _message_delete_send(
                event,
                texts.DEST_DELETE_ASK.format(name=_destination_markup(_destination_label(d), d.get('chat_id'))),
                buttons=[
                    [Button.inline(texts.BTN_DELETE_YES, data=f"destdelyes:{did}")],
                    [Button.inline(texts.BTN_CANCEL_X, data="wiz:cancel")],
                ],
            )
            return

        destdelyes_match = re.match(r"^destdelyes:(\d+)$", data_str)
        if destdelyes_match:
            did = int(destdelyes_match.group(1))
            d = db.get_destination_by_id(did)
            if not d:
                await event.answer(texts.DEST_NOT_FOUND, alert=True)
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
                    texts.DEST_DELETE_PERM.format(name=_destination_markup(_destination_label(d), d.get('chat_id'))),
                    parse_mode="markdown",
                )
            except Exception:
                pass
            destinations = db.get_destination_health()
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
                await event.answer(texts.DEST_NOTHING, alert=True)
                return
            try:
                did = await db.run_async(db.add_destination, raw, raw, True)
            except ValueError as exc:
                await _message_delete_send(
                    event,
                    texts.DEST_ADD_ERROR.format(error=exc),
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
                texts.DEST_ADDED_UNVERIFIED.format(name=_destination_markup(raw)),
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
                        texts.RESEED_DONE.format(n=n),
                        buttons=_home_keyboard(),
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("reseed_from_env failed")
                await event.answer(texts.RESEED_FAIL, alert=True)
            return

        if data_str == "supaddunresolved":
            state = _wizard_state.get(ADMIN_USER_ID) or {}
            raw = state.get("raw") if state.get("step") == "add_confirm_unresolved" else None
            if not raw:
                await event.answer(texts.SOURCE_EXPIRED, alert=True)
                return
            _wizard_state.pop(ADMIN_USER_ID, None)
            username, _ = _supplier_ref_from_text(raw)
            if not username:
                await event.answer(texts.INVALID_REF, alert=True)
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
                    texts.SOURCE_STORED_UNRESOLVED.format(name=username),
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
                texts.SOURCE_ADD_PROMPT,
                buttons=[Button.inline(texts.BTN_CANCEL, data="wiz:cancel")],
                parse_mode="markdown",
            )
            return

        if data_str == "destadd":
            _wizard_state[ADMIN_USER_ID] = {"step": "adddest"}
            await _message_delete_send(
                event,
                texts.DEST_ADD_PROMPT,
                buttons=[Button.inline(texts.BTN_CANCEL, data="wiz:cancel")],
                parse_mode="markdown",
            )
            return

        if data_str == "mainchan:view":
            current = get_dest_channel()
            text = texts.MAIN_CHANNEL_CARD.format(current=current or "(None)")
            buttons = [
                [Button.inline("✏️ Change Main Channel", data="mainchan:edit")],
                [Button.inline(texts.BTN_BACK, data="menu:destinations")],
            ]
            await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
            return

        if data_str == "mainchan:edit":
            _wizard_state[ADMIN_USER_ID] = {"step": "set_main_channel"}
            text = texts.MAIN_CHANNEL_PROMPT
            buttons = [[Button.inline(texts.BTN_CANCEL, data="wiz:cancel")]]
            await _message_delete_send(event, text, buttons=buttons, parse_mode="markdown")
            return

        # ---- Previews / listing actions ------------------------------------
        preview_match = re.match(r"^preview:(\d+)$", data_str)
        if preview_match:
            listing = db.get_listing_by_id(int(preview_match.group(1)))
            if not listing:
                await event.answer(texts.NOT_FOUND_LISTING, alert=True)
                return
            listing_id = listing["id"]
            # Answer the button IMMEDIATELY so Telegram doesn't consider the
            # callback expired (which made the button appear dead).
            await event.answer(texts.PREVIEW_BUILDING.format(id=listing_id))
            try:
                preview_text = await _build_preview_text(listing)
            except Exception:
                logger.exception("Failed to build preview for listing #%s", listing_id)
                await event.answer(texts.PREVIEW_FAIL, alert=True)
                return
            # Send the replacement preview FIRST so the original prompt card is
            # never removed without a confirmed replacement in its place.
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    texts.PREVIEW_FULL.format(
                        id=listing_id,
                        source=_pretty_source(listing.get('supplier_username'), listing.get('supplier_display_name')),
                        status=listing['status'],
                        text=preview_text,
                    ),
                    buttons=_listing_action_buttons(listing),
                    parse_mode="markdown",
                )
            except Exception:
                logger.exception("Failed to send preview message for listing #%s", listing_id)
                await event.answer(texts.PREVIEW_SEND_FAIL, alert=True)
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
                await event.answer(texts.NOT_FOUND_LISTING, alert=True)
                return
            if not listing_is_editable(listing["status"]):
                await event.answer(texts.EDIT_CANT.format(status=listing['status']), alert=True)
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
                    buttons=[Button.inline(texts.BTN_CANCEL, data="wiz:cancel")],
                    parse_mode="markdown",
                )
                prompt_id = getattr(sent, "id", None)
                if prompt_id:
                    _wizard_state[ADMIN_USER_ID]["prompt_message_id"] = prompt_id
            except Exception:
                logger.exception("Failed to send edit prompt for listing #%s", listing_id)
            return

        sold_match = re.match(r"^sold:(\d+)$", data_str)
        if sold_match:
            lid = int(sold_match.group(1))
            await _execute_sold(event, listing_id=lid)
            return

        if data_str.startswith("sold_info:"):
            await event.answer("This post is already marked as SOLD.", alert=True)
            return

        match = re.match(r"^(approve|skip):(\d+)$", data_str)
        if not match:
            await event.answer(texts.UNKNOWN_ACTION, alert=True)
            return

        action, listing_id_str = match.groups()
        listing_id = int(listing_id_str)
        listing = db.get_listing_by_id(listing_id)

        if not listing:
            await event.answer(texts.NOT_FOUND_LISTING, alert=True)
            return

        # Approve / Skip only valid on pending listings (never on already-approved,
        # preventing double-publish races on re-taps).
        if not listing_is_editable(listing["status"]):
            await event.answer(
                texts.ALREADY_DONE.format(status=listing['status']), alert=True
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
            await event.answer(texts.SKIPPED_ANSWER)
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.client.send_message(
                    ADMIN_USER_ID,
                    texts.SKIPPED_MSG.format(id=listing_id),
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
                    texts.APPROVE_STALE_ALERT.format(id=listing_id, status=fresh['status']),
                    alert=True,
                )
                await _message_delete_send(
                    event,
                    texts.APPROVE_STALE.format(id=listing_id, status=fresh['status']),
                    buttons=_home_keyboard(),
                )
                await _advance_review(bot)
                return

            # A draft (from ✏️ Edit) replaces the source content.
            draft = _drafts.pop(listing_id, None)
            content_text = draft or listing.get("clean_text") or listing.get("raw_text") or ""
            content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
            platform_name = listing.get("platform_name") or listing.get("game_name")

            await event.answer(texts.APPROVE_PROCESSING)  # Processing...
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
                header_emoji=await _header_emoji_for_listing(listing_id),
                post_number=post_number,
                source_text=listing.get("raw_text"),
            )

            if user_client_ref and user_client_ref.is_connected() and DEST_CHANNEL:
                # DEDUP-RECHECK: last line of defence before an external send.
                # Dedup only ever ran at ingest, so a listing that sat in review
                # for hours (or was reopened from /skipped) would publish with no
                # duplicate check at all — and two identical rows both awaiting
                # the admin could each win their own claim and both ship. Runs
                # BEFORE claim_listing_for_publish so a blocked approve does not
                # consume the claim. Fails closed.
                unique_ok, twin_id = await db.run_async(
                    db.assert_still_unique, listing_id, DEDUP_HOURS
                )
                if not unique_ok:
                    await db.run_async(db.mark_duplicate_of, listing_id, twin_id)
                    await db.run_async(
                        db.record_audit,
                        "duplicate_at_publish",
                        listing_id,
                        actor_id=event.sender_id,
                        detail=f"already published as #{twin_id}",
                    )
                    await event.client.send_message(
                        ADMIN_USER_ID,
                        texts.APPROVE_DUP.format(id=listing_id, twin=twin_id),
                        parse_mode="markdown",
                        buttons=_home_keyboard(),
                    )
                    await _advance_review(bot)
                    return
                # CONC-3: share the same lock + rate-limit as the worker and the
                # auto-publish path so admin-approve and worker never interleave.
                #
                # F3: atomic claim BEFORE the external send. Only the single
                # winner may publish; a second Approve tap, a second worker, or a
                # crash-retry sees status 'publishing' and must not send again.
                #
                # The claim is ALSO the authoritative final dedup gate, so it can
                # return False because a twin won the race between the pre-check
                # above and here. Distinguish that from a plain double-tap.
                claimed = await db.run_async(
                    db.claim_listing_for_publish,
                    listing_id,
                    post_number,
                    DEDUP_HOURS,
                )
                if not claimed:
                    race_twin = await db.run_async(
                        db.get_earlier_duplicate_twin, listing_id, DEDUP_HOURS
                    )
                    if race_twin is not None:
                        await db.run_async(
                            db.mark_duplicate_of, listing_id, race_twin
                        )
                        await db.run_async(
                            db.record_audit,
                            "duplicate_at_publish",
                            listing_id,
                            actor_id=event.sender_id,
                            detail=f"lost publish claim race to #{race_twin}",
                        )
                        try:
                            await event.client.send_message(
                                ADMIN_USER_ID,
                                texts.APPROVE_RACE.format(id=listing_id, twin=race_twin),
                                parse_mode="markdown",
                                buttons=_home_keyboard(),
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await event.client.send_message(
                                ADMIN_USER_ID,
                                texts.APPROVE_BUSY.format(id=listing_id),
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
                                texts.APPROVE_SEND_FAIL.format(id=listing_id, error=e),
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
                    confirmation = texts.APPROVE_DONE.format(id=listing_id, post=post_number)
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
                        texts.APPROVE_QUEUED.format(id=listing_id),
                        parse_mode="markdown",
                    )
                except Exception:
                    pass
                await _advance_review(bot)
                return

    @bot.on(events.NewMessage())
    async def handle_header_capture(event):
        """Capture the admin's custom emoji as a new header.

        Armed by the ➕ Add Header row in the /headers list (the "addheader" step
        name is the internal state key, not a command — there is no /addheader).

        Registered ABOVE the generic wizard fallback on purpose: Telethon calls
        EVERY matching handler in registration order rather than stopping at the
        first, so this must claim the message before handle_wizard_input can
        interpret it. Claiming = popping the wizard state, which makes the
        fallback's `if not state: return` bail out on the very same message.

        On any validation failure the state is ALSO popped: a mis-sent message
        must not leave the bot silently waiting for the next one, which the admin
        may have intended as something else entirely. Each rejection still ships
        the header-list buttons so there is always a way back or a retry.
        """
        if not await check_admin(event):
            return
        state = _wizard_state.get(ADMIN_USER_ID)
        if not state or state.get("step") != "addheader":
            return
        # Consumed regardless of the outcome — see the docstring.
        _wizard_state.pop(ADMIN_USER_ID, None)

        message = getattr(event, "message", None)
        spans = _custom_emoji_spans(message)
        # Footer for every rejection below: ➕ re-arms the capture, 🏠 gets out.
        retry_buttons = _headers_buttons(db.list_headers())
        if not spans:
            await event.reply(
                texts.HEADER_NO_EMOJI,
                buttons=retry_buttons,
                parse_mode="markdown",
            )
            return
        if len(spans) != HEADER_EMOJI_COUNT:
            await event.reply(
                texts.HEADER_WRONG_COUNT.format(got=len(spans), need=HEADER_EMOJI_COUNT),
                buttons=retry_buttons,
                parse_mode="markdown",
            )
            return
        # Any other text means this wasn't a bare header attempt (a pasted
        # sentence, a price, a link). Refuse rather than silently dropping it.
        stray = _non_emoji_residue(message, spans)
        if stray:
            await event.reply(
                texts.HEADER_STRAY.format(stray=stray, need=HEADER_EMOJI_COUNT),
                buttons=retry_buttons,
                parse_mode="markdown",
            )
            return

        emoji = [{"alt": s["alt"], "doc_id": s["doc_id"]} for s in spans]
        try:
            header_id = await db.run_async(db.add_header, emoji)
        except Exception:
            logger.exception("Failed to save a custom-emoji header")
            await event.reply(
                texts.HEADER_SAVE_FAIL,
                buttons=retry_buttons,
            )
            return

        logger.info(
            "Saved header #%s: %s", header_id, "".join(p["alt"] for p in emoji)
        )
        await db.run_async(
            db.record_audit,
            "header_added",
            None,
            actor_id=ADMIN_USER_ID,
            detail=f"header #{header_id} ({len(emoji)} emoji)",
        )
        saved = db.list_headers()
        await event.reply(
            texts.HEADER_SAVED.format(n=len(saved)),
            buttons=_headers_buttons(saved),
            parse_mode="markdown",
        )

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

        if state and state.get("step") == "set_main_channel" and fwd is not None:
            _wizard_state.pop(ADMIN_USER_ID, None)
            await _run_set_main_channel_flow(event, None, fwd=fwd)
            return

        text = event.text
        if not text:
            return
        text = text.strip()
        if text.startswith("/"):
            return
        # Menu taps must not be swallowed while a wizard is waiting.
        # HEADERS_BTN is still listed even though no button carries it any more:
        # it costs nothing, and if the label is ever re-added to a keyboard the
        # allowlist must already be covering it.
        if text in ("📊 Status", "⏳ Pending", "📋 Sources", DESTINATIONS_BTN,
                    "⏸ All Stop", "▶ All Start", "❓ Help", "✅ Published",
                    "💤 I'm Asleep", "☀️ I'm Awake", HEADERS_BTN):
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

        if state["step"] == "set_main_channel":
            await _run_set_main_channel_flow(event, text)
            return

        if state["step"] == "edit":
            listing_id = state.get("listing_id")
            listing = db.get_listing_by_id(listing_id)
            if not listing:
                await event.reply(texts.NOT_FOUND_LISTING, buttons=_home_keyboard())
                return
            if not listing_is_editable(listing["status"]):
                _drafts.pop(listing_id, None)
                await event.reply(
                    texts.EDIT_LOCKED.format(id=listing_id, status=listing['status']),
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
                    texts.EDIT_PREVIEW_FAIL,
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
                texts.DRAFT.format(id=listing_id, price_note="", text=preview_text),
                buttons=_listing_action_buttons(listing),
                parse_mode="markdown",
            )
            return

        if state["step"] == "post_search":
            if not text.isdigit():
                await event.reply(
                    texts.POST_NOT_NUMBER.format(text=text),
                    buttons=_home_keyboard(),
                    parse_mode="markdown",
                )
                return
            post = db.get_post_by_number(int(text))
            if not post:
                await event.reply(
                    texts.POST_NOT_FOUND.format(text=text),
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
            commands=[BotCommand(command=c, description=d) for c, d in BOT_MENU],
        ))
    except Exception as e:
        logger.warning("Could not set bot commands menu: %s", e)

    # Skip digest: at most once per window, one card per newly-skipped message
    # (capped at SKIP_DIGEST_MAX_CARDS); the marker persists across restarts.
    global _skip_digest_task
    _skip_digest_task = asyncio.create_task(skip_digest_worker(bot))

    logger.info("Admin bot started successfully.")
    return bot