"""S-HOME — Control Center Screen (Blueprint §4.2, R-11)."""

from typing import Any, Dict, List, Optional
from telethon import Button

import db
from services.tokens import Area, build_token
from services.screens import ScreenPayload


def get_home_reply_keyboard(db_path: Optional[str] = None) -> List[List[object]]:
    """4-button persistent reply keyboard (Blueprint §4.1, R-11)."""
    paused = db.is_paused(db_path=db_path)
    pause_label = "▶ Resume auto-publish" if paused else "⏸ Pause auto-publish"
    return [
        [Button.text("⏳ Review", resize=True), Button.text("✅ Posts", resize=True)],
        [Button.text("🏠 Home", resize=True), Button.text(pause_label, resize=True)],
    ]


def render_home_screen(db_path: Optional[str] = None) -> ScreenPayload:
    """Render the Control Center dashboard screen."""
    paused = db.is_paused(db_path=db_path)
    asleep = db.is_buyer_asleep(db_path=db_path)
    
    # Operational stats
    stats = db.get_today_stats(db_path=db_path)
    published_today = stats.get("published", 0)
    skipped_today = stats.get("skipped", 0)
    pending_count = db.count_pending_listings(db_path=db_path)
    sources_count = db.count_suppliers(db_path=db_path)
    
    # Calculate "Needs you" items
    needs_items: List[str] = []
    if pending_count > 0:
        needs_items.append(f"{pending_count} to review")
        
    try:
        with db.db_session(db_path) as conn:
            edit_rows = conn.execute(
                "SELECT COUNT(*) FROM app_settings WHERE key LIKE 'pending_edit:%'"
            ).fetchone()
            supplier_edits = edit_rows[0] if edit_rows else 0
            if supplier_edits > 0:
                needs_items.append(f"{supplier_edits} supplier edit{'s' if supplier_edits > 1 else ''}")
                
            fail_rows = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE status IN ('failed', 'publish_failed')"
            ).fetchone()
            publish_failures = fail_rows[0] if fail_rows else 0
            if publish_failures > 0:
                needs_items.append(f"{publish_failures} publish failure{'s' if publish_failures > 1 else ''}")
    except Exception:
        pass
        
    # Destination health check
    health_rows = db.get_destination_health(db_path=db_path)
    problem_groups = [
        d for d in health_rows 
        if d.get("is_dead") or d.get("is_flapping") or (d.get("consecutive_failures", 0) >= 3)
    ]
    if problem_groups:
        needs_items.append(f"{len(problem_groups)} group{'s' if len(problem_groups) > 1 else ''} unreachable")
        
    # Unresolved sources
    unresolved_sources = db.get_unresolved_suppliers(db_path=db_path)
    if unresolved_sources:
        needs_items.append(f"{len(unresolved_sources)} unresolved source{'s' if len(unresolved_sources) > 1 else ''}")
        
    # Forwarders health check
    try:
        accounts = db.list_forward_accounts(db_path=db_path)
        offline_fwd = [a for a in accounts if not a.get("is_active") or a.get("status") not in ("ready", "active")]
        if offline_fwd:
            needs_items.append(f"{len(offline_fwd)} forwarder{'s' if len(offline_fwd) > 1 else ''} offline")
    except Exception:
        pass

    # Build Header Text
    auto_pub_str = "⏸ PAUSED" if paused else "🟢 ON"
    away_str = "🌙 ON" if asleep else "☀️ OFF"
    
    lines = [
        "⚡ *Control Center*",
        f"Auto-publish: {auto_pub_str}  ·  Away mode: {away_str}",
        f"Today: {published_today} published · {pending_count} pending · {skipped_today} skipped",
    ]
    
    if needs_items:
        lines.append(f"⚠️ *Needs you:* {' · '.join(needs_items)}")
    else:
        lines.append("✅ Nothing needs you.")
        
    text = "\n".join(lines)
    
    # Buttons
    rev_btn_label = f"⏳ Review ({pending_count})" if pending_count > 0 else "⏳ Review"
    src_btn_label = f"📥 Sources ({sources_count})" if sources_count > 0 else "📥 Sources"
    dlv_badge = f" ⚠{len(problem_groups)}" if problem_groups else ""
    dlv_btn_label = f"📤 Delivery{dlv_badge}"
    skp_btn_label = f"🚫 Skipped ({skipped_today})" if skipped_today > 0 else "🚫 Skipped"
    
    pause_toggle_label = "▶ Resume auto-publish" if paused else "⏸ Pause auto-publish"
    away_toggle_label = "☀️ Turn away mode off" if asleep else "💤 Turn away mode on"
    
    buttons = [
        [
            Button.inline(rev_btn_label, data=build_token(Area.REV, "open")),
            Button.inline("✅ Posts", data=build_token(Area.POST, "list", 0)),
        ],
        [
            Button.inline(src_btn_label, data=build_token(Area.SRC, "list", 0)),
            Button.inline(dlv_btn_label, data=build_token(Area.DLV, "list", 0)),
        ],
        [
            Button.inline(skp_btn_label, data=build_token(Area.SKP, "list", "n", 0)),
            Button.inline("📊 System", data=build_token(Area.SYS, "view", "ov")),
        ],
        [
            Button.inline(pause_toggle_label, data=build_token(Area.NAV, "pause")),
            Button.inline(away_toggle_label, data=build_token(Area.NAV, "away")),
        ],
    ]
    
    return ScreenPayload(text=text, buttons=buttons, parse_mode="markdown")

