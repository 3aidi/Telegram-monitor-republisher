"""Database layer for Telegram Monitor & Republisher using SQLite."""

import asyncio
import hashlib
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

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
# v9 : pricing system removed at the application level (columns stay dormant).
# v10: destinations table + forwardings queue (bot-published -> destination groups).
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 10

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


async def run_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking DB call off the event loop (the one async-DB idiom).

    Writes contend on SQLite's single writer lock; a call that hits
    busy_timeout would otherwise freeze the whole event loop. Every database
    WRITE from async code must go through this helper. WAL-mode reads never
    block on the writer, so read-only lookups may stay synchronous (ASYNC-1).
    """
    return await asyncio.to_thread(func, *args, **kwargs)


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


# ---------------------------------------------------------------------------
# Fingerprinting (canonical, shared between store + dedup lookup)
# ---------------------------------------------------------------------------
# In-flight 'received' rows only match each other within this short window, so
# a row stranded in 'received' (e.g. after a hard kill) cannot blind duplicate
# detection for the full dedup window (CONC-2).
_RECEIVED_DEDUP_MINUTES = 30


def make_listing_fingerprint(
    clean_text: str, price: Optional[float] = None
) -> str:
    """Hash of the normalized listing content + its price.

    Dedup must work BEFORE the AI runs (and be indifferent to AI output, which
    is non-deterministic), so the fingerprint is built from the deterministic
    source text only â€” not platform/price (DEDUP-2). The price is folded in so
    the same product re-posted at a different price is NOT treated as a
    duplicate, and a re-post that arrives with a new Telegram message id IS
    caught (identity follows the content, not the message id). The 30-char
prefix truncation is deliberately gone: it caused unrelated long texts to
    collide.
    """
    norm = unicodedata.normalize("NFKC", (clean_text or "").strip().lower())
    # DEDUP-FN: collapse the punctuation variants that are visually identical to
    # readers — em/en/2-em dashes and the minus sign become '-', smart quotes
    # become straight ASCII, non-breaking spaces fold to a space. A re-post with
    # cosmetic punctuation differences therefore produces the SAME fingerprint
    # and is caught as a duplicate instead of slipping through.
    norm = norm.replace("\u2212", "-").replace("--", "-")
    norm = norm.replace("\u2018", "'").replace("\u2019", "'")
    norm = norm.replace("\u201c", '"').replace("\u201d", '"')
    norm = re.sub(r"[\s\u00a0]+", " ", norm)
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


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
def insert_listing(
    supplier_id: Optional[int],
    source_message_id: int,
    game_name: Optional[str],
    rank_tier: Optional[str],
    status: str,
    raw_text: str,
    clean_text: str,
    published_message_id: Optional[int] = None,
    fingerprint: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Insert a captured listing record (idempotent per supplier+message).

    ``fingerprint`` must be set at insert time (CONC-2 race fix): the dedup
    query reads it from the DB, so writing it later left a window in which two
    identical concurrent messages could both pass the duplicate check.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO listings (
                supplier_id, source_message_id, game_name, rank_tier,
                status, raw_text, clean_text,
                fingerprint, published_message_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(supplier_id, source_message_id) DO NOTHING
            """,
            (
                supplier_id,
                source_message_id,
                game_name,
                rank_tier,
                status,
                raw_text,
                clean_text,
                fingerprint,
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
    published_message_id: Optional[int] = None,
    post_number: Optional[int] = None,
    db_path: Optional[str] = None,
) -> None:
    """Update listing status, and optionally published_message_id and post_number."""
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
                published_message_id = coalesce(?, published_message_id),
                post_number = coalesce(?, post_number),
                published_at = coalesce(?, published_at),
                updated_at = ?
            WHERE id = ?
            """,
            (status, published_message_id, post_number, publish_ts, now_iso, listing_id),
        )


def update_listing_content(
    listing_id: int,
    clean_text: str,
    rank_tier: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Update content when source message is edited."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE listings
            SET clean_text = ?,
                rank_tier = coalesce(?, rank_tier),
                updated_at = ?
            WHERE id = ?
            """,
            (clean_text, rank_tier, now_iso, listing_id),
        )


