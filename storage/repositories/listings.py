"""Listings lifecycle, quarantine, deduplication, and publishing claims repository."""

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

from .base import DEFAULT_DB_PATH, db_session, run_async, _SOURCE_HWM_PREFIX

# ---------------------------------------------------------------------------
# Fingerprinting (canonical, shared between store + dedup lookup)
# ---------------------------------------------------------------------------
# In-flight 'received' rows only match each other within this short window, so
# a row stranded in 'received' (e.g. after a hard kill) cannot blind duplicate
# detection for the full dedup window (CONC-2).
_RECEIVED_DEDUP_MINUTES = 30

# REJECT-MEM: how long an admin rejection keeps blocking the same fingerprint.
# Separate from (and much shorter than) the publish dedup window, because a
# rejection is a judgement call about ONE post, not a standing statement that
# the content is worthless for a whole day. Overridable via the environment so
# it can be tuned without a code change; main.py owns the live value.
_REJECT_MEMORY_MINUTES = int(os.environ.get("REJECT_MEMORY_MINUTES", "180") or 180)

# PENDING-MAX-AGE: Maximum lifetime for pending approval / review listings (in hours).
# Listings older than this threshold are auto-expired to 'skipped_expired' so they
# do not linger in the review queue indefinitely.
PENDING_MAX_AGE_HOURS: float = float(os.environ.get("PENDING_MAX_AGE_HOURS", "4") or 4)

# QUARANTINE: statuses that represent a listing the system is actively holding
# or has decided about. A held listing blocks nothing on its own — only the
# atomic claim turns 'held' into a decision — but it must be in this set so a
# twin arriving during the hold window is visible to the claim's NOT EXISTS.
_ACTIVE_DEDUP_STATUSES = (
    "held",
    "claimed",
    "publishing",
    "published",
    "pending_approval",
    "pending_review",
    "approved",
)

DEDUP_IGNORE_PRICE = os.environ.get("DEDUP_IGNORE_PRICE", "1").strip().lower() in ("1", "true", "yes")

def make_listing_fingerprint(
    clean_text: str,
    price: Optional[float] = None,
    platform: Optional[str] = None,
    ignore_price: Optional[bool] = None,
) -> str:
    """Hash of the normalized listing content.

    When DEDUP_IGNORE_PRICE=1 (default), price is omitted and numbers that look
    like prices are removed so re-posts with changed prices or formatting are
    recognized as duplicates. When DEDUP_IGNORE_PRICE=0, price is folded in.
    """
    if ignore_price is None:
        should_ignore = os.environ.get("DEDUP_IGNORE_PRICE", "1").strip().lower() in ("1", "true", "yes")
    else:
        should_ignore = ignore_price
    norm = unicodedata.normalize("NFKC", (clean_text or "").strip().lower())
    # DEDUP-FN: collapse the punctuation variants that are visually identical to
    # readers — em/en/2-em dashes and the minus sign become '-', smart quotes
    # become straight ASCII, non-breaking spaces fold to a space.
    norm = norm.replace("\u2212", "-").replace("--", "-")
    norm = norm.replace("\u2018", "'").replace("\u2019", "'")
    norm = norm.replace("\u201c", '"').replace("\u201d", '"')
    norm = re.sub(r"[\s\u00a0]+", " ", norm)

    if should_ignore:
        # Lowercased, whitespace and punctuation collapsed, numbers that look like prices removed
        norm = re.sub(r"(?:[\$€£]|usd|usdt|eur)\s*\d+[\d.,]*", "", norm, flags=re.IGNORECASE)
        norm = re.sub(r"\d+[\d.,]*\s*(?:[\$€£]|usd|usdt|eur)\b", "", norm, flags=re.IGNORECASE)
        norm = re.sub(r"\b(?:price|rate|cost|budget)\s*[:=]?\s*\d+[\d.,]*", "", norm, flags=re.IGNORECASE)
        norm = re.sub(r"[^\w\s]", " ", norm)
        norm = re.sub(r"\s+", " ", norm).strip()
        if platform:
            norm = f"{platform.strip().lower()} {norm}"
    else:
        if price is not None:
            norm = f"{norm} | {price:g}"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()

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
    hold_until: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Insert a captured listing record (idempotent per supplier+message).

    ``fingerprint`` must be set at insert time (CONC-2 race fix): the dedup
    query reads it from the DB, so writing it later left a window in which two
    identical concurrent messages could both pass the duplicate check.

    ``hold_until`` (QUARANTINE) marks the row as one the held-listings worker
    must wait for before evaluating. It is stored rather than kept in memory so
    a restart cannot lose the hold and re-introduce the publish-before-twins-
    arrive race.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO listings (
                supplier_id, source_message_id, game_name, rank_tier,
                status, raw_text, clean_text,
                fingerprint, published_message_id, created_at, updated_at,
                hold_until
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                hold_until,
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
    header_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> None:
    """Persist parsed listing fields (platform) and the assigned header.

    ``header_id`` is the sticky custom-emoji header chosen for this listing (see
    resolve_header_for_listing). Every argument is coalesce()'d so a caller that
    only knows one field can never clobber the others.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE listings
            SET platform_name = coalesce(?, platform_name),
                intent = coalesce(?, intent),
                header_id = coalesce(?, header_id),
                updated_at = ?
            WHERE id = ?
            """,
            (platform_name, intent, header_id, now_iso, listing_id),
        )

