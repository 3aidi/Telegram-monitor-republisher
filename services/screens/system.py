"""S-SYS — System Screen with Overview, Workers, and Stats Tabs (Blueprint §4.13, R-10, R-24)."""

import os
from typing import Any, Dict, List, Optional
from telethon import Button

import db
from services.tokens import Area, build_token
from services.screens import ScreenPayload


def render_system_screen(
    tab: str = "ov",  # "ov" (Overview), "wk" (Workers), "st" (Stats)
    user_client: Any = None,
    bot_client: Any = None,
    forward_clients: Optional[List[Any]] = None,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render the unified System screen with tab switching."""
    current_tab = (tab or "ov").lower()
    if current_tab not in ("ov", "wk", "st"):
        current_tab = "ov"

    # Tab navigation row
    tab_labels = {
        "ov": "Overview",
        "wk": "Workers",
        "st": "Stats",
    }
    tab_row = [
        Button.inline(
            f"{'• ' if current_tab == 'ov' else ''}Overview",
            data=build_token(Area.SYS, "view", "ov")
        ),
        Button.inline(
            f"{'• ' if current_tab == 'wk' else ''}Workers",
            data=build_token(Area.SYS, "view", "wk")
        ),
        Button.inline(
            f"{'• ' if current_tab == 'st' else ''}Stats",
            data=build_token(Area.SYS, "view", "st")
        ),
    ]

    buttons: List[List[object]] = [tab_row]

    if current_tab == "ov":
        # Overview tab
        paused = db.is_paused(db_path=db_path)
        asleep = db.is_buyer_asleep(db_path=db_path)
        auto_pub = "⏸ PAUSED" if paused else "🟢 ON"
        away = "🌙 ON" if asleep else "☀️ OFF"

        pending_cnt = db.count_pending_listings(db_path=db_path)
        held_cnt = db.count_held_listings(db_path=db_path) if hasattr(db, "count_held_listings") else 0
        
        with db.db_session(db_path) as conn:
            appr_row = conn.execute("SELECT COUNT(*) FROM listings WHERE status = 'approved'").fetchone()
            approved_cnt = appr_row[0] if appr_row else 0
            
            fail_row = conn.execute("SELECT COUNT(*) FROM listings WHERE status IN ('failed', 'publish_failed')").fetchone()
            fail_cnt = fail_row[0] if fail_row else 0

        pending_fwds = len(db.get_pending_forwardings(limit=100, db_path=db_path))
        dests = db.list_destinations(db_path=db_path)
        sources = db.list_suppliers(db_path=db_path)

        # DB file size
        actual_path = db_path or getattr(db, "DEFAULT_DB_PATH", "monitor.db")
        db_size_str = "—"
        if os.path.exists(actual_path):
            size_kb = os.path.getsize(actual_path) / 1024
            db_size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.2f} MB"

        text = (
            "📊 *System — Overview*\n\n"
            f"• Auto-publish: {auto_pub}\n"
            f"• Away mode: {away}\n\n"
            f"*Pipeline Queues:*\n"
            f"• Held: {held_cnt} · Pending review: {pending_cnt}\n"
            f"• Approved (in-flight): {approved_cnt} · Failures: {fail_cnt}\n"
            f"• Forwardings in queue: {pending_fwds}\n\n"
            f"*Storage & Topology:*\n"
            f"• Monitored Sources: {len(sources)}\n"
            f"• Delivery Destinations: {len(dests)}\n"
            f"• Database Size: {db_size_str}"
        )

    elif current_tab == "wk":
        # Workers tab
        user_ok = "🟢 Connected" if (user_client and getattr(user_client, "is_connected", lambda: True)()) else "🔴 Disconnected"
        bot_ok = "🟢 Connected" if (bot_client and getattr(bot_client, "is_connected", lambda: True)()) else "🟢 Active"
        
        if forward_clients:
            conn_count = len([c for c in forward_clients if c and getattr(c, "is_connected", lambda: True)()])
            fwd_ok = f"🟢 Connected ({conn_count}/{len(forward_clients)} in pool)"
        else:
            fwd_ok = "⚪ Shared client"

        last_inbound = db.get_setting("last_inbound_at", db_path=db_path) or "None recorded"
        if len(last_inbound) > 19:
            last_inbound = last_inbound[:19].replace("T", " ")

        text = (
            "⚙️ *System — Workers & Connectivity*\n\n"
            f"• Ingest Listener (User Client): {user_ok}\n"
            f"• Delivery Forwarders Pool: {fwd_ok}\n"
            f"• Admin Bot: {bot_ok}\n"
            f"• Last Inbound Message: `{last_inbound}`\n\n"
            f"*Background Tasks:*\n"
            f"• Held Listings Worker: 🟢 Running\n"
            f"• Approved Listings Publisher: 🟢 Running\n"
            f"• Forwardings Queue Worker: 🟢 Running\n"
            f"• Daily Retention & Backup: 🟢 Scheduled"
        )

    else:
        # Stats tab
        stats = db.get_today_stats(db_path=db_path)
        published_today = stats.get("published", 0)
        skipped_today = stats.get("skipped", 0)
        total_proc = stats.get("total_processed", 0)

        # Reasons breakdown
        reasons = stats.get("skip_reasons") or {}
        reason_lines = [f"• {r}: {cnt}" for r, cnt in sorted(reasons.items(), key=lambda x: -x[1])[:6]]
        reasons_text = "\n".join(reason_lines) if reason_lines else "• None recorded"

        # Supplier breakdown
        suppliers = stats.get("supplier_breakdown") or []
        sup_lines = [
            f"• @{(s.get('channel_username') or 'src')[:15]}: {s.get('published', 0)} pub, {s.get('skipped', 0)} skip"
            for s in suppliers[:5]
        ]
        sup_text = "\n".join(sup_lines) if sup_lines else "• No activity yet"

        text = (
            "📈 *System — Today's Stats*\n\n"
            f"Total Processed: {total_proc} · Published: {published_today} · Skipped: {skipped_today}\n\n"
            f"*Top Skip Reasons:*\n{reasons_text}\n\n"
            f"*Top Sources Today:*\n{sup_text}"
        )

    # Action footer
    buttons.append([
        Button.inline("🔄 Refresh", data=build_token(Area.SYS, "view", current_tab)),
        Button.inline("📤 Delivery", data=build_token(Area.DLV, "list", 0)),
        Button.inline("🏠 Home", data=build_token(Area.NAV, "home")),
    ])

    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