def update_listing_fields(
    listing_id: int,
    platform_name: Optional[str] = None,
    intent: Optional[str] = None,
    header_word: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Persist parsed listing fields (platform)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE listings
            SET platform_name = coalesce(?, platform_name),
                intent = coalesce(?, intent),
                header_word = coalesce(?, header_word),
                updated_at = ?
            WHERE id = ?
            """,
            (platform_name, intent, header_word, now_iso, listing_id),
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
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.channel_id as supplier_channel_id,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.id = ?
    """
    with db_session(db_path) as conn:
        row = conn.execute(query, (listing_id,)).fetchone()
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
    exclude_listing_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Search for a similar fingerprint (normalized text + price) in the last N
    hours. Content-based: a re-post with a fresh Telegram message id is still
    caught, while the same text at a different price is not (deduplication).

    In-flight ``received`` rows are also matched so a concurrent identical twin
    (different message id, same content) is caught while the first message is
    still being processed. Only *recent* received rows count, so a row stranded
    in ``received`` (e.g. after a hard kill) stops blinding duplicates for a
    few minutes instead of the whole window. The caller's own row is excluded
    (``exclude_listing_id``) because it always carries the same fingerprint.
    """
    if not (clean_text or "").strip():
        return None

    fingerprint = make_listing_fingerprint(clean_text, price=price)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    received_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=_RECEIVED_DEDUP_MINUTES)
    ).isoformat()

    query = """
        SELECT * FROM listings
        WHERE created_at >= ?
          AND fingerprint = ?
          AND (
                status IN ('published', 'pending_approval', 'pending_review', 'approved')
                OR (status = 'received' AND created_at >= ?)
              )
    """
    params: List[Any] = [cutoff, fingerprint, received_cutoff]
    if exclude_listing_id is not None:
        query += " AND id != ?"
        params.append(exclude_listing_id)
    query += " ORDER BY id DESC LIMIT 1"

    with db_session(db_path) as conn:
        row = conn.execute(query, params).fetchone()
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


def get_skipped_listings(
    limit: int = 10, offset: int = 0, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Most recent skipped messages, joined with their supplier (and the linked
    listing row, when one exists) for the admin /skipped review screen."""
    query = """
        SELECT k.id as skip_id, k.supplier_id, k.message_id, k.reason, k.raw_text,
               k.timestamp,
               s.channel_username, s.display_name,
               l.id as listing_id, l.status as listing_status
        FROM skips k
        LEFT JOIN suppliers s ON k.supplier_id = s.id
        LEFT JOIN listings l ON l.supplier_id = k.supplier_id
                            AND l.source_message_id = k.message_id
        ORDER BY k.id DESC
        LIMIT ? OFFSET ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def count_skipped_listings(db_path: Optional[str] = None) -> int:
    """Total skipped messages (same filter as get_skipped_listings)."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM skips").fetchone()
        return int(row["c"])


def reopen_skipped(
    skip_id: int, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Move a skipped listing back to pending_approval for a fresh review.

    Returns the listing dict on success, or None when the skip has no linked
    listing or the listing is no longer in a skipped state."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM skips WHERE id = ?", (skip_id,)).fetchone()
        if not row:
            return None
        listing = conn.execute(
            "SELECT * FROM listings WHERE supplier_id = ? AND source_message_id = ?",
            (row["supplier_id"], row["message_id"]),
        ).fetchone()
        if not listing:
            return None
        if listing["status"] not in ("skipped_duplicate", "skipped_chatter", "skipped_filter", "skipped_no_content", "skipped_admin"):
            return None
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE listings SET status = 'pending_approval', updated_at = ? WHERE id = ?",
            (now_iso, listing["id"]),
        )
        listing = dict(listing)
        listing["status"] = "pending_approval"
    record_audit("skipped_reopen", listing["id"], detail=f"skip #{skip_id}", db_path=db_path)
    return listing


def get_skip_digest_marker(db_path: Optional[str] = None) -> int:
    """Highest skips.id the admin has already been shown (skip-digest window)."""
    return int(get_setting("skip_digest_last_id", "0", db_path=db_path) or 0)


