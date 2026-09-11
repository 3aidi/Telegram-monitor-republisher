"""Database layer for Telegram Monitor & Republisher using SQLite."""

import hashlib
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "monitor.db")

# ---------------------------------------------------------------------------
# Schema versioning.
# v1 : unique (supplier_id, source_message_id), new columns, audit log,
#      fingerprint dedup.
# v2 : fingerprint index.
# v3 : intent column (AI-detected buy/sell/neutral).
# v4 : post_number column + backfilled sequential number for published posts.
# v5 : header_word column (AI buyer-framed header tagline).
# v6 : skips table + price-aware content fingerprint for dedup.
# v7 : ai_cache table (fingerprint-keyed AI analysis results).
# v8 : suppliers.display_name for friendly labels (IDs stay the matching key).
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 8

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


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
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
        except sqlite3.OperationalError as exc:
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
                markup_multiplier REAL NOT NULL DEFAULT 0.75,
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
                original_price REAL,
                our_price REAL,
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

        # 6. Custom emoji role mapping (role -> document_id) from user's emoji packs
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS emoji_config (
                role TEXT PRIMARY KEY,
                emoji_char TEXT NOT NULL,
                document_id INTEGER NOT NULL,
                source TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )

        # 7. Skip log — every filtered-away message with its reason so the daily
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

        # 8. AI analysis cache — fingerprint-keyed, so the same listing content
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
        # No backfill — NULL falls back to the buyer default header at render.
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


# ---------------------------------------------------------------------------
# Fingerprinting (canonical, shared between store + dedup lookup)
# ---------------------------------------------------------------------------
def make_listing_fingerprint(
    clean_text: str, price: Optional[float] = None
) -> str:
    """Hash of the normalized listing content + its price.

    Dedup must work BEFORE the AI runs (and be indifferent to AI output, which
    is non-deterministic), so the fingerprint is built from the deterministic
    source text only — not platform/price (DEDUP-2). The price is folded in so
    the same product re-posted at a different price is NOT treated as a
    duplicate, and a re-post that arrives with a new Telegram message id IS
    caught (identity follows the content, not the message id). The 30-char
    prefix truncation is deliberately gone: it caused unrelated long texts to
    collide.
    """
    norm = re.sub(r"\s+", " ", (clean_text or "").strip().lower())
    if price is not None:
        norm = f"{norm} | {price:g}"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
def is_numeric_identifier(value: Optional[str]) -> bool:
    """True if the value looks like a numeric Telegram chat id (e.g. -1003340459479)."""
    v = (value or "").strip()
    return bool(v) and v.lstrip("-").isdigit()


