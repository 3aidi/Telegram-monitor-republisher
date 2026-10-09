"""Destination channels and forwardings repository."""

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
    to_peer_reference,
)

def list_destination_items(active_only: bool = True, db_path: Optional[str] = None) -> List[models.DestinationItem]:
    """Fetch destinations as strongly-typed DestinationItem dataclasses."""
    rows = list_destinations(active_only=active_only, db_path=db_path)
    return [models.DestinationItem.from_row(r) for r in rows]

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
               OR error LIKE '%permission to access%'
               OR error LIKE '%ChatWriteForbidden%'
               OR error LIKE '%ChannelPrivate%'
               OR error LIKE '%throttled%'
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

# DEST-HEALTH: how many recent attempts define a destination's health. Small
# enough that a group banned yesterday is flagged quickly, large enough that one
# bad post does not condemn a healthy destination.
DESTINATION_HEALTH_WINDOW = 50

def get_destination_health(
    window: int = DESTINATION_HEALTH_WINDOW, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Per-destination forward success/failure health over the recent window.

    Counts only the most recent ``window`` attempts per destination, so a
    destination that recovered is reported healthy again. Includes destinations
    that have never been forwarded to (all counters zero) so the caller can
    show every configured destination.

    Only CONCLUDED attempts are scored. ``pending``/``forwarding`` rows are
    unresolved work, not failures: a destination throttled by Telegram still has
    50 pending rows, and counting those as failures reported healthy groups as
    banned. They are surfaced separately as ``deferred``/``is_throttled``.

    ``consecutive_failures`` counts back from the newest attempt while rows are
    still failing, which is what separates "one flaky post" from "permanently
    banned". A destination with successes in the window but recent consecutive
    failures is flapping, not dead.
    """
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM destinations ORDER BY id ASC"
        ).fetchall()
        destinations = [dict(r) for r in rows]

        for dest in destinations:
            attempts = conn.execute(
                """
                SELECT status, error, updated_at
                FROM forwardings
                WHERE destination_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (dest["id"], window),
            ).fetchall()

            successes = sum(1 for a in attempts if a["status"] == "forwarded")
            failures = sum(1 for a in attempts if a["status"] == "failed")
            deferred = sum(
                1 for a in attempts if a["status"] in ("pending", "forwarding")
            )
            total = successes + failures

            consecutive = 0
            for a in attempts:  # newest first
                if a["status"] == "forwarded":
                    break
                if a["status"] == "failed":
                    consecutive += 1
                # Unresolved rows are neither a success nor a failure, so they
                # neither extend nor break a run of real failures.

            last_success = next(
                (a["updated_at"] for a in attempts if a["status"] == "forwarded"),
                None,
            )
            last_error = next(
                (a["error"] for a in attempts if a["status"] == "failed" and a["error"]),
                None,
            )
            newest = attempts[0] if attempts else None
            is_throttled = bool(
                newest
                and newest["status"] == "pending"
                and str(newest["error"] or "").startswith("throttled")
            )

            dest["attempts"] = total
            dest["successes"] = successes
            dest["failures"] = failures
            dest["deferred"] = deferred
            dest["fail_pct"] = round(100.0 * failures / total, 1) if total else 0.0
            dest["consecutive_failures"] = consecutive
            dest["last_success_at"] = last_success
            dest["last_error"] = last_error
            dest["is_throttled"] = is_throttled

            # A destination is only "dead" once it has a real sample and has
            # stopped succeeding entirely. One failure is never enough.
            dest["is_dead"] = total >= 5 and successes == 0
            dest["is_flapping"] = total >= 5 and successes > 0 and consecutive >= 3
        return destinations

def get_destination_by_id(
    destination_id: int, db_path: Optional[str] = None, with_health: bool = False
) -> Optional[Dict[str, Any]]:
    """Return one destination row by its primary key.

    ``with_health`` merges in the same delivery-health fields as
    get_destination_health(), for admin screens that render a single row.
    """
    if not with_health:
        with db_session(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM destinations WHERE id = ?", (destination_id,)
            ).fetchone()
            return dict(row) if row else None
    for row in get_destination_health(db_path=db_path):
        if row["id"] == destination_id:
            return row
    return None

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

def mark_forwarded(
    forwarding_id: int,
    destination_message_id: Any = None,
    db_path: Optional[str] = None,
) -> None:
    """Record a successfully forwarded message, storing destination_message_id if provided."""
    dest_msg_id = None
    real_db_path = db_path
    if isinstance(destination_message_id, int):
        dest_msg_id = destination_message_id
    elif isinstance(destination_message_id, str) and real_db_path is None:
        real_db_path = destination_message_id

    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(real_db_path) as conn:
        if dest_msg_id is not None:
            conn.execute(
                """
                UPDATE forwardings
                SET status = 'forwarded', error = NULL, retry_at = NULL,
                    destination_message_id = ?, forwarded_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(dest_msg_id), now_iso, now_iso, forwarding_id),
            )
        else:
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

def defer_forwarding(
    forwarding_id: int,
    error: str,
    delay_seconds: int,
    db_path: Optional[str] = None,
) -> None:
    """Push a row's next attempt out WITHOUT consuming its retry budget.

    DEST-2: a FloodWait is the account being throttled, not the destination
    being broken. Counting it against FORWARD_MAX_RETRIES meant a burst of
    throttling burned all 8 attempts and marked a perfectly healthy
    destination dead. This defers the row instead, so the same budget is still
    available if the destination is genuinely broken later.
    """
    now = datetime.now(timezone.utc)
    retry_at = (now + timedelta(seconds=max(1, delay_seconds))).isoformat()
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE forwardings
            SET status = 'pending', error = ?, retry_count = retry_count,
                retry_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (error[:500], retry_at, now.isoformat(), forwarding_id),
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

def get_forwardings_for_listing(
    listing_id: int, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Return all forwardings (and destination chat_id) for a listing."""
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT f.*, d.chat_id AS destination_chat_id, d.title AS destination_title
            FROM forwardings f
            JOIN destinations d ON d.id = f.destination_id
            WHERE f.listing_id = ?
            ORDER BY f.id ASC
            """,
            (listing_id,),
        ).fetchall()
        return [dict(row) for row in rows]

def cancel_pending_forwardings_for_listing(
    listing_id: int, db_path: Optional[str] = None
) -> int:
    """Cancel any pending forwardings when a listing is marked as sold."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE forwardings
            SET status = 'cancelled', updated_at = ?
            WHERE listing_id = ? AND status = 'pending'
            """,
            (now_iso, listing_id),
        )
        return cur.rowcount