def set_skip_digest_marker(skip_id: int, db_path: Optional[str] = None) -> None:
    """Advance the skip-digest marker past ``skip_id``."""
    set_setting("skip_digest_last_id", str(int(skip_id)), db_path=db_path)


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

    skipped_reasons = get_skip_reasons_today(db_path)
    total_skipped = sum(skipped_reasons.values())
    supplier_breakdown = get_supplier_stats_today(db_path)

    return {
        "active_suppliers": active_suppliers,
        "total_processed": total_processed,
        "published": published_count,
        "pending": pending_count,
        "total_skipped": total_skipped,
        "skip_reasons": skipped_reasons,
        "supplier_breakdown": supplier_breakdown,
    }


def get_pending_listings(
    limit: int = 10, offset: int = 0, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve listings pending admin approval."""
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status IN ('pending_approval', 'pending_review')
        ORDER BY l.id ASC
        LIMIT ? OFFSET ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def count_pending_listings(db_path: Optional[str] = None) -> int:
    """Total listings pending approval (same filter as get_pending_listings)."""
    with db_session(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM listings
            WHERE status IN ('pending_approval', 'pending_review')
            """
        ).fetchone()
        return int(row["c"])


def get_unpublished_listings(
    limit: Optional[int] = None,
    min_age_seconds: Optional[int] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Listings that have not been published yet (received / pending review / approval).

    ``limit`` bounds the sweep â€” startup recovery must be a bounded pass, never
    an unbounded AI batch. ``min_age_seconds`` skips listings created too
    recently so the sweep never races a message the live pipeline is still
    holding in-flight (REPHR-1).
    """
    where = ["status IN ('received', 'pending_approval', 'pending_review')"]
    params: List[Any] = []
    if min_age_seconds:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
        ).isoformat()
        where.append("created_at <= ?")
        params.append(cutoff)
    query = f"SELECT * FROM listings WHERE {' AND '.join(where)} ORDER BY id ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with db_session(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
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


def claim_listing_for_publish(
    listing_id: int, post_number: int, db_path: Optional[str] = None
) -> bool:
    """Atomically claim a listing for one external Telegram publish (once-only).

    The single-statement transition to the transient status 'publishing' IS the
    claim (F3): the guard ``status IN ('approved','pending_approval',
    'pending_review') AND published_message_id IS NULL`` means a second claimer
    — a second worker, an admin double-tap, or a post-crash retry — sees the row
    in a non-claimable state and returns False, so it cannot send another copy.

    The reserved post number is persisted on the row at claim time so a crash
    after the claim but before bookkeeping can be reconstructed byte-for-byte
    during stale-claim recovery (same inputs -> same deterministic message).

    Returns True only for the single winner.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE listings
            SET status = 'publishing',
                post_number = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('approved', 'pending_approval', 'pending_review')
              AND published_message_id IS NULL
            """,
            (post_number, now_iso, listing_id),
        )
        return cursor.rowcount > 0


def get_stale_publish_claims(
    grace_seconds: int = 300, limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Claims stuck in 'publishing' longer than ``grace_seconds``.

    ``updated_at`` is the claim timestamp (set by claim_listing_for_publish).
    Recovery runs in the approved-listings worker: each stale row is verified
    against the destination channel and either recorded as published (the send
    actually landed) or released back to 'approved' (the send provably did not).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat()
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE status = 'publishing'
              AND updated_at <= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def release_publish_claim(
    listing_id: int, status: str = "approved", db_path: Optional[str] = None
) -> bool:
    """Abandon a 'publishing' claim when the send provably did NOT reach the channel.

    Used by the ambiguous-failure path (after destination verification returned
    nothing) and by stale-claim recovery. Releasing puts the listing back where
    the worker can claim it again on the next drain.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE listings
            SET status = ?, updated_at = ?
            WHERE id = ? AND status = 'publishing'
            """,
            (status, now_iso, listing_id),
        )
        return cursor.rowcount > 0


def get_published_listings(
    limit: int = 10, offset: int = 0, db_path: Optional[str] = None
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
        LIMIT ? OFFSET ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def count_published_listings(db_path: Optional[str] = None) -> int:
    """Total published listings (same filter as get_published_listings)."""
    with db_session(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM listings
            WHERE status = 'published' AND published_message_id IS NOT NULL
            """
        ).fetchone()
        return int(row["c"])


