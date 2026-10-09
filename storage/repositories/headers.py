"""Custom emoji headers pool repository."""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import models

from .base import DEFAULT_DB_PATH, db_session, run_async

def list_header_items(db_path: Optional[str] = None) -> List[models.HeaderItem]:
    """Fetch headers as strongly-typed HeaderItem dataclasses."""
    rows = list_headers(db_path=db_path)
    return [models.HeaderItem.from_row(r) for r in rows]

# ---------------------------------------------------------------------------
# Custom-emoji headers (admin-managed pool, schema v12).
#
# A header is one short run of custom emoji the admin supplied from the bot
# (currently three, spelling WTB). Each emoji is stored as {"alt", "doc_id"}:
# `alt` is the exact glyph Telegram wrote into the message text at the custom
# emoji entity's UTF-16 span, and `doc_id` is the document Telegram overlays on
# it. Both are persisted together so a render is a pure function of the DB row
# and never has to re-resolve an emoji over the API \u2014 the reply type of
# messages.getCustomEmojiDocuments is not in Telethon 1.44's layer-227 schema,
# so it cannot be deserialized at all (see the header capture handler).
# ---------------------------------------------------------------------------
def _row_to_header(row) -> Optional[Dict[str, Any]]:
    """Decode a `headers` row into {"id", "text", "emoji": [{"alt","doc_id"}]}."""
    if row is None:
        return None
    try:
        emoji = json.loads(row["emoji_json"])
    except (TypeError, ValueError):
        emoji = []
    if not isinstance(emoji, list):
        emoji = []
    # A header is only usable if every part survived decoding with a glyph and a
    # document id; a half-broken header would silently render as a gap.
    clean = [
        {"alt": str(e.get("alt") or ""), "doc_id": int(e.get("doc_id"))}
        for e in emoji
        if isinstance(e, dict) and e.get("alt") and e.get("doc_id")
    ]
    if not clean:
        return None
    return {"id": row["id"], "text": row["text"], "emoji": clean}

def add_header(emoji: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    """Store a new header from its [{"alt", "doc_id"}] parts and return its id.

    Ids are autoincrement, so successive calls are "header 1", "header 2", \u2026
    in the order the admin added them.
    """
    parts = [
        {"alt": str(e.get("alt")), "doc_id": int(e.get("doc_id"))}
        for e in (emoji or [])
        if isinstance(e, dict) and e.get("alt") and e.get("doc_id")
    ]
    if not parts:
        raise ValueError("add_header needs at least one {'alt','doc_id'} part")
    text = "".join(p["alt"] for p in parts)
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO headers (text, emoji_json, created_at) VALUES (?, ?, ?)",
            (text, json.dumps(parts, ensure_ascii=False), now_iso),
        )
        return int(cur.lastrowid)

def list_headers(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every configured header, oldest first (i.e. in the order they were added)."""
    with db_session(db_path) as conn:
        rows = conn.execute("SELECT * FROM headers ORDER BY id").fetchall()
    return [h for h in (_row_to_header(r) for r in rows) if h]

def get_header(header_id: Optional[int], db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One header by id, or None when it is unset or no longer decodable."""
    if header_id is None:
        return None
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM headers WHERE id = ?", (int(header_id),)).fetchone()
    return _row_to_header(row)

def count_headers(db_path: Optional[str] = None) -> int:
    """How many headers are available to pick from (0 = posts render headerless)."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM headers").fetchone()
    return int(row["n"]) if row else 0

def delete_header(header_id: int, db_path: Optional[str] = None) -> bool:
    """Delete a header. Returns True when a row was actually removed.

    Deleting only affects FUTURE posts: listings already assigned to this header
    keep their header_id, and resolve_header_for_listing reports it as gone so
    those listings simply re-pick from whatever remains.
    """
    with db_session(db_path) as conn:
        cur = conn.execute("DELETE FROM headers WHERE id = ?", (int(header_id),))
        return cur.rowcount > 0

def resolve_header_for_listing(listing_id: int, db_path: Optional[str] = None) -> Optional[int]:
    """Return this listing's header id, assigning a random one on first call.

    The pick is STICKY: it is written to listings.header_id the first time and
    every later render (auto-publish, admin preview, manual approve, edit,
    stale-claim recovery) reuses it, so the preview the admin approves is
    byte-for-byte the post that ships. Returns None when no header is
    configured, or when the listing's own header has since been deleted \u2014 in
    which case a new one is picked if any remain.
    """
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT header_id FROM listings WHERE id = ?", (int(listing_id),)
        ).fetchone()
        if row is None:
            return None
        current = row["header_id"]
        if current is not None and conn.execute(
            "SELECT 1 FROM headers WHERE id = ?", (int(current),)
        ).fetchone():
            return int(current)
        # Either never assigned, or the assigned header was deleted.
        ids = [r["id"] for r in conn.execute("SELECT id FROM headers").fetchall()]
        if not ids:
            return None
        picked = int(random.choice(ids))
        # The IS guard means a concurrent worker that already assigned a header
        # wins, so re-read rather than reporting a value we may not have written.
        conn.execute(
            "UPDATE listings SET header_id = ? WHERE id = ? AND header_id IS ?",
            (picked, int(listing_id), current),
        )
        row = conn.execute(
            "SELECT header_id FROM listings WHERE id = ?", (int(listing_id),)
        ).fetchone()
        return int(row["header_id"]) if row and row["header_id"] is not None else picked