def add_supplier(
    channel_username: str,
    channel_id: Optional[int] = None,
    markup_multiplier: float = 0.75,
    db_path: Optional[str] = None,
) -> int:
    """Add a new supplier channel or activate it if previously added."""
    username = channel_username.strip()
    if username.startswith("@"):
        username = username[1:]

    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO suppliers (channel_username, channel_id, active, markup_multiplier, added_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(channel_username) DO UPDATE SET
                    channel_id = coalesce(excluded.channel_id, suppliers.channel_id),
                    active = 1,
                    markup_multiplier = coalesce(excluded.markup_multiplier, suppliers.markup_multiplier)
                """,
                (username.lower(), channel_id, markup_multiplier, now_iso),
            )
        except sqlite3.IntegrityError:
            # channel_id already belongs to another username (e.g. a renamed
            # channel): act as an upsert on channel_id instead of failing (DB-2).
            # Prefer a REAL username over a numeric-ID string so friendly labels
            # survive even when the same channel is added once by ID and once by
            # username. Keep the existing row's username when it is already real.
            existing_row = conn.execute(
                "SELECT channel_username FROM suppliers WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            existing_user = (existing_row["channel_username"] if existing_row else "") or ""
            if existing_user and not is_numeric_identifier(existing_user):
                username = existing_user
            conn.execute(
                """
                UPDATE suppliers
                SET channel_username = ?, active = 1,
                    markup_multiplier = coalesce(?, markup_multiplier)
                WHERE channel_id = ?
                """,
                (username.lower(), markup_multiplier, channel_id),
            )
        row = conn.execute(
            "SELECT id FROM suppliers WHERE channel_username = ? OR channel_id = ?",
            (username.lower(), channel_id),
        ).fetchone()
        return row["id"] if row else cursor.lastrowid


def set_supplier_channel_id(
    channel_username: str, channel_id: int, db_path: Optional[str] = None
) -> bool:
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


def set_supplier_rule(
    channel_username: str, markup_multiplier: float, db_path: Optional[str] = None
) -> bool:
    """Set custom markup multiplier for a specific supplier."""
    username = channel_username.strip().lstrip("@").lower()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE suppliers SET markup_multiplier = ? WHERE channel_username = ?",
            (markup_multiplier, username),
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


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
def insert_listing(
    supplier_id: Optional[int],
    source_message_id: int,
    game_name: Optional[str],
    rank_tier: Optional[str],
    original_price: Optional[float],
    our_price: Optional[float],
    status: str,
    raw_text: str,
    clean_text: str,
    published_message_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> int:
    """Insert a captured listing record (idempotent per supplier+message)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO listings (
                supplier_id, source_message_id, game_name, rank_tier,
                original_price, our_price, status, raw_text, clean_text,
                published_message_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier_id, source_message_id) DO NOTHING
            """,
            (
                supplier_id,
                source_message_id,
                game_name,
                rank_tier,
                original_price,
                our_price,
                status,
                raw_text,
                clean_text,
                published_message_id,
                now_iso,
                now_iso,
            ),
        )
        row = cursor.execute(
            "SELECT id FROM listings WHERE supplier_id = ? AND source_message_id = ?",
            (supplier_id, source_message_id),
        ).fetchone()
        return row["id"] if row else cursor.lastrowid


def update_listing_status(
    listing_id: int,
    status: str,
    our_price: Optional[float] = None,
    published_message_id: Optional[int] = None,
    post_number: Optional[int] = None,
    db_path: Optional[str] = None,
) -> None:
    """Update listing status, and optionally our_price, published_message_id and post_number."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        # published_at is stamped when transitioning to a published state.
        publish_ts = None
        if status == "published":
            publish_ts = now_iso
        conn.execute(
            """
            UPDATE listings
            SET status = ?,
                our_price = coalesce(?, our_price),
                published_message_id = coalesce(?, published_message_id),
                post_number = coalesce(?, post_number),
                published_at = coalesce(?, published_at),
                updated_at = ?
            WHERE id = ?
            """,
            (status, our_price, published_message_id, post_number, publish_ts, now_iso, listing_id),
        )


def update_listing_content(
    listing_id: int,
    clean_text: str,
    our_price: Optional[float] = None,
    rank_tier: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Update content and price when source message is edited."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE listings
            SET clean_text = ?,
                our_price = coalesce(?, our_price),
                rank_tier = coalesce(?, rank_tier),
                updated_at = ?
            WHERE id = ?
            """,
            (clean_text, our_price, rank_tier, now_iso, listing_id),
        )


def update_listing_fields(
    listing_id: int,
    platform_name: Optional[str] = None,
    original_price: Optional[float] = None,
    our_price: Optional[float] = None,
    intent: Optional[str] = None,
    header_word: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Persist parsed listing fields (platform + original price)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE listings
            SET platform_name = coalesce(?, platform_name),
                original_price = coalesce(?, original_price),
                our_price = coalesce(?, our_price),
                intent = coalesce(?, intent),
                header_word = coalesce(?, header_word),
                updated_at = ?
            WHERE id = ?
            """,
            (platform_name, original_price, our_price, intent, header_word, now_iso, listing_id),
        )


def set_listing_fingerprint(
    listing_id: int,
    clean_text: str,
    price: Optional[float] = None,
    db_path: Optional[str] = None,
) -> None:
    """Store the content dedup fingerprint (normalized text + price)."""
    fingerprint = make_listing_fingerprint(clean_text, price=price)
    with db_session(db_path) as conn:
        conn.execute(
            "UPDATE listings SET fingerprint = ? WHERE id = ?",
            (fingerprint, listing_id),
        )


def mark_listing_failed(
    listing_id: int, error_message: str, db_path: Optional[str] = None
) -> None:
    """Move a listing to the failed/DLQ state and record the error."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE listings
            SET status = 'failed',
                last_error = ?,
                retry_count = retry_count + 1,
                updated_at = ?
            WHERE id = ?
            """,
            ((error_message or "")[:500], now_iso, listing_id),
        )