def next_post_number(db_path: Optional[str] = None) -> int:
    """Atomically reserve + return the next sequential post number.

    Backed by the 'post_seq' counter in app_settings (seeded by the v4
    migration with the count of already-published posts). Gaps are acceptable:
    a number reserved before a send that later fails is simply skipped.

    The counter row is seeded via INSERT ... ON CONFLICT DO NOTHING so two
    concurrent first-time callers on a fresh database can never both reach an
    unguarded INSERT (which would have raised a PRIMARY KEY violation on the
    loser). Each call then runs inside its own write transaction, so SQLite's
    single-writer rule guarantees every caller sees a distinct value.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('post_seq', '0', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (now_iso,),
        )
        conn.execute(
            "UPDATE app_settings SET value = CAST(value AS INTEGER) + 1, updated_at = ? "
            "WHERE key = 'post_seq'",
            (now_iso,),
        )
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'post_seq'"
        ).fetchone()
        return int(row["value"])


# ---------------------------------------------------------------------------
# App settings (key/value) â€” runtime toggles like the pause switch
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
    """True when AUTOMATIC publishing is paused via the admin 'All Stop' switch.

    Manual admin Approve is unaffected: approval always publishes immediately.
    """
    return get_setting("paused", "0", db_path=db_path) == "1"


def set_paused(paused: bool, db_path: Optional[str] = None) -> None:
    """Set the pause switch ('1' pauses AUTOMATIC publishing, '0' resumes).

    Only gates the bot's own auto-publish paths; manual Approve still publishes.
    """
    set_setting("paused", "1" if paused else "0", db_path=db_path)


def is_buyer_asleep(db_path: Optional[str] = None) -> bool:
    """True when the 'I'm Asleep' toggle is ON (appends a short out-of-office
    footer to every published post; see BUYER_ASLEEP_FOOTER / buy_asleep_footer)."""
    return get_setting("buyer_asleep", "0", db_path=db_path) == "1"


def set_buyer_asleep(asleep: bool, db_path: Optional[str] = None) -> None:
    """Set the 'I'm Asleep' toggle ('1' appends the footer, '0' hides it)."""
    set_setting("buyer_asleep", "1" if asleep else "0", db_path=db_path)


def get_buyer_asleep_footer(db_path: Optional[str] = None) -> str:
    """Footer line appended to published posts while the buyer is 'asleep'.

    Resolved per-render: app_settings key 'buyer_asleep_footer' (settable via
    the admin bot) wins, then the BUYER_ASLEEP_FOOTER env var, then the built-in
    default 'Buyer away, back shortly'.
    """
    custom = get_setting("buyer_asleep_footer", None, db_path=db_path)
    if custom and custom.strip():
        return custom.strip()
    env = os.environ.get("BUYER_ASLEEP_FOOTER", "").strip()
    if env:
        return env
    return "Buyer away, back shortly"


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


# ---------------------------------------------------------------------------
# AI analysis cache â€” fingerprint-keyed so identical listing content is never
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


# ---------------------------------------------------------------------------
# Destinations — groups/chats that receive forwarded bot publications.
# Conceptually the forwarding twin of sources: same management semantics
# (add / list / enable-disable / remove) but used purely as forward targets.
# ---------------------------------------------------------------------------


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


def reset_unresolved_forwardings(db_path: Optional[str] = None) -> int:
    """Reset forwardings that were blocked by entity resolution string errors.

    When destinations or channels were stored as strings like '-100...',
    Telethon failed with 'Cannot find any entity corresponding to ...'.
    Resetting them to 'pending' with retry_count=0 and retry_at=NULL allows
    the forwarder to retry them with proper integer peer resolution immediately.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE forwardings
            SET status = 'pending', retry_count = 0, retry_at = NULL, updated_at = ?
            WHERE error LIKE '%Cannot find any entity%'
               OR error LIKE '%Could not find the input entity%'
            """,
            (now_iso,),
        )
        return cur.rowcount


