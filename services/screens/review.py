"""S-REV — Review Card Screen & Workflow (Blueprint §4.3, R-06)."""

import os
from typing import Any, Dict, List, Optional, Tuple
from telethon import Button

import db
import parser
from services.tokens import Area, build_token
from services.screens import ScreenPayload


def _resolve_header_emoji(listing_id: int, db_path: Optional[str] = None) -> list:
    """Resolve and pin header emoji for listing."""
    try:
        header_id = db.resolve_header_for_listing(listing_id, db_path=db_path)
        if header_id is None:
            return []
        h = db.get_header(header_id, db_path=db_path)
        if not h:
            return []
        return [(p["alt"], p["doc_id"]) for p in (h.get("emoji") or [])]
    except Exception:
        return []


def _resolve_source_url(listing: dict) -> Optional[str]:
    """Extract public t.me source URL if resolvable."""
    url = listing.get("source_url") or listing.get("url")
    if url and url.startswith("http"):
        return url
    username = listing.get("supplier_username") or ""
    msg_id = listing.get("source_message_id")
    if username and msg_id:
        clean_user = username.lstrip("@")
        return f"https://t.me/{clean_user}/{msg_id}"
    return None


def render_review_card(
    listing: dict,
    page_idx: int = 0,
    total_count: int = 1,
    toggle_mode: str = "proposed",  # "proposed" or "original"
    undo_skip_id: Optional[int] = None,
    is_standalone: bool = False,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render a pending listing as a true WYSIWYG review card."""
    lid = listing["id"]
    supplier = listing.get("supplier_username") or listing.get("supplier_display_name") or f"src#{listing.get('supplier_id')}"
    platform = (listing.get("platform_name") or listing.get("game_name") or "Unspecified").title()
    price = listing.get("price") or "DM"
    has_draft = bool(listing.get("draft_text"))
    
    # Header summary line
    edited_marker = " · ✏️ edited" if has_draft else ""
    left_str = f" · {total_count} left" if total_count > 1 else ""
    card_header = f"📬 *Manual review* · #{lid}{left_str}{edited_marker}\n@{supplier.lstrip('@')} · Platform: {platform} · Price: {price}\n"
    
    # Body rendering: WYSIWYG
    contact_username = os.environ.get("CONTACT_USERNAME", "")
    header_emoji = _resolve_header_emoji(lid, db_path=db_path)
    
    if toggle_mode == "original":
        body_content = listing.get("raw_text") or "*(No raw text)*"
        preview_block = (
            f"┌─── 📋 *Original Supplier Text* ───\n"
            f"{body_content}\n"
            f"└──────────────────────────────"
        )
        entities = None
    else:
        content_text = listing.get("draft_text") or listing.get("clean_text") or listing.get("raw_text") or ""
        content_lines = [ln.strip() for ln in content_text.split("\n") if ln.strip()]
        built_text, entities = parser.build_ai_message(
            content_lines=content_lines,
            platform=listing.get("platform_name") or listing.get("game_name"),
            contact_username=contact_username,
            header_emoji=header_emoji,
            post_number=listing.get("post_number"),
            source_text=listing.get("raw_text"),
        )
        preview_block = (
            f"┌──────────────────────────────\n"
            f"{built_text}\n"
            f"└──────────────────────────────"
        )

    full_text = f"{card_header}\n{preview_block}"
    
    # Buttons
    buttons: List[List[object]] = []
    
    # Optional Undo skip banner row
    if undo_skip_id:
        buttons.append([
            Button.inline(f"↩ Undo skip #{undo_skip_id}", data=build_token(Area.REV, "undoskip", undo_skip_id))
        ])
        
    # Primary actions row
    buttons.append([
        Button.inline("✅ Approve", data=build_token(Area.REV, "ok", lid)),
        Button.inline("⏭ Skip", data=build_token(Area.REV, "skip", lid)),
        Button.inline("✏️ Edit", data=build_token(Area.REV, "edit", lid)),
    ])
    
    # View toggles & Source row
    second_row: List[object] = []
    if toggle_mode == "original":
        second_row.append(Button.inline("🔁 Proposed", data=build_token(Area.REV, "prop", lid)))
    else:
        second_row.append(Button.inline("🔁 Original", data=build_token(Area.REV, "orig", lid)))
        
    source_url = _resolve_source_url(listing)
    if source_url:
        second_row.append(Button.url("🔗 Source", source_url))
        
    buttons.append(second_row)
    
    # Queue navigation & More row
    if is_standalone:
        if total_count > 1:
            buttons.append([
                Button.inline(f"📬 Open queue ({total_count})", data=build_token(Area.REV, "open")),
                Button.inline("⋯ More", data=build_token(Area.REV, "more", lid)),
            ])
        else:
            buttons.append([
                Button.inline("⋯ More", data=build_token(Area.REV, "more", lid)),
                Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
            ])
    else:
        prev_idx = max(0, page_idx - 1)
        next_idx = min(total_count - 1, page_idx + 1)
        nav_row = [
            Button.inline("◀", data=build_token(Area.REV, "pg", prev_idx)),
            Button.inline(f"{page_idx + 1}/{total_count}", data=build_token(Area.REV, "noop")),
            Button.inline("▶", data=build_token(Area.REV, "pg", next_idx)),
            Button.inline("⋯ More", data=build_token(Area.REV, "more", lid)),
        ]
        buttons.append(nav_row)
        
    return ScreenPayload(text=full_text, buttons=buttons, formatting_entities=entities)


def render_review_more_overlay(listing: dict, total_pending: int) -> ScreenPayload:
    """Render the More actions overlay for a review card (Blueprint §4.3)."""
    lid = listing["id"]
    supplier = listing.get("supplier_username") or listing.get("supplier_display_name") or f"src#{listing.get('supplier_id')}"
    
    text = (
        f"⚙️ *Review Actions* · #{lid}\n"
        f"Source: @{supplier.lstrip('@')}\n"
        f"Select an extended action for this listing or the review queue."
    )
    
    buttons = [
        [Button.inline(f"⏸ Pause source @{supplier.lstrip('@')[:20]}", data=build_token(Area.REV, "psrc", lid))],
        [Button.inline(f"⏭ Skip all pending ({total_pending})", data=build_token(Area.REV, "skall"))],
        [Button.inline("🏷 Set platform", data=build_token(Area.REV, "plat", lid))],
        [
            Button.inline("◀ Back to card", data=build_token(Area.REV, "card", lid)),
            Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
        ],
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_review_empty_state(db_path: Optional[str] = None) -> ScreenPayload:
    """Render the empty state when no listings are pending review."""
    skipped_count = db.count_skipped_listings(db_path=db_path)
    text = "✅ *Queue is clear.*\nThere are no pending listings awaiting manual review."
    buttons = [
        [Button.inline(f"🚫 Review skipped ({skipped_count})", data=build_token(Area.SKP, "list", "n", 0))],
        [Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))],
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_review_edit_prompt(listing: dict) -> ScreenPayload:
    """Render the edit wizard prompt for a review card."""
    lid = listing["id"]
    current_body = listing.get("draft_text") or listing.get("clean_text") or listing.get("raw_text") or ""
    text = (
        f"✏️ *Edit #{lid}*\n"
        f"Current text below (tap to copy). Send the corrected lines.\n"
        f"Price and contact are added automatically.\n\n"
        f"```\n{current_body}\n```"
    )
    buttons = [
        [Button.inline("✖ Cancel", data=build_token(Area.REV, "card", lid))]
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

