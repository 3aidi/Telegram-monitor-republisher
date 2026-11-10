"""S-SRC & S-SRC-V — Sources Hub & Source Card (Blueprint §4.7, R-16)."""

from typing import Any, Dict, List, Optional
from telethon import Button

import db
from services.tokens import Area, build_token
from services.screens import ScreenPayload


_SRC_PAGE_SIZE = 8


def render_sources_list(page: int = 0, db_path: Optional[str] = None) -> ScreenPayload:
    """Render the Monitored Sources list screen (S-SRC)."""
    suppliers = db.list_suppliers(db_path=db_path)
    total = len(suppliers)
    if total == 0:
        text = "📥 *Sources*\nNo sources added yet. Use the button below to add your first supplier."
        buttons = [
            [Button.inline("➕ Add sources", data=build_token(Area.SRC, "add"))],
            [Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))],
        ]
        return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

    active_count = sum(1 for s in suppliers if s.get("active"))
    paused_count = total - active_count
    unresolved = db.get_unresolved_suppliers(db_path=db_path)
    unresolved_count = len(unresolved)

    page_count = max(1, (total + _SRC_PAGE_SIZE - 1) // _SRC_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    offset = page * _SRC_PAGE_SIZE
    paged_suppliers = suppliers[offset:offset + _SRC_PAGE_SIZE]

    text = (
        f"📥 *Sources* — {total} · 🟢 {active_count} active · ⏸ {paused_count} paused · ⚠️ {unresolved_count} unresolved\n"
        f"Page {page + 1}/{page_count}:"
    )

    buttons: List[List[object]] = []
    for s in paged_suppliers:
        sid = s["id"]
        handle = s.get("channel_username") or s.get("display_name") or f"Source #{sid}"
        if not s.get("active"):
            icon = "⏸"
        elif not s.get("channel_id"):
            icon = "⚠️"
        else:
            icon = "🟢"
        label = f"{icon} @{handle.lstrip('@')[:24]}"
        buttons.append([
            Button.inline(label, data=build_token(Area.SRC, "v", sid, page))
        ])

    buttons.append([Button.inline("➕ Add sources", data=build_token(Area.SRC, "add"))])

    # Pagination row
    nav_row: List[object] = []
    if page > 0:
        nav_row.append(Button.inline("◀ Prev", data=build_token(Area.SRC, "list", page - 1)))
    if page < page_count - 1:
        nav_row.append(Button.inline("Next ▶", data=build_token(Area.SRC, "list", page + 1)))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))])
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_source_card(
    source_id: int,
    from_page: int = 0,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render detail card for a single supplier source (S-SRC-V)."""
    supplier = db.get_supplier_by_id(source_id, db_path=db_path)
    if not supplier:
        text = f"❌ *Source #{source_id} not found.*"
        buttons = [
            [Button.inline("◀ Back", data=build_token(Area.SRC, "list", from_page))],
            [Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))],
        ]
        return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

    handle = supplier.get("channel_username") or supplier.get("display_name") or f"Source #{source_id}"
    channel_id = supplier.get("channel_id") or "Unresolved"
    active = supplier.get("active", True)
    created_at = (supplier.get("created_at") or "")[:10]

    # Activity stats
    stats = db.get_stats_by_source(source_id, db_path=db_path) if hasattr(db, "get_stats_by_source") else {}
    received_count = stats.get("received", 0)
    published_count = stats.get("published", 0)
    skipped_count = stats.get("skipped", 0)

    text = (
        f"Sources › *@{''.join(handle.lstrip('@'))}*\n\n"
        f"Channel ID: `{channel_id}`\n"
        f"Status: {'🟢 Active & Monitoring' if active else '⏸ Paused'}\n"
        f"Added: {created_at}\n"
        f"Activity: {received_count} received · {published_count} published · {skipped_count} skipped"
    )

    buttons: List[List[object]] = []

    # Chat link if valid handle
    clean_handle = handle.lstrip("@")
    if clean_handle and not clean_handle.startswith("-") and not clean_handle.isdigit():
        buttons.append([Button.url("🔗 Open chat", f"https://t.me/{clean_handle}")])

    pause_label = "▶ Resume" if not active else "⏸ Pause"
    buttons.append([
        Button.inline(pause_label, data=build_token(Area.SRC, "tg", source_id)),
        Button.inline("🗑 Remove", data=build_token(Area.SRC, "rm", source_id)),
    ])

    buttons.append([
        Button.inline("◀ Back", data=build_token(Area.SRC, "list", from_page)),
        Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
    ])
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_source_remove_confirm(
    source_id: int,
    from_page: int = 0,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render confirmation dialog before removing a source."""
    supplier = db.get_supplier_by_id(source_id, db_path=db_path)
    handle = supplier.get("channel_username") or f"Source #{source_id}" if supplier else f"#{source_id}"

    text = (
        f"⚠️ *Remove @{handle.lstrip('@')}?*\n\n"
        f"Monitoring will stop immediately.\n"
        f"Historical listings and publish records will be preserved."
    )
    buttons = [
        [Button.inline(f"Yes, remove @{handle.lstrip('@')[:20]}", data=build_token(Area.SRC, "rmy", source_id))],
        [Button.inline("✖ Cancel", data=build_token(Area.SRC, "v", source_id, from_page))],
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