def requeue_listing(listing_id: int, db_path: Optional[str] = None) -> bool:
    """Move a failed/error listing back to the approved (published-by-worker) state."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE listings SET status = 'approved', last_error = NULL, updated_at = ? WHERE id = ?",
            (now_iso, listing_id),
        )
        return cursor.rowcount > 0


def get_failed_listings(
    limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return failed listings (DLQ) ordered oldest-first.

    'error' is retained in the read query purely for legacy rows: no current
    code path writes it (mark_listing_failed sets 'failed' only) — STATE-1.
    """
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.*, s.channel_username as supplier_username,
                   s.display_name as supplier_display_name
            FROM listings l
            LEFT JOIN suppliers s ON l.supplier_id = s.id
            WHERE l.status IN ('failed', 'error')
            ORDER BY l.updated_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_listing_by_source(
    supplier_id: Optional[int],
    source_message_id: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve existing listing by supplier_id and source_message_id."""
    with db_session(db_path) as conn:
        if supplier_id is not None:
            row = conn.execute(
                "SELECT * FROM listings WHERE supplier_id = ? AND source_message_id = ?",
                (supplier_id, source_message_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM listings WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
        return dict(row) if row else None


def get_listing_by_id(
    listing_id: int, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return dict(row) if row else None


def get_post_by_number(
    post_number: int, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Fetch a published post by its sequential post number, joined with its supplier."""
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.channel_id as supplier_channel_id,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status = 'published'
          AND l.published_message_id IS NOT NULL
          AND l.post_number = ?
    """
    with db_session(db_path) as conn:
        row = conn.execute(query, (post_number,)).fetchone()
        return dict(row) if row else None


def get_last_source_message_id(
    supplier_id: Optional[int], db_path: Optional[str] = None
) -> int:
    """Highest source message id already recorded for a supplier (for backfill)."""
    if supplier_id is None:
        return 0
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(source_message_id), 0) FROM listings WHERE supplier_id = ?",
            (supplier_id,),
        ).fetchone()
        return int(row[0])


def record_blocklist_hit(
    listing_id: int, matched_keyword: str, db_path: Optional[str] = None
) -> None:
    """Record illicit/stolen account keyword match."""
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO blocklist_hits (listing_id, matched_keyword) VALUES (?, ?)",
            (listing_id, matched_keyword),
        )


def find_recent_similar_listing(
    clean_text: str,
    hours: int = 48,
    price: Optional[float] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Search for a similar fingerprint (normalized text + price) in the last N
    hours. Content-based: a re-post with a fresh Telegram message id is still
    caught, while the same text at a different price is not (deduplication).
    """
    if not (clean_text or "").strip():
        return None

    fingerprint = make_listing_fingerprint(clean_text, price=price)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    query = """
        SELECT * FROM listings
        WHERE created_at >= ?
          AND fingerprint = ?
          AND status IN ('published', 'pending_approval', 'pending_review', 'approved')
        ORDER BY id DESC LIMIT 1
    """

    with db_session(db_path) as conn:
        row = conn.execute(query, (cutoff, fingerprint)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def record_audit(
    action: str,
    listing_id: Optional[int] = None,
    actor_id: Optional[int] = None,
    detail: Optional[Any] = None,
    db_path: Optional[str] = None,
) -> None:
    """Append an event to the audit log."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_log (action, listing_id, actor_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, listing_id, actor_id, str(detail or "")[:1000], now_iso),
        )


# ---------------------------------------------------------------------------
# Skip log
# ---------------------------------------------------------------------------
def log_skip(
    supplier_id: Optional[int],
    message_id: Optional[int],
    reason: str,
    raw_text: str,
    db_path: Optional[str] = None,
) -> int:
    """Record a single skipped message with its named filter reason.

    This is the single source of truth for the daily report's per-reason and
    per-supplier skip breakdowns.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO skips (supplier_id, message_id, reason, raw_text, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (supplier_id, message_id, reason, (raw_text or "")[:4000], now_iso),
        )
        return cursor.lastrowid


def get_skip_reasons_today(db_path: Optional[str] = None) -> Dict[str, int]:
    """Counts of today's skips grouped by reason (e.g. {'duplicate': 1})."""
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT reason, count(*) as cnt
            FROM skips
            WHERE timestamp >= ?
            GROUP BY reason
            ORDER BY cnt DESC, reason ASC
            """,
            (today_start,),
        ).fetchall()
        return {row["reason"]: row["cnt"] for row in rows}


def get_supplier_stats_today(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Per-active-supplier daily breakdown: processed / published / skipped.

    'processed' counts every listing captured today, 'published' those that
    made it out today (by capture date, matching the headline stats), and
    'skipped' the rows in the skips log today. Correlated subqueries keep the
    three numbers independent (no join cartesian inflation).
    """
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id,
                   s.channel_username,
                   s.display_name as supplier_display_name,
                   (SELECT count(*) FROM listings l
                     WHERE l.supplier_id = s.id AND l.created_at >= ?) AS processed,
                   (SELECT count(*) FROM listings l
                     WHERE l.supplier_id = s.id AND l.created_at >= ?
                       AND l.status = 'published') AS published,
                   (SELECT count(*) FROM skips k
                     WHERE k.supplier_id = s.id AND k.timestamp >= ?) AS skipped
            FROM suppliers s
            WHERE s.active = 1
            ORDER BY s.id ASC
            """,
            (today_start, today_start, today_start),
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Stats & review queues
# ---------------------------------------------------------------------------
def get_today_stats(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Calculate daily operational statistics for the admin bot.

    Headline totals (processed/published/pending/errors) come from the listings
    table; skip counts come exclusively from the skips log (single source of
    truth for skipped messages).
    """
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )

    with db_session(db_path) as conn:
        active_suppliers = conn.execute(
            "SELECT count(*) FROM suppliers WHERE active = 1"
        ).fetchone()[0]

        total_processed = conn.execute(
            "SELECT count(*) FROM listings WHERE created_at >= ?", (today_start,)
        ).fetchone()[0]

        published_count = conn.execute(
            "SELECT count(*) FROM listings WHERE created_at >= ? AND status = 'published'",
            (today_start,),
        ).fetchone()[0]

        pending_count = conn.execute(
            "SELECT count(*) FROM listings WHERE created_at >= ? AND status IN ('pending_approval', 'pending_review')",
            (today_start,),
        ).fetchone()[0]

        errors_count = conn.execute(
            "SELECT count(*) FROM listings WHERE created_at >= ? AND status IN ('error', 'failed')",
            (today_start,),
        ).fetchone()[0]

    skipped_reasons = get_skip_reasons_today(db_path)
    total_skipped = sum(skipped_reasons.values())
    supplier_breakdown = get_supplier_stats_today(db_path)

    return {
        "active_suppliers": active_suppliers,
        "total_processed": total_processed,
        "published": published_count,
        "pending": pending_count,
        "errors": errors_count,
        "total_skipped": total_skipped,
        "skip_reasons": skipped_reasons,
        "supplier_breakdown": supplier_breakdown,
    }


def get_pending_listings(
    limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve listings pending admin approval."""
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status IN ('pending_approval', 'pending_review')
        ORDER BY l.id ASC
        LIMIT ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]


def get_unpublished_listings(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Listings that have not been published yet (received / pending review / approval)."""
    query = """
        SELECT * FROM listings
        WHERE status IN ('received', 'pending_approval', 'pending_review')
        ORDER BY id ASC
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_approved_listings_to_publish(
    limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Listings approved by admin that still need publishing (never double-publish)."""
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status = 'approved'
          AND l.published_message_id IS NULL
        ORDER BY l.id ASC
        LIMIT ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]


def get_published_listings(
    limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Recently published listings (with channel post ids) plus their supplier,
    so every post can be traced back to its source message.
    """
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.channel_id as supplier_channel_id,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status = 'published'
          AND l.published_message_id IS NOT NULL
        ORDER BY l.post_number IS NULL, l.post_number DESC, l.id DESC
        LIMIT ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]


def next_post_number(db_path: Optional[str] = None) -> int:
    """Atomically reserve + return the next sequential post number.

    Backed by the 'post_seq' counter in app_settings (seeded by the v4
    migration with the count of already-published posts). Gaps are acceptable:
    a number reserved before a send that later fails is simply skipped.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE app_settings SET value = CAST(value AS INTEGER) + 1, updated_at = ? "
            "WHERE key = 'post_seq'",
            (now_iso,),
        )
        if cursor.rowcount:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'post_seq'"
            ).fetchone()
            return int(row["value"])
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('post_seq', '1', ?)",
            (now_iso,),
        )
        return 1


