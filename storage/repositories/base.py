"""Database base connection, session management, and schema migrations."""

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


DEFAULT_DB_PATH = os.environ.get("DB_PATH", "monitor.db")

# ---------------------------------------------------------------------------
# Schema versioning.
# v1 : unique (supplier_id, source_message_id), new columns, audit log,
#      fingerprint dedup.
# v2 : fingerprint index.
# v3 : intent column (AI-detected buy/sell/neutral).
# v4 : post_number column + backfilled sequential number for published posts.
# v5 : header_word column (AI buyer-framed header tagline). DORMANT since v12 \u2014
#       the header is now admin-supplied custom emoji (see the headers table).
# v6 : skips table + price-aware content fingerprint for dedup.
# v7 : ai_cache table (fingerprint-keyed AI analysis results).
# v8 : suppliers.display_name for friendly labels (IDs stay the matching key).
# v9 : pricing system removed at the application level (columns stay dormant).
# v10: destinations table + forwardings queue (bot-published -> destination groups).
# v11: listings.hold_until + listings.duplicate_of (the dedup quarantine).
# v12: headers table (admin-managed custom-emoji header pool) + listings.header_id.
# ---------------------------------------------------------------------------
# NOTE: user_version reached 11 in production before SCHEMA_VERSION tracked it, so
# the marker here is deliberately bumped PAST every version that has ever been
# written to a real database. Never reuse a number a live DB may already carry.
SCHEMA_VERSION = 12

# Columns added in schema v1 to an existing (v0) listings table.
V1_LISTING_COLUMNS = {
    "platform_name": "TEXT",
    "retry_count": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
    "reviewed_by": "INTEGER",
    "reviewed_at": "TEXT",
    "published_at": "TEXT",
    "fingerprint": "TEXT",
}

_SOURCE_HWM_PREFIX = "hwm:"

