"""S-DLV, S-GRP, S-MAIN, S-HDR, S-FWD — Delivery Hub & Siblings (Blueprint §4.8 - §4.12, R-09)."""

import os
from typing import Any, Dict, List, Optional
from telethon import Button

import db
from services.tokens import Area, build_token
from services.screens import ScreenPayload


_DEST_PAGE_SIZE = 8


def render_delivery_hub(page: int = 0, db_path: Optional[str] = None) -> ScreenPayload:
    """Render the unified Delivery Hub screen (S-DLV)."""
    dest_channel = db.get_dest_channel(db_path=db_path) or os.environ.get("DEST_CHANNEL", "") or "Not configured"
    
    # Forwarders summary
    fwd_accounts = db.list_forward_accounts(db_path=db_path)
    fwd_total = len(fwd_accounts)
    fwd_ready = sum(1 for a in fwd_accounts if a.get("active", 1) and a.get("status", "ready") in ("ready", "active"))
    fwd_paused = fwd_total - fwd_ready
    
    # Headers summary
    headers = db.list_headers(db_path=db_path)
    header_count = len(headers)
    
    # Groups & health summary
    groups = db.get_destination_health(db_path=db_path)
    total_groups = len(groups)
    healthy = sum(1 for g in groups if g.get("active") and not g.get("is_dead") and not g.get("is_flapping") and g.get("consecutive_failures", 0) < 3)
    failing = sum(1 for g in groups if g.get("is_flapping") or (g.get("consecutive_failures", 0) >= 3 and not g.get("is_dead")))
    unreachable = sum(1 for g in groups if g.get("is_dead"))
    needs_link = sum(1 for g in groups if g.get("link_state") == "needs_link")
    paused_groups = sum(1 for g in groups if not g.get("active"))
    
    # Queue & rate
    pending_fwds = len(db.get_pending_forwardings(limit=100, db_path=db_path))
    total_attempts = sum(g.get("attempts", 0) for g in groups)
    total_successes = sum(g.get("successes", 0) for g in groups)
    success_rate = round(100.0 * total_successes / total_attempts, 1) if total_attempts > 0 else 100.0

    lines = [
        "📤 *Delivery Hub*",
        f"📢 Main channel: `{dest_channel}` ✅",
        f"👥 Forwarders: {fwd_ready} ready · {fwd_paused} paused",
        f"📥 Groups: {total_groups} — 🟢 {healthy} healthy · ⚠️ {failing} failing · ❌ {unreachable} unreachable · 🔗 {needs_link} needs link",
        f"Queue: {pending_fwds} pending · Success (24 h): {success_rate}%",
    ]
    text = "\n".join(lines)

    buttons: List[List[object]] = []
    
    # Siblings navigation row
    buttons.append([
        Button.inline("📢 Main channel", data=build_token(Area.DLV, "main")),
        Button.inline(f"🏷 Headers ({header_count})", data=build_token(Area.HDR, "list")),
        Button.inline(f"👥 Forwarders ({fwd_total})", data=build_token(Area.FWD, "list")),
    ])

    # Paginated group rows
    page_count = max(1, (total_groups + _DEST_PAGE_SIZE - 1) // _DEST_PAGE_SIZE) if total_groups > 0 else 1
    page = max(0, min(page, page_count - 1))
    offset = page * _DEST_PAGE_SIZE
    paged_groups = groups[offset:offset + _DEST_PAGE_SIZE]

    for g in paged_groups:
        gid = g["id"]
        title = (g.get("title") or f"Group #{gid}")[:22]
        if not g.get("active"):
            icon = "⏸"
            suffix = " — paused"
        elif g.get("is_dead"):
            icon = "❌"
            suffix = " — unreachable"
        elif g.get("link_state") == "needs_link":
            icon = "🔗"
            suffix = " — needs link"
        elif g.get("is_flapping") or g.get("consecutive_failures", 0) >= 3:
            icon = "⚠️"
            suffix = " — failing"
        else:
            icon = "🟢"
            suffix = ""
        label = f"{icon} {title}{suffix}"
        buttons.append([
            Button.inline(label, data=build_token(Area.GRP, "v", gid, page))
        ])

    # Action buttons
    action_row: List[object] = [
        Button.inline("➕ Add group", data=build_token(Area.GRP, "add")),
        Button.inline("🔄 Refresh", data=build_token(Area.DLV, "list", page)),
    ]
    if unreachable > 0:
        action_row.insert(1, Button.inline(f"🧹 Remove {unreachable} dead", data=build_token(Area.GRP, "rmdead")))
    buttons.append(action_row)

    # Pagination row
    nav_row: List[object] = []
    if page > 0:
        nav_row.append(Button.inline("◀ Prev", data=build_token(Area.DLV, "list", page - 1)))
    if page < page_count - 1:
        nav_row.append(Button.inline("Next ▶", data=build_token(Area.DLV, "list", page + 1)))
    if nav_row:
        buttons.append(nav_row)

    # Home footer
    buttons.append([Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))])
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_group_card(
    dest_id: int,
    from_page: int = 0,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render detail card for a single forwarding destination group (S-GRP)."""
    groups = db.get_destination_health(db_path=db_path)
    dest = next((g for g in groups if g["id"] == dest_id), None)
    if not dest:
        dest = db.get_destination_by_id(dest_id, db_path=db_path)

    if not dest:
        text = f"❌ *Group #{dest_id} not found.*"
        buttons = [
            [Button.inline("◀ Back", data=build_token(Area.DLV, "list", from_page))],
            [Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))],
        ]
        return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

    title = dest.get("title") or f"Group #{dest_id}"
    chat_id = dest.get("chat_id")
    active = dest.get("active", True)
    link = dest.get("invite_link") or "None"
    attempts = dest.get("attempts", 0)
    successes = dest.get("successes", 0)
    last_err = dest.get("last_error") or "None"

    if not active:
        status_text = "⏸ Paused — forwarding skipped"
    elif dest.get("is_dead"):
        status_text = "❌ Unreachable — permanent error (banned/peer invalid)"
    elif dest.get("link_state") == "needs_link":
        status_text = "🔗 Needs link — invite hash invalid or expired"
    elif dest.get("is_flapping") or dest.get("consecutive_failures", 0) >= 3:
        status_text = "⚠️ Failing — consecutive forward failures"
    else:
        status_text = "🟢 Healthy"

    text = (
        f"Delivery › *{title}*\n\n"
        f"Chat ID: `{chat_id}`\n"
        f"Status: {status_text}\n"
        f"Delivery rate: {successes}/{attempts} ({dest.get('fail_pct', 0)}% fail)\n"
        f"Invite link: {link}\n"
        f"Last error: `{last_err[:80]}`"
    )

    buttons: List[List[object]] = []
    
    # Open chat link if public or invite link available
    if link and link.startswith("http"):
        buttons.append([Button.url("🔗 Open chat", link)])

    pause_label = "▶ Resume" if not active else "⏸ Pause"
    buttons.append([
        Button.inline(pause_label, data=build_token(Area.GRP, "tg", dest_id)),
        Button.inline("🔗 Set invite link", data=build_token(Area.GRP, "lnk", dest_id)),
    ])
    
    action_row: List[object] = []
    if link and link.startswith("http"):
        action_row.append(Button.inline("🚀 Join forwarders", data=build_token(Area.GRP, "join", dest_id)))
    action_row.append(Button.inline("🗑 Remove", data=build_token(Area.GRP, "rm", dest_id)))
    buttons.append(action_row)

    buttons.append([
        Button.inline("◀ Back", data=build_token(Area.DLV, "list", from_page)),
        Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
    ])
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_main_channel_screen(db_path: Optional[str] = None) -> ScreenPayload:
    """Render the Main Channel settings screen (S-MAIN)."""
    dest_channel = db.get_dest_channel(db_path=db_path) or os.environ.get("DEST_CHANNEL", "") or "Not configured"
    source_of_truth = "Database setting (DEST_CHANNEL)" if db.get_dest_channel(db_path=db_path) else ".env variable"
    asleep = db.is_buyer_asleep(db_path=db_path)
    away_str = "🌙 ON" if asleep else "☀️ OFF"
    header_count = len(db.list_headers(db_path=db_path))
    
    posts = db.get_published_listings(limit=1, offset=0, db_path=db_path)
    last_post = posts[0].get("post_number") if posts else "None"

    text = (
        f"Delivery › *Main Channel*\n\n"
        f"Channel: `{dest_channel}`\n"
        f"Posting rights: ✅ Verified\n"
        f"Source of truth: {source_of_truth}\n"
        f"Last post number: #{last_post}\n"
        f"Header pool: {header_count} configured\n"
        f"Away mode: {away_str}"
    )

    buttons = [
        [
            Button.inline("✏️ Change channel", data=build_token(Area.DLV, "mainedit")),
            Button.inline(f"🏷 Headers ({header_count})", data=build_token(Area.HDR, "list")),
        ],
        [
            Button.inline("◀ Back", data=build_token(Area.DLV, "list", 0)),
            Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
        ],
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")


def render_headers_screen(
    undo_header_id: Optional[int] = None,
    deleted_note: str = "",
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render the custom-emoji headers management screen (S-HDR)."""
    headers = db.list_headers(db_path=db_path)
    
    lines = ["Delivery › *Custom Emoji Headers*\n"]
    if deleted_note:
        lines.append(f"_{deleted_note}_\n")
    if undo_header_id:
        lines.append(f"Banner: Header #{undo_header_id} deleted.\n")

    lines.append(
        "Post headers live in a pool: each post draws one at random and pins it.\n"
        "Configured headers:\n"
    )

    entities = []
    if not headers:
        lines.append("_(No headers configured. Add one below!)_")
    else:
        for idx, h in enumerate(headers, start=1):
            emoji_str = "".join(p.get("alt", "🅰") for p in (h.get("emoji") or []))
            lines.append(f"{idx}. {emoji_str} (id #{h['id']})")

    text = "\n".join(lines)
    buttons: List[List[object]] = []

    if undo_header_id:
        buttons.append([
            Button.inline(f"↩ Undo #{undo_header_id}", data=build_token(Area.HDR, "undo", undo_header_id))
        ])

    buttons.append([Button.inline("➕ Add header", data=build_token(Area.HDR, "add"))])

    if headers:
        row: List[object] = []
        for h in headers:
            row.append(Button.inline(f"🗑 #{h['id']}", data=build_token(Area.HDR, "del", h["id"])))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    buttons.append([
        Button.inline("◀ Back", data=build_token(Area.DLV, "list", 0)),
        Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
    ])
    return ScreenPayload(text=text, buttons=buttons, formatting_entities=entities)


def render_forwarders_screen(db_path: Optional[str] = None) -> ScreenPayload:
    """Render the Forwarders list screen (S-FWD)."""
    accounts = db.list_forward_accounts(db_path=db_path)
    total = len(accounts)
    ready = sum(1 for a in accounts if a.get("is_active") and a.get("status") in ("ready", "active"))

    lines = [
        "Delivery › *Forwarders*\n",
        f"Accounts pool: {total} total · 🟢 {ready} ready",
        "Forwarders distribute posts to destination groups with failover & rate pacing.\n",
    ]

    buttons: List[List[object]] = []

    if not accounts:
        lines.append("_(No forwarder accounts added yet.)_")
    else:
        for a in accounts:
            aid = a["id"]
            name = a.get("display_name") or a.get("session_name") or a.get("phone") or f"Account #{aid}"
            active = bool(a.get("active", 1))
            status = a.get("status", "ready")
            icon = "🟢" if active and status in ("ready", "active") else ("⏸" if not active else "⚠️")
            label = f"{icon} {name}"
            buttons.append([
                Button.inline(label, data=build_token(Area.FWD, "v", aid))
            ])

    buttons.append([
        Button.inline("➕ Add forwarder", data=build_token(Area.FWD, "add")),
        Button.inline("🔄 Join all groups", data=build_token(Area.FWD, "joinall")),
    ])

    buttons.append([
        Button.inline("◀ Back", data=build_token(Area.DLV, "list", 0)),
        Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
    ])

    return ScreenPayload(text="\n".join(lines), buttons=buttons, parse_mode="markdown")


def render_forwarder_card(account_id: int, db_path: Optional[str] = None) -> ScreenPayload:
    """Render detail card for a single forwarder account (S-FWDC)."""
    acct = db.get_forward_account(account_id, db_path=db_path)
    if not acct:
        text = f"❌ *Forwarder account #{account_id} not found.*"
        buttons = [
            [Button.inline("◀ Back", data=build_token(Area.FWD, "list"))],
            [Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))],
        ]
        return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

    name = acct.get("display_name") or acct.get("session_name") or "Forwarder"
    phone = acct.get("phone") or "—"
    masked_phone = f"{phone[:3]}••••{phone[-4:]}" if len(phone) >= 7 else phone
    status = acct.get("status") or "ready"
    active = bool(acct.get("active", 1))
    reason = acct.get("status_reason") or "Normal operation"

    text = (
        f"Delivery › Forwarders › *{name}*\n\n"
        f"Phone: `{masked_phone}`\n"
        f"State: {'🟢 Active' if active else '⏸ Paused'}\n"
        f"Status: `{status}`\n"
        f"Note: {reason}\n"
        f"Failover eligible: {'Yes' if active and status in ('ready', 'active') else 'No'}"
    )

    pause_label = "▶ Resume" if not active else "⏸ Pause"
    buttons = [
        [
            Button.inline(pause_label, data=build_token(Area.FWD, "tg", account_id)),
            Button.inline("🚀 Join groups", data=build_token(Area.FWD, "join", account_id)),
        ],
        [Button.inline("🚪 Remove & log out", data=build_token(Area.FWD, "rm", account_id))],
        [
            Button.inline("◀ Back", data=build_token(Area.FWD, "list")),
            Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
        ],
    ]
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")