# ---------------------------------------------------------------------------
# App settings (key/value) — runtime toggles like the pause switch
# ---------------------------------------------------------------------------
def set_setting(key: str, value: str, db_path: Optional[str] = None) -> None:
    """Upsert a key/value setting."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(value), now_iso),
        )


def get_setting(key: str, default: Optional[str] = None, db_path: Optional[str] = None) -> Optional[str]:
    """Read a key/value setting, returning default if unset."""
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def is_paused(db_path: Optional[str] = None) -> bool:
    """True when publishing is paused via the admin 'All Stop' switch."""
    return get_setting("paused", "0", db_path=db_path) == "1"


def set_paused(paused: bool, db_path: Optional[str] = None) -> None:
    """Set the pause switch ('1' pauses all publishing, '0' resumes)."""
    set_setting("paused", "1" if paused else "0", db_path=db_path)


# ---------------------------------------------------------------------------
# Custom emoji config — role -> document_id mapping from the user's emoji packs
# ---------------------------------------------------------------------------
def set_emoji_config(
    role: str,
    emoji_char: str,
    document_id: int,
    source: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Upsert the document_id used for a message role (fire/lightning/star/...)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO emoji_config (role, emoji_char, document_id, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(role) DO UPDATE SET
                emoji_char = excluded.emoji_char,
                document_id = excluded.document_id,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (role, emoji_char, int(document_id), source, now_iso),
        )


def get_emoji_configs(db_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Return {role: {role, emoji_char, document_id, source}} for all configured emoji."""
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT role, emoji_char, document_id, source FROM emoji_config"
        ).fetchall()
        return {row["role"]: dict(row) for row in rows}