def set_listing_fingerprint(
    listing_id: int,
    clean_text: str,
    price: Optional[float] = None,
    platform: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Store the content dedup fingerprint."""
    with db_session(db_path) as conn:
        if platform is None:
            r = conn.execute("SELECT platform_name FROM listings WHERE id = ?", (listing_id,)).fetchone()
            if r:
                platform = r["platform_name"]
        fingerprint = make_listing_fingerprint(clean_text, price=price, platform=platform)
        conn.execute(
            "UPDATE listings SET fingerprint = ? WHERE id = ?",
            (fingerprint, listing_id),
        )

def update_listing_raw_text(
    listing_id: int, raw_text: str, db_path: Optional[str] = None
) -> None:
    """Update raw_text and updated_at for an edited listing."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            "UPDATE listings SET raw_text = ?, updated_at = ? WHERE id = ?",
            (raw_text, now_iso, listing_id),
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
    """Fetch a published or sold post by its sequential post number, joined with its supplier."""
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.channel_id as supplier_channel_id,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status IN ('published', 'sold')
          AND l.published_message_id IS NOT NULL
          AND l.post_number = ?
    """
    with db_session(db_path) as conn:
        row = conn.execute(query, (post_number,)).fetchone()
        return dict(row) if row else None

def get_last_source_message_id(
    supplier_id: Optional[int], db_path: Optional[str] = None
) -> int:
    """Highest source message id already recorded for a supplier (for backfill).

    Also consults the high-water mark saved by purge_expired_data(): once old
    listings are deleted, MAX(source_message_id) alone would fall back to 0 and
    the backfill sweep would re-ingest (and re-publish) old history."""
    if supplier_id is None:
        return 0
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(source_message_id), 0) FROM listings WHERE supplier_id = ?",
            (supplier_id,),
        ).fetchone()
        hwm_row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (f"{_SOURCE_HWM_PREFIX}{supplier_id}",),
        ).fetchone()
        try:
            hwm = int(hwm_row["value"]) if hwm_row else 0
        except (TypeError, ValueError):
            hwm = 0
        return max(int(row[0]), hwm)

def find_recent_similar_listing(
    clean_text: str,
    hours: int = 8,
    price: Optional[float] = None,
    exclude_listing_id: Optional[int] = None,
    reject_memory_minutes: Optional[int] = None,
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

    REJECT-MEM: rows the admin REJECTED (or skipped via the admin's skip button)
    are ALSO matched, for their own shorter ``reject_memory_minutes`` window.
    Previously they were invisible to dedup, which meant rejecting a post erased
    it from memory entirely and the very next identical copy published instead of
    being caught — the "two buyers post the same thing a minute apart and both go
    out" failure. A rejection is a deliberate decision and must block like a
    publish does, so it uses a separate short window rather than the full
    ``hours`` (a mistaken rejection should not silence a listing all day).
    """
    if not (clean_text or "").strip():
        return None

    fingerprint = make_listing_fingerprint(clean_text, price=price)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat()
    received_cutoff = (now - timedelta(minutes=_RECEIVED_DEDUP_MINUTES)).isoformat()
    if reject_memory_minutes is None:
        reject_memory_minutes = _REJECT_MEMORY_MINUTES
    reject_cutoff = (now - timedelta(minutes=reject_memory_minutes)).isoformat()

    query = """
        SELECT * FROM listings
        WHERE created_at >= ?
          AND fingerprint = ?
          AND (
                status IN ('published', 'pending_approval', 'pending_review', 'approved')
                OR (status = 'received' AND created_at >= ?)
                OR (status IN ('rejected', 'skipped_admin') AND created_at >= ?)
              )
    """
    params: List[Any] = [cutoff, fingerprint, received_cutoff, reject_cutoff]
    if exclude_listing_id is not None:
        query += " AND id != ?"
        params.append(exclude_listing_id)
    query += " ORDER BY id DESC LIMIT 1"

    with db_session(db_path) as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

def get_held_listings_due(
    limit: int = 10, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Held listings whose quarantine has expired, oldest arrival first.

    Ordering by id is what makes the winner of a burst deterministic: the
    first-arrived copy is always claimed first, so the suppressed twins can name
    it as their ``duplicate_of``. Rows stranded in 'held' by a hard kill are
    already past ``hold_until``, so the next tick picks them up with no separate
    repair path needed.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.*,
                   s.channel_username AS supplier_username,
                   s.display_name     AS supplier_display_name
            FROM listings l
            LEFT JOIN suppliers s ON l.supplier_id = s.id
            WHERE l.status = 'held'
              AND l.hold_until IS NOT NULL
              AND l.hold_until <= ?
            ORDER BY l.id ASC
            LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

def claim_held_listing(
    listing_id: int, hours: int = 8, db_path: Optional[str] = None
) -> Optional[int]:
    """Atomically claim a held listing for evaluation, or report the winner.

    Returns the listing's own id when the claim WON (proceed with filtering,
    AI and publishing), or the older twin's id when it LOST — in which case the
    caller must mark this listing ``skipped_duplicate`` and must NOT publish it.

    The single-statement ``UPDATE ... WHERE NOT EXISTS`` IS the claim. SQLite
    runs one UPDATE as one write transaction, so two concurrent claims on the
    same fingerprint serialize: the second sees the first's committed row and its
    NOT EXISTS is satisfied, so its rowcount is 0. This replaces the old
    read-then-write check, where a sync read was followed by an await-ed write
    and a twin arriving in that gap published too.

    Only strictly OLDER rows (``x.id < listings.id``) can beat a listing, so the
    earliest arrival in any burst always wins and the outcome does not depend on
    drain order or timing.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    placeholders = ",".join("?" for _ in _ACTIVE_DEDUP_STATUSES)
    with db_session(db_path) as conn:
        cursor = conn.execute(
            f"""
            UPDATE listings
            SET status = 'claimed', updated_at = ?, received_at = ?
            WHERE id = ?
              AND status = 'held'
              AND (
                    fingerprint IS NULL
                    OR NOT EXISTS (
                        SELECT 1 FROM listings x
                        WHERE x.fingerprint = listings.fingerprint
                          AND x.id < listings.id
                          AND x.created_at >= ?
                          AND x.status IN ({placeholders})
                      )
                  )
            """,
            (now_iso, now_iso, listing_id, cutoff, *_ACTIVE_DEDUP_STATUSES),
        )
        if cursor.rowcount > 0:
            # Quarantine guarantee: immediately mark any other concurrent 'held'
            # rows with the same fingerprint as skipped_duplicate, even if their
            # hold_until has not expired yet.
            row_fp = conn.execute(
                "SELECT fingerprint FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if row_fp and row_fp["fingerprint"]:
                conn.execute(
                    """
                    UPDATE listings
                    SET status = 'skipped_duplicate',
                        duplicate_of = ?,
                        updated_at = ?
                    WHERE fingerprint = ?
                      AND id != ?
                      AND status = 'held'
                    """,
                    (listing_id, now_iso, row_fp["fingerprint"], listing_id),
                )
            return listing_id

        row = conn.execute(
            """
            SELECT id FROM listings
            WHERE fingerprint = (
                    SELECT fingerprint FROM listings WHERE id = ?
                 )
              AND id < ?
              AND created_at >= ?
              AND status IN (%s)
            ORDER BY id ASC
            LIMIT 1
            """
            % placeholders,
            (listing_id, listing_id, cutoff, *_ACTIVE_DEDUP_STATUSES),
        ).fetchone()
        return row["id"] if row else None

def mark_duplicate_of(
    listing_id: int, winner_id: int, db_path: Optional[str] = None
) -> None:
    """Record that ``listing_id`` lost the dedup claim to ``winner_id``."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            "UPDATE listings SET status = 'skipped_duplicate', duplicate_of = ?, "
            "updated_at = ? WHERE id = ?",
            (winner_id, now_iso, listing_id),
        )

def get_duplicate_suppressed_ids(
    winner_id: int, db_path: Optional[str] = None
) -> List[int]:
    """Ids of the twins that were suppressed in favour of ``winner_id``.

    Lets the alert name the whole burst ("published #1, dropped #2 #3 #4") so a
    five-post burst produces ONE admin message instead of five separate
    notifications — which is what flooded the review queue.
    """
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM listings WHERE duplicate_of = ? ORDER BY id ASC",
            (winner_id,),
        ).fetchall()
        return [r["id"] for r in rows]