def _destination_chat_ref(chat_id) -> Optional[str]:
    """Canonical destination peer reference: marked numeric id as TEXT, or '@username'.

    Numeric ids (int or numeric string) are normalized to the -100 marked form;
    '@handle' strings pass through lowercased. Anything else is rejected, so a
    typo never becomes an unreachable destination row.
    """
    if isinstance(chat_id, str):
        ref = (chat_id or "").strip()
        if ref.startswith("@"):
            if len(ref) > 1 and all(c.isalnum() or c == "_" for c in ref[1:]):
                return ref.lower()
            return None
        marked = normalize_channel_id(ref)
        return str(marked) if marked is not None else None
    marked = normalize_channel_id(chat_id)
    return str(marked) if marked is not None else None


def add_destination(
    chat_id,
    title: Optional[str] = None,
    active: bool = True,
    db_path: Optional[str] = None,
) -> int:
    """Register a destination chat (upsert by chat_id) and return its id.

    Mirrors add_supplier: the reference ('-100...' marked id or '@username') is
    the identity; a re-add reactivates/updates the existing row instead of
    duplicating it. Raises ValueError for references that can't be a real peer.
    """
    chat_ref = _destination_chat_ref(chat_id)
    if chat_ref is None:
        raise ValueError("destination requires a valid Telegram chat id or @username")
    name = (title or "").strip()[:100]
    active_int = 1 if active else 0
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO destinations (chat_id, title, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (chat_ref, name, active_int, now_iso, now_iso),
        )
        row = conn.execute(
            "SELECT id FROM destinations WHERE chat_id = ?", (chat_ref,)
        ).fetchone()
        return row["id"] if row else None


def set_destination_active(
    destination_id: int, active: bool, db_path: Optional[str] = None
) -> bool:
    """Enable/disable a destination by id. Returns True if a row was updated."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE destinations SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, now_iso, destination_id),
        )
        return cursor.rowcount > 0


def delete_destination(destination_id: int, db_path: Optional[str] = None) -> bool:
    """Permanently remove a destination. Forwarding history rows are kept.

    Rows already queued for a deleted destination stay in forwardings as-is
    (the queue join to destinations simply stops matching them); historical
    'forwarded' records are preserved for the audit trail.
    """
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM destinations WHERE id = ?", (destination_id,)
        )
        return cursor.rowcount > 0


def list_destinations(
    active_only: bool = False, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List all configured destinations, oldest first."""
    query = "SELECT * FROM destinations"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id ASC"
    with db_session(db_path) as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_destination_by_id(
    destination_id: int, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return one destination row by its primary key."""
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM destinations WHERE id = ?", (destination_id,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Forwardings queue — persistent, idempotent work list for destination forwards.
# ---------------------------------------------------------------------------

FORWARD_MAX_RETRIES = 8
FORWARD_RETRY_BASE_SECONDS = 30
FORWARD_RETRY_CAP_SECONDS = 3600


def _forward_retry_at(retry_count: int) -> str:
    """Wall-clock time after which a transient failure may be retried.

    Exponential backoff capped at one hour so a destination that is briefly
    down is retried soon, but the worker never hot-loops on it. The next retry
    only becomes due after `retry_count` failures, so SQLite never thrashes.
    """
    delay = min(
        FORWARD_RETRY_BASE_SECONDS * (2 ** max(retry_count - 1, 0)),
        FORWARD_RETRY_CAP_SECONDS,
    )
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def queue_forwarding(
    listing_id: int,
    published_chat_id,
    published_message_id: int,
    db_path: Optional[str] = None,
) -> int:
    """Record pending forwarding work for ONE bot-published message.

    Called ONLY from the successful-publication path (never from channel
    monitoring), one row is created per ACTIVE destination. Re-queuing is a
    no-op (UNIQUE(published_chat_id, published_message_id, destination_id)) so
    publish bookkeeping retries and process restarts can never forward twice.
    Returns the number of new rows created (0 when everything was already
    queued or no destinations are enabled).
    """
    destinations = list_destinations(active_only=True, db_path=db_path)
    if not destinations:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    with db_session(db_path) as conn:
        for dest in destinations:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO forwardings
                    (listing_id, published_chat_id, published_message_id,
                     destination_id, status, retry_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    listing_id,
                    str(published_chat_id),
                    int(published_message_id),
                    dest["id"],
                    now_iso,
                    now_iso,
                ),
            )
            created += cur.rowcount
    return created


def get_pending_forwardings(
    limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Rows ready to forward now, oldest first, only for ACTIVE destinations.

    'Ready' means rate-limit/backoff (retry_at) is in the past or unset. A row
    whose destination was deleted or disabled stops matching the JOIN, so the
    worker never forwards to a chat the admin turned off.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT f.*, d.chat_id AS destination_chat_id, d.title AS destination_title
            FROM forwardings f
            JOIN destinations d ON d.id = f.destination_id
            WHERE f.status = 'pending'
              AND d.active = 1
              AND (f.retry_at IS NULL OR f.retry_at <= ?)
            ORDER BY f.id ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_forwarded(forwarding_id: int, db_path: Optional[str] = None) -> None:
    """Record a successfully forwarded message."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE forwardings
            SET status = 'forwarded', error = NULL, retry_at = NULL,
                forwarded_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_iso, now_iso, forwarding_id),
        )


