"""S-POSTS & S-POST — Published Posts List and Post Card (Blueprint §4.4, §4.5, R-07)."""

import os
from typing import Any, Dict, List, Optional
from telethon import Button

import db
from services.tokens import Area, build_token
from services.screens import ScreenPayload
from services.screens.review import _resolve_header_emoji, _resolve_source_url


_POST_PAGE_SIZE = 8


def render_posts_list(page: int = 0, db_path: Optional[str] = None) -> ScreenPayload:
    """Render the published posts list screen (S-POSTS)."""
    total = db.count_published_listings(db_path=db_path)
    if total == 0:
        text = "✅ *Published Posts*\nNo published posts yet."
        buttons = [[Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))]]
        return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

    page_count = max(1, (total + _POST_PAGE_SIZE - 1) // _POST_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    offset = page * _POST_PAGE_SIZE
    rows = db.get_published_listings(limit=_POST_PAGE_SIZE, offset=offset, db_path=db_path)

    text = f"✅ *Posts* — page {page + 1}/{page_count} · 🟢 active · ✅ filled"
    buttons: List[List[object]] = []

    for r in rows:
        num = r.get("post_number") or r["id"]
        status = r.get("status")
        icon = "✅" if status == "sold" else "🟢"
        platform = (r.get("platform_name") or r.get("game_name") or "Post").title()
        time_str = (r.get("created_at") or "")[:10]
        label = f"{icon} #{num} · {platform} · {time_str}"
        buttons.append([
            Button.inline(label, data=build_token(Area.POST, "v", num, page))
        ])

    # Action row
    buttons.append([
        Button.inline("🔍 Find #", data=build_token(Area.POST, "find"))
    ])

    # Pagination row
    nav_row: List[object] = []
    if page > 0:
        nav_row.append(Button.inline("◀ Prev", data=build_token(Area.POST, "list", page - 1)))
    if page < page_count - 1:
        nav_row.append(Button.inline("Next ▶", data=build_token(Area.POST, "list", page + 1)))
    if nav_row:
        buttons.append(nav_row)

    # Footer
    buttons.append([Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))])
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_post_card(
    post_identifier: int,
    from_page: int = 0,
    dest_channel: Optional[str] = None,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render the Post card detail screen (S-POST)."""
    listing = db.get_post_by_number(post_identifier, db_path=db_path)
    if not listing:
        listing = db.get_listing_by_id(post_identifier, db_path=db_path)

    if not listing:
        text = f"❌ *Post #{post_identifier} not found.*"
        buttons = [
            [Button.inline("◀ Back to list", data=build_token(Area.POST, "list", from_page))],
            [Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))],
        ]
        return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

    lid = listing["id"]
    post_num = listing.get("post_number") or lid
    platform = (listing.get("platform_name") or listing.get("game_name") or "Post").title()
    supplier = listing.get("supplier_username") or f"src#{listing.get('supplier_id')}"
    created_at = (listing.get("created_at") or "")[:16].replace("T", " ")
    status = listing.get("status")

    # Delivery & forwarding info
    fwds = db.get_forwardings_for_listing(lid, db_path=db_path)
    fwd_total = len(fwds)
    fwd_success = sum(1 for f in fwds if f.get("status") == "forwarded")
    fwd_failed = sum(1 for f in fwds if f.get("status") == "failed")

    status_str = "🟢 Active" if status == "published" else f"✅ Filled ({fwd_success} copies removed)"
    fwd_str = f"Forwarding: {fwd_success}/{fwd_total} delivered" if fwd_total > 0 else "Forwarding: None configured"
    if fwd_failed > 0:
        fwd_str += f" · ⚠️ {fwd_failed} failed"

    # Post content preview block
    content_text = listing.get("clean_text") or listing.get("raw_text") or ""
    text = (
        f"📋 *Post #{post_num} · {platform}*\n"
        f"Source: @{supplier.lstrip('@')} · {created_at}\n"
        f"Status: {status_str}\n"
        f"{fwd_str}\n\n"
        f"```\n{content_text[:400]}\n```"
    )

    # Buttons
    buttons: List[List[object]] = []
    link_row: List[object] = []

    # Channel link
    dest = dest_channel or db.get_dest_channel(db_path=db_path) or os.environ.get("DEST_CHANNEL", "")
    pub_msg_id = listing.get("published_message_id")
    if dest and pub_msg_id:
        clean_dest = dest.lstrip("@")
        link_row.append(Button.url("🔗 Open post", f"https://t.me/{clean_dest}/{pub_msg_id}"))

    # Source link
    source_url = _resolve_source_url(listing)
    if source_url:
        link_row.append(Button.url("🔗 Source", source_url))
    if link_row:
        buttons.append(link_row)

    # Actions row
    action_row: List[object] = []
    if status == "published":
        action_row.append(Button.inline("✅ Mark filled", data=build_token(Area.POST, "fill", post_num)))
    if fwd_failed > 0:
        action_row.append(Button.inline("🔁 Retry failed", data=build_token(Area.POST, "rtf", lid)))
    if action_row:
        buttons.append(action_row)

    # Nav footer
    buttons.append([
        Button.inline("◀ Back to list", data=build_token(Area.POST, "list", from_page)),
        Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
    ])

    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_post_fill_confirm(
    post_identifier: int,
    from_page: int = 0,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render confirmation dialog before marking a post as filled/sold (Blueprint §4.5)."""
    listing = db.get_post_by_number(post_identifier, db_path=db_path)
    if not listing:
        listing = db.get_listing_by_id(post_identifier, db_path=db_path)

    if not listing:
        return render_post_card(post_identifier, from_page=from_page, db_path=db_path)

    lid = listing["id"]
    post_num = listing.get("post_number") or lid
    platform = (listing.get("platform_name") or listing.get("game_name") or "Post").title()
    fwds = db.get_forwardings_for_listing(lid, db_path=db_path)
    fwd_count = len([f for f in fwds if f.get("status") == "forwarded"])

    text = (
        f"⚠️ *Mark Post #{post_num} as Filled / Sold?*\n\n"
        f"This will:\n"
        f"• Reply **STOP** to the post in the main channel\n"
        f"• Delete **{fwd_count}** forwarded copies across all groups\n"
        f"• Update status to **SOLD**\n\n"
        f"*This action cannot be undone.*"
    )
    buttons = [
        [Button.inline("✅ Yes, post STOP", data=build_token(Area.POST, "filly", lid))],
        [Button.inline("✖ Cancel", data=build_token(Area.POST, "v", post_num, from_page))],
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