def assert_still_unique(
    listing_id: int, hours: int = 8, db_path: Optional[str] = None
) -> Tuple[bool, Optional[int]]:
    """Final pre-publish duplicate guard. Returns ``(ok, winner_id)``.

    Dedup historically ran at exactly ONE place: ingest. Nothing re-checked
    before an actual publish, so a listing that sat in the review queue for
    hours — or was revived from the skipped list by restore_skipped_to_pending
    — would publish with no duplicate check at all, even if an identical copy
    had already shipped in the meantime.

    Both publish paths (the approved-listings worker and the admin's approve
    button) call this immediately before sending and fail closed: on ``False``
    the listing is marked ``skipped_duplicate`` instead of published.

    Only COMMITTED states block — ``claimed``/``publishing``/``published``, i.e.
    content that is out or provably on its way out. Two rows merely awaiting the
    admin do not block each other: that is a legitimate review queue, and
    blocking on it would wedge the queue so an approved listing could never
    publish. Arrival order is deliberately NOT considered — a copy that published
    while this listing sat in review always has a higher id than it, so
    restricting the search to newer rows would miss precisely the case this
    guard exists to catch.
    """
    committed = ("claimed", "publishing", "published")
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT fingerprint FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            return False, None
        fingerprint = row["fingerprint"]
        if not fingerprint:
            return True, None
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        placeholders = ",".join("?" for _ in committed)
        twin = conn.execute(
            f"""
            SELECT id FROM listings
            WHERE fingerprint = ?
              AND id != ?
              AND created_at >= ?
              AND status IN ({placeholders})
            ORDER BY id ASC
            LIMIT 1
            """,
            (fingerprint, listing_id, cutoff, *committed),
        ).fetchone()
        if twin is None:
            return True, None
        return False, twin["id"]