def mark_forward_failed(
    forwarding_id: int,
    error: str,
    permanent: bool = False,
    db_path: Optional[str] = None,
) -> None:
    """Record a failed forward.

    Permanent failures (permission errors, deleted/invalid chat, bot removed)
    are failed immediately and are never retried. Transient failures bump the
    retry count and stay 'pending' with a backoff (retry_at); after
    FORWARD_MAX_RETRIES they are marked failed permanently so a dead destination
    does not grind the queue forever.
"""
    now_iso = datetime.now(timezone.utc).isoformat()
    if not permanent:
        with db_session(db_path) as conn:
            row = conn.execute(
                "SELECT retry_count FROM forwardings WHERE id = ?", (forwarding_id,)
            ).fetchone()
            retry_count = (row["retry_count"] if row else 0) + 1
        if retry_count < FORWARD_MAX_RETRIES:
            with db_session(db_path) as conn:
                conn.execute(
                    """
                    UPDATE forwardings
                    SET status = 'pending', error = ?, retry_count = ?, retry_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (error[:500], retry_count, _forward_retry_at(retry_count), now_iso, forwarding_id),
                )
            return
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE forwardings
            SET status = 'failed', error = ?, updated_at = ?
            WHERE id = ?
            """,
            (error[:500], now_iso, forwarding_id),
        )


def claim_forwarding(forwarding_id: int, db_path: Optional[str] = None) -> bool:
    """Atomically claim one pending forwarding row before the external forward.

    The transient status 'forwarding' is the once-only claim (F6): the guard
    ``status = 'pending'`` plus the backoff check means a second worker or a
    post-crash retry sees the row non-claimable and cannot forward it again.
    Returns True only for the single winner.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE forwardings
            SET status = 'forwarding', updated_at = ?
            WHERE id = ? AND status = 'pending'
              AND (retry_at IS NULL OR retry_at <= ?)
            """,
            (now_iso, forwarding_id, now_iso),
        )
        return cursor.rowcount > 0


def reset_stale_forward_claims(
    grace_seconds: int = 300, db_path: Optional[str] = None
) -> int:
    """Release forward rows stuck in 'forwarding' (crash between claim + forward).

    Runs at startup. Rows are restored to 'pending' with ``error`` set to the
    marker ``'stale_claim_reset'`` so the drain loop KNOWS a previous attempt
    may have actually landed and must VERIFY against the destination (by
    ``fwd_from.channel_post``) before forwarding again — a blind resend could
    duplicate a forward that succeeded right before the crash.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE forwardings
            SET status = 'pending', error = 'stale_claim_reset',
                retry_at = NULL, updated_at = ?
            WHERE status = 'forwarding' AND updated_at <= ?
            """,
            (now_iso, cutoff),
        )
        return cur.rowcount