async def run_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking DB call off the event loop (the one async-DB idiom).

    Writes contend on SQLite's single writer lock; a call that hits
    busy_timeout would otherwise freeze the whole event loop. Every database
    WRITE from async code must go through this helper. WAL-mode reads never
    block on the writer, so read-only lookups may stay synchronous (ASYNC-1).
    """
    return await asyncio.to_thread(func, *args, **kwargs)

def _get_default_db_path() -> str:
    import sys
    db_mod = sys.modules.get('storage.db') or sys.modules.get('db')
    if db_mod is not None and hasattr(db_mod, 'DEFAULT_DB_PATH'):
        return db_mod.DEFAULT_DB_PATH
    return DEFAULT_DB_PATH

def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or _get_default_db_path()
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    # SQLite does NOT enforce foreign keys by default; enable per connection so
    # supplier_id/listing_id references are actually validated (DB-1).
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def _commit_with_retry(conn, retries: int = 5, base_delay: float = 0.1) -> None:
    """Commit, retrying briefly on SQLITE_BUSY / database-is-locked errors."""
    for attempt in range(retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError:
            if attempt >= retries - 1:
                raise
            time.sleep(base_delay * (attempt + 1))

# ---------------------------------------------------------------------------
# Connection strategy (PERF-1).
# Each db_session() uses a short-lived, per-call connection. Reusing pooled
# connections across await boundaries / threads caused unbounded SELECT hangs
# that stalled the whole asyncio event loop on Windows, so pooling is removed
# in favour of fresh connections with a bounded busy_timeout (a locked DB now
# raises instead of freezing the process).
# ---------------------------------------------------------------------------
@contextmanager
def db_session(db_path: Optional[str] = None):
    """Context manager: opens a fresh connection, commits, and closes."""
    conn = get_db_connection(db_path)
    try:
        yield conn
        _commit_with_retry(conn)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Initialization & migrations
# ---------------------------------------------------------------------------
def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite tables/indexes and migrate an existing schema."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()

        # 1. Suppliers table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT UNIQUE,
                channel_id INTEGER UNIQUE,
                display_name TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL
            );
            """
        )

        # 2. Listings table (full schema; old DBs get missing columns migrated)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER,
                source_message_id INTEGER NOT NULL,
                game_name TEXT,
                rank_tier TEXT,
                status TEXT NOT NULL,
                raw_text TEXT,
                clean_text TEXT,
                published_message_id INTEGER,
                post_number INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                platform_name TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                published_at TEXT,
                fingerprint TEXT,
                intent TEXT,
                FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
            );
            """
        )

        # 3. Blocklist hits table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS blocklist_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                matched_keyword TEXT NOT NULL,
                FOREIGN KEY(listing_id) REFERENCES listings(id)
            );
            """
        )

        # 4. Audit log
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                listing_id INTEGER,
                actor_id INTEGER,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

        # 5. App settings (key/value) for runtime toggles like the pause switch
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # 6. Skip log â€” every filtered-away message with its reason so the daily
        #    report can break skipped stats down per reason and per supplier.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER,
                message_id INTEGER,
                reason TEXT NOT NULL,
                raw_text TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
            );
            """
        )

        # 7. AI analysis cache â€” fingerprint-keyed, so the same listing content
        #    is never re-analyzed (edits, restarts, re-deliveries, rephrase sweep).
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                analysis_json TEXT NOT NULL,
                model_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_cache_created ON ai_cache(created_at);"
        )

        # 8. Destinations â€” groups/chats that receive forwarded bot publications.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                title TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # 9. Forwardings queue â€” one row per (bot-published message, destination).
        #    UNIQUE(...) makes re-queuing idempotent: publishing bookkeeping or a
        #    restart can never enqueue the same forward twice (DEST-1).
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS forwardings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                published_chat_id TEXT NOT NULL,
                published_message_id INTEGER NOT NULL,
                destination_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                forwarded_at TEXT,
                destination_message_id INTEGER,
                UNIQUE(published_chat_id, published_message_id, destination_id)
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwardings_pending ON forwardings(status, id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwardings_dest ON forwardings(destination_id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwardings_listing ON forwardings(listing_id);"
        )

        # Safe indexes on columns present in both v0 and v1 schemas
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_suppliers_channel_id ON suppliers(channel_id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_suppliers_username ON suppliers(channel_username);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_supplier_msg ON listings(supplier_id, source_message_id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_created_at ON listings(created_at);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocklist_listing ON blocklist_hits(listing_id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_listing ON audit_log(listing_id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skips_reason_ts ON skips(reason, timestamp);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skips_ts ON skips(timestamp);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skips_supplier_ts ON skips(supplier_id, timestamp);"
        )

        _migrate(conn)

def _migrate(conn: sqlite3.Connection) -> None:
    """Migrate older schemas to the current version."""
    cursor = conn.cursor()
    version = cursor.execute("PRAGMA user_version").fetchone()[0]

    if version < 1:
        table_names = {
            r["name"]
            for r in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "listings" in table_names:
            existing_cols = {
                r["name"] for r in cursor.execute("PRAGMA table_info(listings)").fetchall()
            }
            for col, decl in V1_LISTING_COLUMNS.items():
                if col not in existing_cols:
                    cursor.execute(
                        f"ALTER TABLE listings ADD COLUMN {col} {decl}"
                    )

            # Deduplicate any pre-existing duplicates (keep the latest row).
            cursor.execute(
                """
                DELETE FROM listings WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM listings
                    GROUP BY supplier_id, source_message_id
                )
                """
            )

        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_unique "
            "ON listings(supplier_id, source_message_id)"
        )
        cursor.execute("PRAGMA user_version = 1")

    if version < 2:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint)"
        )
        cursor.execute("PRAGMA user_version = 2")

    if version < 3:
        table_info = cursor.execute("PRAGMA table_info(listings)").fetchall()
        cols = {r["name"] for r in table_info}
        if "intent" not in cols:
            cursor.execute("ALTER TABLE listings ADD COLUMN intent TEXT")
        cursor.execute("PRAGMA user_version = 3")

    if version < 4:
        table_info = cursor.execute("PRAGMA table_info(listings)").fetchall()
        cols = {r["name"] for r in table_info}
        if "post_number" not in cols:
            cursor.execute("ALTER TABLE listings ADD COLUMN post_number INTEGER")

        # Backfill a sequential number for every already-published post (in
        # publish order), so the admin can reference past posts the same way.
        published = cursor.execute(
            """
            SELECT id FROM listings
            WHERE status = 'published' AND post_number IS NULL
            ORDER BY coalesce(published_at, created_at), id
            """
        ).fetchall()
        for n, row in enumerate(published, start=1):
            cursor.execute(
                "UPDATE listings SET post_number = ? WHERE id = ?",
                (n, row["id"]),
            )
        if published:
            cursor.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) "
                "VALUES ('post_seq', ?, ?)",
                (str(len(published)), datetime.now(timezone.utc).isoformat()),
            )
        cursor.execute("PRAGMA user_version = 4")

    if version < 5:
        table_info = cursor.execute("PRAGMA table_info(listings)").fetchall()
        cols = {r["name"] for r in table_info}
        if "header_word" not in cols:
            cursor.execute("ALTER TABLE listings ADD COLUMN header_word TEXT")
        # No backfill â€” NULL falls back to the buyer default header at render.
        cursor.execute("PRAGMA user_version = 5")

    if version < 6:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER,
                message_id INTEGER,
                reason TEXT NOT NULL,
                raw_text TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY(supplier_id) REFERENCES suppliers(id)
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skips_reason_ts ON skips(reason, timestamp);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skips_ts ON skips(timestamp);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_skips_supplier_ts ON skips(supplier_id, timestamp);"
        )
        cursor.execute("PRAGMA user_version = 6")

    if version < 7:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                analysis_json TEXT NOT NULL,
                model_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_cache_created ON ai_cache(created_at);"
        )
        cursor.execute("PRAGMA user_version = 7")

    if version < 8:
        table_info = cursor.execute("PRAGMA table_info(suppliers)").fetchall()
        cols = {r["name"] for r in table_info}
        if "display_name" not in cols:
            cursor.execute("ALTER TABLE suppliers ADD COLUMN display_name TEXT")
        cursor.execute("PRAGMA user_version = 8")

    if version < 9:
        # PRIC-1: the pricing system is removed at the application level. Source
        # price is still read from the message text transiently for the dedup
        # fingerprint only; no price is stored, computed, or rendered. Legacy
        # columns (listings.original_price/our_price, suppliers.markup_multiplier)
        # are intentionally LEFT in place so existing databases migrate without a
        # risky table rebuild â€” new databases never create them, and no code in
        # the application reads or writes them anymore. Fresh installs created
        # after v8 have no such columns (see CREATE TABLE above).
        cursor.execute("PRAGMA user_version = 9")

    if version < 10:
        # DEST-1: destinations config + the persistent forwarding queue. Pure
        # additive schema â€” existing v9 databases gain two new tables and their
        # indexes; no existing table is touched.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                title TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS forwardings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                published_chat_id TEXT NOT NULL,
                published_message_id INTEGER NOT NULL,
                destination_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                forwarded_at TEXT,
                UNIQUE(published_chat_id, published_message_id, destination_id)
            );
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwardings_pending ON forwardings(status, id);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwardings_dest ON forwardings(destination_id);"
        )
        cursor.execute("PRAGMA user_version = 10")

    # QUARANTINE: the dedup hold. Purely additive — two nullable columns and
    # one index, no existing row is read, rewritten, or deleted, so this is
    # safe on a live database mid-burst.
    #
    # hold_until  : when the held-listings worker may evaluate this listing.
    # duplicate_of: the listing id that won the dedup race, so every
    #              suppressed twin is traceable to the copy that shipped.
    #
    # RECONCILE (MIGRATION-1): this block deliberately does NOT sit behind
    # `if version < 11`. A production database was found carrying
    # user_version=11 with neither column present, and the old version guard
    # therefore skipped this migration forever: ingest failed on every insert
    # and the held worker crash-looped, with no way for the code to recover.
    # A version marker is not proof of schema state, so the actual columns are
    # inspected on every startup and anything missing is added. The statements
    # are idempotent, so this is a no-op once the schema is correct.
    #
    # user_version is advanced only AFTER the columns exist, so the marker can
    # never lead the schema again.
    _QUARANTINE_COLUMNS = (
        ("hold_until", "TEXT"),
        ("duplicate_of", "INTEGER"),
    )
    _table_info = cursor.execute("PRAGMA table_info(listings)").fetchall()
    if _table_info:  # only reconcile when the table actually exists
        _cols = {r["name"] for r in _table_info}
        for _col, _decl in _QUARANTINE_COLUMNS:
            if _col not in _cols:
                cursor.execute(f"ALTER TABLE listings ADD COLUMN {_col} {_decl}")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_hold "
            "ON listings(status, hold_until)"
        )
        # The idempotency index insert_listing()'s ON CONFLICT clause depends on.
        # It used to be created only in the `version < 1` block, so a database
        # that had diverged from its version marker would reject every insert
        # with "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
        # constraint". Recreated unconditionally for the same reason as the
        # columns above. IF NOT EXISTS makes it free when the index is present.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_unique "
            "ON listings(supplier_id, source_message_id)"
        )
    if version < 11:
        cursor.execute("PRAGMA user_version = 11")

    # HEADERS (v12): the pool of admin-managed custom-emoji headers. Each row is
    # one "WTB" drawn as three custom emoji, stored as the exact glyph Telegram
    # uses (alts) plus their document ids, so a render never has to go back to
    # the API to resolve them. listings.header_id is the STICKY pick: whichever
    # header a listing was assigned is reused by every later render of it
    # (preview, edit, stale-claim recovery), so the admin preview can never
    # disagree with the post that actually shipped.
    #
    # Like the quarantine reconcile above, this runs UNCONDITIONALLY and
    # inspects real schema state rather than trusting the version marker \u2014 a
    # database in the wild was already found carrying user_version=11 with none
    # of the v11 columns (MIGRATION-1), so a bare `if version < 12` guard is
    # not trustworthy here. Every statement is idempotent, so this is a cheap
    # no-op once the schema is correct.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS headers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            emoji_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    _header_table_info = cursor.execute("PRAGMA table_info(listings)").fetchall()
    if _header_table_info:  # only reconcile when the table actually exists
        _header_cols = {r["name"] for r in _header_table_info}
        if "header_id" not in _header_cols:
            cursor.execute("ALTER TABLE listings ADD COLUMN header_id INTEGER")
    _fwd_table_info = cursor.execute("PRAGMA table_info(forwardings)").fetchall()
    if _fwd_table_info:
        _fwd_cols = {r["name"] for r in _fwd_table_info}
        if "destination_message_id" not in _fwd_cols:
            cursor.execute("ALTER TABLE forwardings ADD COLUMN destination_message_id INTEGER")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwardings_listing ON forwardings(listing_id);"
        )
    if version < 12:
        cursor.execute("PRAGMA user_version = 12")

# Schema objects the running code depends on. Used by verify_schema() so a
# diverged database is reported loudly at startup instead of failing later
# inside a worker, where the traceback is easy to miss.
_REQUIRED_LISTINGS_COLUMNS = ("hold_until", "duplicate_of", "header_id")

def verify_schema(db_path: str = None) -> list:
    """Return the list of required columns missing from `listings` (empty = ok).

    Read-only. Does not raise on a missing file or table so it is safe to call
    straight after init_db() in the startup path.
    """
    path = db_path or DEFAULT_DB_PATH
    try:
        with db_session(path) as conn:
            rows = conn.execute("PRAGMA table_info(listings)").fetchall()
            if not rows:
                return list(_REQUIRED_LISTINGS_COLUMNS)
            cols = {r["name"] for r in rows}
            return [c for c in _REQUIRED_LISTINGS_COLUMNS if c not in cols]
    except sqlite3.Error:
        return list(_REQUIRED_LISTINGS_COLUMNS)

# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
def is_numeric_identifier(value: Optional[str]) -> bool:
    """True if the value looks like a numeric Telegram chat id (e.g. -1003340459479)."""
    v = (value or "").strip()
    return bool(v) and v.lstrip("-").isdigit()

# ---------------------------------------------------------------------------
# Canonical chat-id form
#
# Telegram has two equivalent ids for the same chat:
#   * bare form  (entity.id)  -> channels: 4331866910
#   * marked form             -> channels: -1004331866910
# event.chat_id / telethon.utils.get_peer_id() ALWAYS report the marked form,
# so every channel_id persisted MUST be the marked form. Storing entity.id (bare)
# silently breaks resolve_supplier_for_event() matching for those suppliers.
# ---------------------------------------------------------------------------
TELEGRAM_CHANNEL_MARK = 1000000000000

def normalize_channel_id(value: Any) -> Optional[int]:
    """Return the canonical marked chat id for storage/matching.

    Accepts an int (bare or marked), a numeric string, or a Telethon entity / peer
    object. Idempotent: already-marked ids pass through unchanged; bare channel
    ids are re-marked with the -100 prefix Telethon events report.
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = (value or "").strip()
        if not is_numeric_identifier(raw):
            return None
        value = int(raw)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            return value
        return -(TELEGRAM_CHANNEL_MARK + value)
    # Telethon entity / peer -> the authoritative marked form
    try:
        from telethon import utils

        return int(utils.get_peer_id(value))
    except Exception:
        bare = getattr(value, "id", None)
        if bare is None:
            return None
        return normalize_channel_id(int(bare))

def to_peer_reference(chat_id: Any) -> Any:
    """Convert a stored chat/channel reference to the type expected by Telethon.

    Telethon expects integer IDs as `int` (bare or -100 marked) so it can look
    them up in its session entity cache. Passing numeric strings (e.g. '-1004444128274')
    causes Telethon to attempt username/phone string lookup and fail with
    'Cannot find any entity corresponding to ...'. Public usernames ('@handle')
    remain strings.
    """
    if chat_id is None:
        return None
    if isinstance(chat_id, int):
        return chat_id
    if isinstance(chat_id, str):
        s = chat_id.strip()
        if is_numeric_identifier(s):
            return int(s)
        return s
    return chat_id