def get_earlier_duplicate_twin(
    listing_id: int, hours: int = 8, db_path: Optional[str] = None
) -> Optional[int]:
    """Id of an identical listing already COMMITTED (claimed/publishing/published).

    Same twin rule as ``assert_still_unique``, but returns the id instead of a
    (ok, id) pair. Used to tell a *failed publish claim* apart from a *duplicate*:
    ``claim_listing_for_publish`` now returns False for both "another path already
    owns this row" and "an identical copy won the race to the channel", and only
    the second one deserves a skipped_duplicate verdict and an admin alert.

    Returns None when no committed twin exists (a plain claim race) and when the
    listing has no fingerprint (null fingerprints never dedup).
    """
    committed = ("claimed", "publishing", "published")
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT fingerprint FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            return None
        fingerprint = row["fingerprint"]
        if not fingerprint:
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        placeholders = ",".join("?" for _ in committed)
        twin = conn.execute(
            f"""
            SELECT id FROM listings
            WHERE fingerprint = ?
              AND id != ?
              AND created_at >= ?
              AND status IN ({placeholders})
            ORDER BY id ASC
            LIMIT 1
            """,
            (fingerprint, listing_id, cutoff, *committed),
        ).fetchone()
        return twin["id"] if twin else None

def expire_stale_pending_listings(
    max_age_hours: Optional[float] = None,
    db_path: Optional[str] = None,
) -> int:
    """Transition pending listings older than max_age_hours to 'skipped_expired'.

    Records skip logs (reason='expired') and audit log entries.
    Returns the number of listings expired.
    """
    hours = PENDING_MAX_AGE_HOURS if max_age_hours is None else float(max_age_hours)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.id,
                   CASE WHEN s.id IS NOT NULL THEN l.supplier_id ELSE NULL END AS supplier_id,
                   l.source_message_id, l.raw_text, l.clean_text
            FROM listings l
            LEFT JOIN suppliers s ON l.supplier_id = s.id
            WHERE l.status IN ('pending_approval', 'pending_review', 'received', 'claimed')
              AND l.created_at < ?
            """,
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0

        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE listings
            SET status = 'skipped_expired',
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (now_iso, *ids),
        )
        for row in rows:
            raw_text = (row["raw_text"] or row["clean_text"] or "")[:4000]
            conn.execute(
                """
                INSERT INTO skips (supplier_id, message_id, reason, raw_text, timestamp)
                VALUES (?, ?, 'expired', ?, ?)
                """,
                (row["supplier_id"], row["source_message_id"], raw_text, now_iso),
            )
            conn.execute(
                """
                INSERT INTO audit_log (action, listing_id, actor_id, detail, created_at)
                VALUES ('pending_expired', ?, NULL, ?, ?)
                """,
                (row["id"], f"expired after {hours}h", now_iso),
            )
        return len(ids)

def skip_all_pending_listings(
    actor_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> int:
    """Transition ALL currently pending listings to 'skipped_admin'.

    Records audit logs and skips table entries for every listing skipped.
    Returns the number of listings skipped.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT l.id,
                   CASE WHEN s.id IS NOT NULL THEN l.supplier_id ELSE NULL END AS supplier_id,
                   l.source_message_id, l.raw_text, l.clean_text
            FROM listings l
            LEFT JOIN suppliers s ON l.supplier_id = s.id
            WHERE l.status IN ('pending_approval', 'pending_review')
            """
        ).fetchall()
        if not rows:
            return 0

        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE listings
            SET status = 'skipped_admin',
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (now_iso, *ids),
        )
        for row in rows:
            raw_text = (row["raw_text"] or row["clean_text"] or "")[:4000]
            conn.execute(
                """
                INSERT INTO skips (supplier_id, message_id, reason, raw_text, timestamp)
                VALUES (?, ?, 'admin_skip', ?, ?)
                """,
                (row["supplier_id"], row["source_message_id"], raw_text, now_iso),
            )
            conn.execute(
                """
                INSERT INTO audit_log (action, listing_id, actor_id, detail, created_at)
                VALUES ('skipped_admin', ?, ?, 'skip_all_pending', ?)
                """,
                (row["id"], actor_id, now_iso),
            )
        return len(ids)

def get_pending_listings(
    limit: int = 10,
    offset: int = 0,
    db_path: Optional[str] = None,
    max_age_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Retrieve listings pending admin approval (auto-expires listings > max_age_hours old)."""
    hours = PENDING_MAX_AGE_HOURS if max_age_hours is None else float(max_age_hours)
    expire_stale_pending_listings(hours, db_path=db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    query = """
        SELECT l.*, s.channel_username as supplier_username,
               s.display_name as supplier_display_name
        FROM listings l
        LEFT JOIN suppliers s ON l.supplier_id = s.id
        WHERE l.status IN ('pending_approval', 'pending_review')
          AND l.created_at >= ?
        ORDER BY l.id ASC
        LIMIT ? OFFSET ?
    """
    with db_session(db_path) as conn:
        rows = conn.execute(query, (cutoff, limit, offset)).fetchall()
        return [dict(row) for row in rows]

def count_pending_listings(
    db_path: Optional[str] = None,
    max_age_hours: Optional[float] = None,
) -> int:
    """Total listings pending approval (auto-expires listings > max_age_hours old)."""
    hours = PENDING_MAX_AGE_HOURS if max_age_hours is None else float(max_age_hours)
    expire_stale_pending_listings(hours, db_path=db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db_session(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM listings
            WHERE status IN ('pending_approval', 'pending_review')
              AND created_at >= ?
            """,
            (cutoff,),
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
    listing_id: int,
    post_number: int,
    hours: int = 8,
    db_path: Optional[str] = None,
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

    DEDUP (concurrency): the claim is ALSO the authoritative final duplicate
    gate. ``assert_still_unique`` used to be a separate read that both publish
    paths called just before this claim, leaving a TOCTOU window: two paths
    (admin Approve + the approved worker) could both read "no twin" and then both
    claim and send. Folding the twin check into this one UPDATE makes the
    decision atomic — SQLite serialises the write, so whichever transaction lands
    first moves its row to 'publishing' and the loser's NOT EXISTS sees it and
    returns False. The loser is then marked skipped_duplicate and never sent.

    Only COMMITTED states block (see _ACTIVE_DEDUP_STATUSES); two rows merely
    awaiting the admin do not block each other, matching assert_still_unique so
    the pre-check and the gate can never disagree about a legitimate queue.

    Returns True only for the single winner.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    committed = ("claimed", "publishing", "published")
    placeholders = ",".join("?" for _ in committed)
    with db_session(db_path) as conn:
        cursor = conn.execute(
            f"""
            UPDATE listings
            SET status = 'publishing',
                post_number = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('approved', 'pending_approval', 'pending_review', 'claimed', 'received')
              AND published_message_id IS NULL
              AND NOT EXISTS (
                    SELECT 1 FROM listings AS twin
                    WHERE twin.fingerprint IS NOT NULL
                      AND twin.fingerprint = listings.fingerprint
                      AND twin.id != listings.id
                      AND twin.created_at >= ?
                      AND twin.status IN ({placeholders})
              )
            """,
            (post_number, now_iso, listing_id, cutoff, *committed),
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

    Backed by the 'post_counter' counter in app_settings (migrated in v13).
    Incremented atomically in a single transaction; never derived from MAX()
    on listings and survives all database purges.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path, immediate=True) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('post_counter', '0', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (now_iso,),
        )
        row_seq = conn.execute("SELECT value FROM app_settings WHERE key = 'post_seq'").fetchone()
        row_ctr = conn.execute("SELECT value FROM app_settings WHERE key = 'post_counter'").fetchone()
        if row_seq and row_seq["value"]:
            try:
                seq_val = int(row_seq["value"])
                ctr_val = int(row_ctr["value"]) if row_ctr and row_ctr["value"] else 0
                if seq_val > ctr_val:
                    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'post_counter'", (str(seq_val),))
            except ValueError:
                pass
        conn.execute(
            "UPDATE app_settings SET value = CAST(value AS INTEGER) + 1, updated_at = ? "
            "WHERE key = 'post_counter'",
            (now_iso,),
        )
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'post_counter'"
        ).fetchone()
        val = int(row["value"])
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('post_seq', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?",
            (str(val), now_iso, str(val), now_iso),
        )
        return val

def claim_stale_received_listing(
    stale_threshold_seconds: int = 120, limit: int = 5, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Find listings in 'received' or 'claimed' status older than stale_threshold_seconds and atomically touch them."""
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    cutoff = (now_dt - timedelta(seconds=stale_threshold_seconds)).isoformat()
    max_age_cutoff = (now_dt - timedelta(hours=PENDING_MAX_AGE_HOURS)).isoformat()
    with db_session(db_path, immediate=True) as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE status IN ('received', 'claimed')
              AND (created_at IS NULL OR created_at >= ?)
              AND (
                  (received_at IS NOT NULL AND received_at <= ?)
                  OR (received_at IS NULL AND updated_at <= ?)
              )
            ORDER BY id ASC
            LIMIT ?
            """,
            (max_age_cutoff, cutoff, cutoff, limit),
        ).fetchall()
        claimed = []
        for r in rows:
            cur = conn.execute(
                """
                UPDATE listings
                SET received_at = ?, updated_at = ?
                WHERE id = ?
                  AND status IN ('received', 'claimed')
                  AND (created_at IS NULL OR created_at >= ?)
                  AND (
                      (received_at IS NOT NULL AND received_at <= ?)
                      OR (received_at IS NULL AND updated_at <= ?)
                  )
                """,
                (now_iso, now_iso, r["id"], max_age_cutoff, cutoff, cutoff),
            )
            if cur.rowcount > 0:
                claimed.append(dict(r))
        return claimed

def recompute_active_fingerprints(hours: int = 8, db_path: Optional[str] = None) -> int:
    """Recompute dedup fingerprints for listings active in the last `hours` window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT id, raw_text, clean_text, platform_name FROM listings WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        count = 0
        for r in rows:
            txt = r["clean_text"] or r["raw_text"] or ""
            fp = make_listing_fingerprint(txt, platform=r["platform_name"])
            conn.execute("UPDATE listings SET fingerprint = ? WHERE id = ?", (fp, r["id"]))
            count += 1
        return count

# ---------------------------------------------------------------------------
# Strongly-typed domain model queries (models.py)
# ---------------------------------------------------------------------------
def get_listing_item(listing_id: int, db_path: Optional[str] = None) -> Optional[models.ListingItem]:
    """Fetch a listing by ID as a strongly-typed ListingItem dataclass."""
    raw = get_listing_by_id(listing_id, db_path=db_path)
    return models.ListingItem.from_row(raw) if raw else None

def mark_listing_sold(listing_id: int, db_path: Optional[str] = None) -> bool:
    """Mark a listing as sold."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE listings
            SET status = 'sold', updated_at = ?
            WHERE id = ?
            """,
            (now_iso, listing_id),
        )
        return cur.rowcount > 0