# ---------------------------------------------------------------------------
# AI analysis cache — fingerprint-keyed so identical listing content is never
# re-analyzed by the model (rephrase sweep, edits, re-deliveries).
# ---------------------------------------------------------------------------
def get_ai_cache(
    fingerprint: str,
    max_age_hours: float = 48,
    db_path: Optional[str] = None,
) -> Optional[Tuple[str, Optional[str]]]:
    """Return (analysis_json, model_name) for a fresh cache entry, else None."""
    if not fingerprint:
        return None
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat()
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT analysis_json, model_name FROM ai_cache "
            "WHERE fingerprint = ? AND created_at >= ?",
            (fingerprint, cutoff),
        ).fetchone()
    return (row["analysis_json"], row["model_name"]) if row else None


def set_ai_cache(
    fingerprint: str,
    analysis_json: str,
    model_name: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Upsert an AI analysis result keyed by fingerprint."""
    if not fingerprint or not analysis_json:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ai_cache (fingerprint, analysis_json, model_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                analysis_json = excluded.analysis_json,
                model_name = excluded.model_name,
                updated_at = excluded.updated_at
            """,
            (fingerprint, analysis_json, model_name, now_iso, now_iso),
        )


def prune_ai_cache(max_age_hours: float = 48, db_path: Optional[str] = None) -> int:
    """Delete cache rows older than max_age_hours. Returns the count pruned."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute("DELETE FROM ai_cache WHERE created_at < ?", (cutoff,))
        return cursor.rowcount