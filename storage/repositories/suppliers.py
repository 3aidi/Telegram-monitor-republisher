"""Suppliers and source channels repository."""

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

from .base import (
    DEFAULT_DB_PATH,
    db_session,
    run_async,
    is_numeric_identifier,
    normalize_channel_id,
)
from .audit import record_audit, get_setting, set_setting

def normalize_supplier_channel_ids(
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Migrate existing suppliers to the canonical marked channel-id form.

    Older builds stored entity.id (bare, e.g. 4331866910) where Telethon reports
    the marked form (-1004331866910), so suppliers added by username or forwarded
    post silently never matched incoming events. Re-marks bare ids in place and
    merges into an already-existing row when re-marking would collide, so the
    already-correct rows are never duplicated. Idempotent: safe to run on every
    startup.

    Returns {"scanned": int, "normalized": int, "merged": int}.
    """
    with db_session(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, channel_username, channel_id FROM suppliers "
                "WHERE channel_id IS NOT NULL ORDER BY id ASC"
            )
        ]

    scanned = len(rows)
    normalized = 0
    merged = 0
    for row in rows:
        raw = row["channel_id"]
        marked = normalize_channel_id(raw)
        if marked is None or marked == raw:
            continue
        keeper = get_supplier_by_chat(chat_id=marked, db_path=db_path)
        if keeper is not None and keeper["id"] != row["id"]:
            if merge_supplier_rows(keeper["id"], row["id"], db_path=db_path):
                merged += 1
            continue
        with db_session(db_path) as conn:
            conn.execute(
                "UPDATE suppliers SET channel_id = ? WHERE id = ?", (marked, row["id"])
            )
            if is_numeric_identifier(row["channel_username"]):
                conn.execute(
                    "UPDATE suppliers SET channel_username = ? WHERE id = ?",
                    (str(marked), row["id"]),
                )
        normalized += 1

    if normalized or merged:
        record_audit(
            "channel_id_normalized",
            None,
            actor_id=None,
            detail=f"re-marked {normalized} bare supplier id(s), merged {merged} row(s)",
            db_path=db_path,
        )
    return {"scanned": scanned, "normalized": normalized, "merged": merged}

def add_supplier(
    channel_username: str,
    channel_id: Optional[int] = None,
    db_path: Optional[str] = None,
    active: bool = True,
) -> int:
    """Add a supplier channel, merging onto an existing row by channel_id FIRST.

    Resolution happens BEFORE any caller reaches this function (see admin_bot);
    this is pure persistence. A supplier whose channel_id already exists on another
    row is updated/reactivated there instead of creating a second row keyed by a
    different username â€” that is what made private channels silently duplicate.

    All channel_ids are normalized to the canonical marked form (-100 prefix for
    channels) so that events always match stored suppliers regardless of how they
    were added.
    """
    username = channel_username.strip()
    if username.startswith("@"):
        username = username[1:]
    username = username.lower()
    channel_id = normalize_channel_id(channel_id)
    active_int = 1 if active else 0

    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.cursor()

        if channel_id is not None:
            existing = conn.execute(
                "SELECT * FROM suppliers WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if existing:
                existing_user = existing["channel_username"] or ""
                if username and not is_numeric_identifier(username) and username != existing_user:
                    conn.execute(
                        "UPDATE suppliers SET channel_username = ?, active = ?, added_at = ? WHERE id = ?",
                        (username, active_int, now_iso, existing["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE suppliers SET active = ?, added_at = ? WHERE id = ?",
                        (active_int, now_iso, existing["id"]),
                    )
                return existing["id"]

        if is_numeric_identifier(username) and channel_id is None:
            existing = conn.execute(
                "SELECT * FROM suppliers WHERE channel_id = ?",
                (normalize_channel_id(int(username)),),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE suppliers SET active = ?, added_at = ? WHERE id = ?",
                    (active_int, now_iso, existing["id"]),
                )
                return existing["id"]

        try:
            cursor.execute(
                """
                INSERT INTO suppliers (channel_username, channel_id, active, added_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_username) DO UPDATE SET
                    channel_id = coalesce(excluded.channel_id, suppliers.channel_id),
                    active = excluded.active,
                    added_at = excluded.added_at
                """,
                (username, channel_id, active_int, now_iso),
            )
        except sqlite3.IntegrityError:
            # channel_id already belongs to another username (e.g. a renamed
            # channel): act as an upsert on channel_id instead of failing (DB-2).
            # Prefer a REAL username over a numeric-ID string so friendly labels
            # survive even when the same channel is added once by ID and once by
            # username. Keep the existing row's username when it is already real.
            existing_row = conn.execute(
                "SELECT * FROM suppliers WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            existing_user = (existing_row["channel_username"] if existing_row else "") or ""
            if existing_user and not is_numeric_identifier(existing_user):
                username = existing_user
            conn.execute(
                """
                UPDATE suppliers
                SET channel_username = ?, active = ?, added_at = ?
                WHERE channel_id = ?
                """,
                (username.lower(), active_int, now_iso, channel_id),
            )
        row = conn.execute(
            "SELECT id FROM suppliers WHERE channel_username = ? OR channel_id = ?",
            (username.lower(), channel_id),
        ).fetchone()
        return row["id"] if row else cursor.lastrowid

def set_supplier_channel_id(
    channel_username: str, channel_id: int, db_path: Optional[str] = None
) -> bool:
    channel_id = normalize_channel_id(channel_id)
    if channel_id is None:
        return False
    username = channel_username.strip().lstrip("@").lower()
    try:
        with db_session(db_path) as conn:
            cursor = conn.execute(
                "UPDATE suppliers SET channel_id = ? WHERE channel_username = ?",
                (channel_id, username),
            )
            return cursor.rowcount > 0
    except sqlite3.IntegrityError:
        return False

def set_supplier_display_name(
    identifier: str, display_name: str, db_path: Optional[str] = None
) -> bool:
    """Store a friendly display name for a supplier, matching by channel_id or
    channel_username. Display-only: the numeric ID stays the matching key."""
    raw = str(identifier).strip()
    if raw.startswith("@"):
        raw = raw[1:]
    name = (display_name or "").strip()[:100]
    if not name:
        return False
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE suppliers SET display_name = ? "
            "WHERE channel_id = ? OR channel_username = ?",
            (name, raw, raw.lower()),
        )
        return cursor.rowcount > 0

def set_supplier_active(
    channel_username: str, active: bool, db_path: Optional[str] = None
) -> bool:
    """Activate or deactivate a supplier by username or numeric channel id."""
    username = channel_username.strip().lstrip("@").lower()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE suppliers SET active = ? WHERE channel_username = ? OR channel_id = ?",
            (1 if active else 0, username, username),
        )
        return cursor.rowcount > 0

def remove_supplier(channel_username: str, db_path: Optional[str] = None) -> bool:
    """Mark supplier as inactive."""
    return set_supplier_active(channel_username, False, db_path=db_path)

def list_suppliers(
    active_only: bool = False, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List all configured suppliers."""
    query = "SELECT * FROM suppliers"
    params: Tuple[Any, ...] = ()
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id ASC"

    with db_session(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

def get_supplier_by_chat(
    chat_id: Optional[int] = None,
    username: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find a supplier row by channel_id or channel_username."""
    chat_id = normalize_channel_id(chat_id)
    with db_session(db_path) as conn:
        if chat_id is not None:
            row = conn.execute(
                "SELECT * FROM suppliers WHERE channel_id = ?", (chat_id,)
            ).fetchone()
            if row:
                return dict(row)

        if username:
            clean_user = username.strip().lstrip("@").lower()
            if clean_user:
                row = conn.execute(
                    "SELECT * FROM suppliers WHERE channel_username = ?", (clean_user,)
                ).fetchone()
                if row:
                    return dict(row)
    return None

def get_supplier_by_id(
    supplier_id: int, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return one supplier row by its primary key."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        return dict(row) if row else None

def get_unresolved_suppliers(
    active_only: bool = True, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Suppliers still missing a resolvable channel_id.

    A row without channel_id never matches incoming messages (resolution is keyed
    on chat_id), so it is dead weight until it resolves. The periodic worker in
    main.py uses this to retry just those rows.
    """
    query = "SELECT * FROM suppliers WHERE channel_id IS NULL"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY id ASC"
    with db_session(db_path) as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]

def _move_listings_to_supplier(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Re-parent listings + skips from drop_id onto keep_id in one connection.

    Any listing whose (supplier_id, source_message_id) already exists on the keeper
    is removed first so the unique index never trips during the move.
    """
    conn.execute(
        """
        DELETE FROM listings WHERE supplier_id = ? AND EXISTS (
            SELECT 1 FROM listings k
            WHERE k.supplier_id = ? AND k.source_message_id = listings.source_message_id
        )
        """,
        (drop_id, keep_id),
    )
    conn.execute(
        "UPDATE listings SET supplier_id = ? WHERE supplier_id = ?", (keep_id, drop_id)
    )
    conn.execute(
        "UPDATE skips SET supplier_id = ? WHERE supplier_id = ?", (keep_id, drop_id)
    )

def merge_supplier_rows(
    keep_id: int, drop_id: int, db_path: Optional[str] = None
) -> bool:
    """Fold supplier row drop_id into keep_id.

    Listings/history are re-parented onto the keeper (duplicates removed), richer
    identity fields are promoted, and the drop row is hard-deleted.
    """
    if keep_id == drop_id:
        return False
    with db_session(db_path) as conn:
        drop = conn.execute(
            "SELECT * FROM suppliers WHERE id = ?", (drop_id,)
        ).fetchone()
        keep = conn.execute(
            "SELECT * FROM suppliers WHERE id = ?", (keep_id,)
        ).fetchone()
        if not drop or not keep:
            return False
        _move_listings_to_supplier(conn, keep_id, drop_id)

        # The keep row's channel_id is definitionally correct whenever it is
        # non-null â€” every call site keeps the row that OWNS the resolved id â€” so
        # it must never be overwritten from the drop row. Only a keep row with a
        # NULL channel_id adopts the drop row's id. The drop row's unique slot is
        # freed FIRST, otherwise the transfer writes a value the (still-present)
        # drop row holds and trips UNIQUE(channel_id) mid-transaction.
        if keep["channel_id"] is None:
            conn.execute(
                "UPDATE suppliers SET channel_id = NULL WHERE id = ?", (drop_id,)
            )
            conn.execute(
                "UPDATE suppliers SET channel_id = ?, "
                "display_name = COALESCE(?, display_name) WHERE id = ?",
                (drop["channel_id"], drop["display_name"], keep_id),
            )
        else:
            conn.execute(
                "UPDATE suppliers SET display_name = COALESCE(?, display_name) WHERE id = ?",
                (drop["display_name"], keep_id),
            )

        drop_user = (drop["channel_username"] or "") or ""
        keep_user = (keep["channel_username"] or "") or ""
        drop_user_lower = drop_user.lower().lstrip("@")
        conn.execute("DELETE FROM suppliers WHERE id = ?", (drop_id,))
        if (
            drop_user
            and not is_numeric_identifier(drop_user)
            and is_numeric_identifier(keep_user)
        ):
            collision = conn.execute(
                "SELECT 1 FROM suppliers WHERE channel_username = ? AND id != ?",
                (drop_user_lower, keep_id),
            ).fetchone()
            if not collision:
                conn.execute(
                    "UPDATE suppliers SET channel_username = ? WHERE id = ?",
                    (drop_user_lower, keep_id),
                )
        return True

def dedupe_suppliers(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Scan suppliers and merge rows that point at the same real channel.

    Two duplication patterns are folded together:
      1. multiple rows sharing one resolved channel_id (added by a wrong username,
         then re-added by the correct numeric id);
      2. a row whose channel_username is a numeric id string while another row
         owns that exact channel_id.

    The keeper row is the one with a real username / display name; redundant rows
    are merged (listings re-parented) and deleted. Returns a summary dict.
    """
    merges: List[Dict[str, Any]] = []
    removed_ids: List[int] = []

    with db_session(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY id ASC")]

        buckets: Dict[int, List[dict]] = {}
        for s in rows:
            cid = s.get("channel_id")
            if cid is not None:
                buckets.setdefault(cid, []).append(s)
            elif is_numeric_identifier(s.get("channel_username")):
                buckets.setdefault(int(s["channel_username"]), []).append(s)

        for cid, group in buckets.items():
            group = [s for s in group if s["id"] not in removed_ids]
            if len(group) < 2:
                continue

            def keeper_rank(s: dict):
                u = (s.get("channel_username") or "") or ""
                return (
                    0 if (not is_numeric_identifier(u) and (s.get("display_name") or "")) else
                    1 if not is_numeric_identifier(u) else
                    2 if (s.get("display_name") or "") else 3,
                    s["id"],
                )

            keeper = min(group, key=keeper_rank)
            for dup in group:
                if dup["id"] == keeper["id"]:
                    continue
                _move_listings_to_supplier(conn, keeper["id"], dup["id"])
                dup_user = (dup.get("channel_username") or "") or ""
                keeper_user = (keeper.get("channel_username") or "") or ""
                dup_user_lower = dup_user.lower().lstrip("@")
                conn.execute(
                    "UPDATE suppliers SET channel_id = COALESCE(?, channel_id), "
                    "display_name = COALESCE(?, display_name) WHERE id = ?",
                    (dup.get("channel_id"), dup.get("display_name"), keeper["id"]),
                )
                collision = conn.execute(
                    "SELECT 1 FROM suppliers WHERE channel_username = ? AND id NOT IN (?, ?)",
                    (dup_user_lower, keeper["id"], dup["id"]),
                ).fetchone()
                conn.execute("DELETE FROM suppliers WHERE id = ?", (dup["id"],))
                removed_ids.append(dup["id"])
                if (
                    dup_user
                    and not is_numeric_identifier(dup_user)
                    and dup_user_lower != keeper_user.lower().lstrip("@")
                    and not collision
                ):
                    conn.execute(
                        "UPDATE suppliers SET channel_username = ? WHERE id = ?",
                        (dup_user_lower, keeper["id"]),
                    )
                merges.append({
                    "merged_into": keeper["id"],
                    "removed": dup["id"],
                    "duplicate_username": dup_user,
                    "channel_id": cid,
                })

    return {"merges": merges, "rows_removed": len(removed_ids)}

def delete_supplier(supplier_id: int, db_path: Optional[str] = None) -> bool:
    """Hard-delete a supplier row.

    Existing listings are KEPT for audit history: their supplier_id is nulled
    (renders as 'â€”' in the admin UI, never as a fake/deleted channel). Skips log
    rows for the supplier are removed with it.
    """
    with db_session(db_path) as conn:
        conn.execute(
            "UPDATE listings SET supplier_id = NULL WHERE supplier_id = ?",
            (supplier_id,),
        )
        conn.execute("DELETE FROM skips WHERE supplier_id = ?", (supplier_id,))
        cursor = conn.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
        return cursor.rowcount > 0

def list_supplier_items(active_only: bool = True, db_path: Optional[str] = None) -> List[models.SupplierItem]:
    """Fetch suppliers as strongly-typed SupplierItem dataclasses."""
    rows = list_suppliers(active_only=active_only, db_path=db_path)
    return [models.SupplierItem.from_row(r) for r in rows]

# ---------------------------------------------------------------------------
# .env bootstrap seeding â€” SOURCE_CHANNELS is ONE-TIME, never live state
# ---------------------------------------------------------------------------
# The suppliers table is the single source of truth for what is monitored.
# SOURCE_CHANNELS is only consulted on the very first startup with an empty
# suppliers table ('env_seed_completed' unset + zero rows); afterwards every
# restart skips it no matter what .env contains, so a supplier deleted via the
# admin bot cannot silently reappear. /reseed_from_env exists as the explicit,
# manual-only way to re-import from .env.
ENV_SEED_KEY = "env_seed_completed"

def env_seed_completed(db_path: Optional[str] = None) -> bool:
    """True once the one-time .env bootstrap has run (or been migrated)."""
    return get_setting(ENV_SEED_KEY, "0", db_path=db_path) == "1"

def mark_env_seed_completed(db_path: Optional[str] = None) -> None:
    """Record that .env must never be re-synced automatically again."""
    set_setting(ENV_SEED_KEY, "1", db_path=db_path)

def clear_env_seed_completed(db_path: Optional[str] = None) -> None:
    """Clear the seed marker so the next ensure_env_seed re-runs bootstrap."""
    set_setting(ENV_SEED_KEY, "0", db_path=db_path)

def count_suppliers(db_path: Optional[str] = None) -> int:
    """Total supplier rows (active + paused), for the fresh-vs-existing check."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()
    return int(row["n"])

def seed_suppliers_from_env(
    channels: List[str],
    db_path: Optional[str] = None,
) -> int:
    """Bootstrap: upsert every channel from the env list into suppliers.

    Numeric ids become channel_id-keyed rows, usernames are stored lowercased.
    Returns the number of channels processed.
    """
    seeded = 0
    for raw in channels:
        ch = (raw or "").strip()
        if not ch:
            continue
        if is_numeric_identifier(ch):
            channel_id = normalize_channel_id(int(ch))
            add_supplier(
                channel_username=str(channel_id),
                channel_id=channel_id,
                db_path=db_path,
            )
        else:
            add_supplier(
                ch.lstrip("@").lower(),
                db_path=db_path,
            )
        seeded += 1
    return seeded

def ensure_env_seed(
    channels: List[str],
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the one-time .env bootstrap exactly once, safely.

    - marker already set  -> skip entirely ('already_seeded'); deleted suppliers
      never come back.
    - marker unset + rows -> existing DB migration: mark seeded WITHOUT touching
      rows, so a deploy never re-adds what the admin already deleted.
    - marker unset + empty -> seed from .env, then mark.

    Returns a dict describing what happened for startup logging.
    """
    if env_seed_completed(db_path):
        return {"state": "already_seeded", "seeded": 0, "migrated": False}

    existing = count_suppliers(db_path)
    if existing > 0:
        mark_env_seed_completed(db_path)
        record_audit(
            "env_seed_migrated",
            None,
            actor_id=None,
            detail=f"suppliers table already had {existing} row(s); .env seeding skipped",
            db_path=db_path,
        )
        return {"state": "migrated", "seeded": 0, "migrated": True, "existing": existing}

    seeded = seed_suppliers_from_env(channels, db_path=db_path)
    mark_env_seed_completed(db_path)
    record_audit(
        "env_seed_completed",
        None,
        actor_id=None,
        detail=f"seeded {seeded} supplier(s) from SOURCE_CHANNELS",
        db_path=db_path,
    )
    return {"state": "seeded", "seeded": seeded, "migrated": False}

def validate_env_seed_config(
    channels_raw: str, db_path: Optional[str] = None
) -> Optional[str]:
    """Return a bootstrapping WARNING message (or None) about the .env seed state.

    Never fatal: SOURCE_CHANNELS is entirely optional. When it is empty AND the
    suppliers table is empty, the bot simply starts with 0 monitored sources and
    the admin adds channels later via the Sources menu. The caller logs the
    returned string with logger.warning(...) and continues startup normally.
    Must be called AFTER init_db().
    """
    if count_suppliers(db_path=db_path) == 0 and not env_seed_completed(db_path=db_path):
        if not (channels_raw or "").strip():
            return (
                "No suppliers configured yet (SOURCE_CHANNELS empty, database empty). "
                "Bot will start with 0 monitored sources â€” add channels via the admin "
                "bot's Sources menu (Add Source, or forward a message from a private "
                "channel/group)."
            )
    return None

