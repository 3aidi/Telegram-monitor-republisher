"""Audit logs, skip logs, settings, and AI caching repository."""

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

def record_blocklist_hit(
    listing_id: int, matched_keyword: str, db_path: Optional[str] = None
) -> None:
    """Record illicit/stolen account keyword match."""
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO blocklist_hits (listing_id, matched_keyword) VALUES (?, ?)",
            (listing_id, matched_keyword),
        )

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

        errors_count = conn.execute(
            "SELECT count(*) FROM listings WHERE created_at >= ? AND status = 'failed'",
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
        "errors": errors_count,
        "skip_reasons": skipped_reasons,
        "supplier_breakdown": supplier_breakdown,
    }

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
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
        except sqlite3.OperationalError:
            return default

def delete_setting(key: str, db_path: Optional[str] = None) -> None:
    """Delete a key/value setting."""
    with db_session(db_path) as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))

def delete_settings_prefix(prefix: str, db_path: Optional[str] = None) -> None:
    """Delete all settings matching a key prefix."""
    with db_session(db_path) as conn:
        conn.execute("DELETE FROM app_settings WHERE key LIKE ?", (f"{prefix}%",))

def get_all_settings(prefix: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, str]:
    """Retrieve all key/value settings, optionally matching a key prefix."""
    with db_session(db_path) as conn:
        if prefix:
            rows = conn.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE ?", (f"{prefix}%",)
            ).fetchall()
        else:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

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

def get_dest_channel(default: Optional[str] = None, db_path: Optional[str] = None) -> str:
    """Retrieve the main destination channel from app_settings, falling back to default or env."""
    val = get_setting("dest_channel", None, db_path=db_path)
    if val and val.strip():
        return val.strip()
    return (default or os.environ.get("DEST_CHANNEL", "") or "").strip()

def set_dest_channel(channel: str, db_path: Optional[str] = None) -> str:
    """Persist the main destination channel in app_settings.

    Accepts @usernames, numeric IDs (-100...), or bare strings.
    Normalizes numeric IDs and strips leading/trailing whitespace.
    """
    clean = (channel or "").strip()
    if not clean:
        raise ValueError("Destination channel cannot be empty")
    if is_numeric_identifier(clean):
        clean = str(normalize_channel_id(int(clean)))
    elif clean.startswith("https://t.me/") or clean.startswith("t.me/"):
        part = clean.rstrip("/").split("/")[-1]
        if part and not part.startswith("+"):
            clean = f"@{part.lstrip('@')}"

    set_setting("dest_channel", clean, db_path=db_path)
    return clean

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
# RETENTION: rolling time window. Everything message-related older than the
# window is deleted — published, skipped, pending review, rejected, failed —
# so every admin list (Published / Skipped / Pending) only ever shows the last
# N hours. Configuration is NEVER touched: suppliers, destinations, headers and
# app_settings (incl. the post_seq counter, so #numbers keep counting up).
#
# Only rows that are physically mid-send are spared ('claimed'/'publishing'
# listings, 'forwarding' forwards): deleting those under a running worker could
# lose the bookkeeping of a message that is landing right now. They are swept on
# the next pass once they settle.
#
# Dedup compares against listings.created_at within DEDUP_HOURS, so as long as
# the retention window is >= DEDUP_HOURS (main.py enforces this) the purge can
# never delete a row the duplicate check still needs.
# ---------------------------------------------------------------------------
_RETENTION_INFLIGHT_LISTING_STATUSES = ("claimed", "publishing")
# app_settings key prefix for the per-supplier backfill high-water mark.
_SOURCE_HWM_PREFIX = "src_hwm:"

def purge_expired_data(
    max_age_hours: float, db_path: Optional[str] = None
) -> Dict[str, int]:
    """Delete all message data older than ``max_age_hours``.

    Returns a per-table count of deleted rows. Runs as ONE transaction so a
    crash mid-purge leaves the database either untouched or fully purged.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat()
    inflight = ",".join("?" for _ in _RETENTION_INFLIGHT_LISTING_STATUSES)
    expired_listings_sql = (
        f"SELECT id FROM listings WHERE created_at < ? "
        f"AND status NOT IN ({inflight})"
    )
    expired_params = (cutoff, *_RETENTION_INFLIGHT_LISTING_STATUSES)
    counts: Dict[str, int] = {}
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        # Save each supplier's highest seen source message id BEFORE deleting,
        # so the backfill sweep keeps resuming after it (see
        # get_last_source_message_id). Only ever moves forward.
        for hw in conn.execute(
            """
            SELECT supplier_id, MAX(source_message_id) AS mx FROM listings
            WHERE supplier_id IS NOT NULL GROUP BY supplier_id
            """
        ).fetchall():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = CASE WHEN CAST(excluded.value AS INTEGER)
                                      > CAST(app_settings.value AS INTEGER)
                                 THEN excluded.value ELSE app_settings.value END,
                    updated_at = excluded.updated_at
                """,
                (f"{_SOURCE_HWM_PREFIX}{hw['supplier_id']}", str(int(hw["mx"])), now_iso),
            )
        # Children first: blocklist_hits has a real FOREIGN KEY to listings and
        # foreign_keys=ON, so the parent delete would fail while they exist.
        counts["blocklist_hits"] = conn.execute(
            f"DELETE FROM blocklist_hits WHERE listing_id IN ({expired_listings_sql})",
            expired_params,
        ).rowcount
        counts["forwardings"] = conn.execute(
            f"""
            DELETE FROM forwardings
            WHERE status != 'forwarding'
              AND (created_at < ? OR listing_id IN ({expired_listings_sql}))
            """,
            (cutoff, *expired_params),
        ).rowcount
        counts["audit_log"] = conn.execute(
            f"""
            DELETE FROM audit_log
            WHERE created_at < ? OR listing_id IN ({expired_listings_sql})
            """,
            (cutoff, *expired_params),
        ).rowcount
        counts["listings"] = conn.execute(
            f"DELETE FROM listings WHERE id IN ({expired_listings_sql})",
            expired_params,
        ).rowcount
        counts["skips"] = conn.execute(
            "DELETE FROM skips WHERE timestamp < ?", (cutoff,)
        ).rowcount
        counts["ai_cache"] = conn.execute(
            "DELETE FROM ai_cache WHERE created_at < ?", (cutoff,)
        ).rowcount
    return counts

