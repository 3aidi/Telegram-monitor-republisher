"""S-SKP — Skipped Hub Screen with Filter Chips and Snippets (Blueprint §4.6, R-14)."""

from typing import Any, Dict, List, Optional, Tuple
from telethon import Button

import db
from services.tokens import Area, build_token
from services.screens import ScreenPayload


_SKP_PAGE_SIZE = 6


def _filter_sql(filter_code: str) -> Tuple[str, tuple]:
    """Map filter code to SQL WHERE clause."""
    code = (filter_code or "n").lower()
    if code == "m":  # By me (admin skips)
        return "WHERE k.reason IN ('admin', 'skipped_admin')", ()
    if code == "x":  # Noise (duplicates & chatter)
        return "WHERE k.reason IN ('duplicate', 'chatter', 'skipped_duplicate', 'skipped_chatter')", ()
    if code == "a":  # All
        return "", ()
    # Default: Needs a look (filter drops, no content, ai rejected, etc.)
    return "WHERE k.reason NOT IN ('duplicate', 'chatter', 'skipped_duplicate', 'skipped_chatter', 'admin', 'skipped_admin')", ()


def get_filtered_skipped(
    filter_code: str = "n",
    limit: int = _SKP_PAGE_SIZE,
    offset: int = 0,
    db_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, int]]:
    """Query skipped listings with filtering and compute filter chip counts."""
    where_clause, params = _filter_sql(filter_code)
    
    query = f"""
        SELECT k.id as skip_id, k.supplier_id, k.message_id, k.reason, k.raw_text,
               k.timestamp,
               s.channel_username, s.display_name,
               l.id as listing_id, l.status as listing_status
        FROM skips k
        LEFT JOIN suppliers s ON k.supplier_id = s.id
        LEFT JOIN listings l ON l.supplier_id = k.supplier_id
                            AND l.source_message_id = k.message_id
        {where_clause}
        ORDER BY k.id DESC
        LIMIT ? OFFSET ?
    """
    count_query = f"SELECT COUNT(*) AS c FROM skips k {where_clause}"

    with db.db_session(db_path) as conn:
        rows = conn.execute(query, params + (limit, offset)).fetchall()
        items = [dict(r) for r in rows]
        total_for_filter = conn.execute(count_query, params).fetchone()["c"]

        # Counts for all 4 chips
        c_all = conn.execute("SELECT COUNT(*) AS c FROM skips").fetchone()["c"]
        c_by_me = conn.execute("SELECT COUNT(*) AS c FROM skips WHERE reason IN ('admin', 'skipped_admin')").fetchone()["c"]
        c_noise = conn.execute("SELECT COUNT(*) AS c FROM skips WHERE reason IN ('duplicate', 'chatter', 'skipped_duplicate', 'skipped_chatter')").fetchone()["c"]
        c_needs = max(0, c_all - c_by_me - c_noise)

        counts = {"n": c_needs, "m": c_by_me, "x": c_noise, "a": c_all}
        return items, total_for_filter, counts


def render_skipped_hub(
    filter_code: str = "n",
    page: int = 0,
    db_path: Optional[str] = None,
) -> ScreenPayload:
    """Render the Skipped messages screen with filter chips and re-open actions."""
    items, total, counts = get_filtered_skipped(filter_code=filter_code, limit=_SKP_PAGE_SIZE, offset=page * _SKP_PAGE_SIZE, db_path=db_path)
    page_count = max(1, (total + _SKP_PAGE_SIZE - 1) // _SKP_PAGE_SIZE) if total > 0 else 1
    page = max(0, min(page, page_count - 1))

    # Header text
    filter_names = {"n": "Needs a look", "m": "By me", "x": "Noise", "a": "All"}
    cur_filter_name = filter_names.get(filter_code, "Needs a look")
    text_lines = [
        f"🚫 *Skipped Messages* — filter: *{cur_filter_name}* ({total})",
        f"Page {page + 1}/{page_count} · Tap any row to re-open for review:\n",
    ]

    buttons: List[List[object]] = []

    # Filter chips row
    def _chip_label(code: str, name: str, count: int) -> str:
        marker = "• " if code == filter_code else ""
        return f"{marker}{name} ({count})"

    chip_row = [
        Button.inline(_chip_label("n", "🔎 Needs look", counts["n"]), data=build_token(Area.SKP, "list", "n", 0)),
        Button.inline(_chip_label("m", "🙋 By me", counts["m"]), data=build_token(Area.SKP, "list", "m", 0)),
        Button.inline(_chip_label("x", "🔇 Noise", counts["x"]), data=build_token(Area.SKP, "list", "x", 0)),
        Button.inline(_chip_label("a", "All", counts["a"]), data=build_token(Area.SKP, "list", "a", 0)),
    ]
    buttons.append(chip_row)

    # Skipped rows
    if not items:
        text_lines.append("_(Nothing here for this filter.)_")
    else:
        for it in items:
            sid = it["skip_id"]
            reason = (it.get("reason") or "skip").replace("skipped_", "")
            supplier = it.get("channel_username") or f"src#{it.get('supplier_id')}"
            raw = (it.get("raw_text") or "").replace("\n", " ").strip()
            snippet = (raw[:20] + "…") if len(raw) > 20 else (raw or "empty")
            time_str = (it.get("timestamp") or "")[11:16] or "—"
            
            label = f"#{sid} · {reason} · @{supplier.lstrip('@')[:10]} · \"{snippet}\""
            buttons.append([
                Button.inline(label, data=build_token(Area.SKP, "re", sid))
            ])

    # Pagination row
    nav_row: List[object] = []
    if page > 0:
        nav_row.append(Button.inline("◀ Prev", data=build_token(Area.SKP, "list", filter_code, page - 1)))
    if page < page_count - 1:
        nav_row.append(Button.inline("Next ▶", data=build_token(Area.SKP, "list", filter_code, page + 1)))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([Button.inline("🏠 Home", data=build_token(Area.NAV, "home"))])
    return ScreenPayload(text="\n".join(text_lines), buttons=buttons, parse_mode="markdown")

