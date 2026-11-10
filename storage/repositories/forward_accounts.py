"""Forwarder accounts pool repository."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import (
    db_session,
)


def add_forward_account(
    session_name: str,
    phone: Optional[str] = None,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    active: bool = True,
    db_path: Optional[str] = None,
) -> int:
    """Register or update a forwarder session account."""
    clean_session = (session_name or "").strip()
    if not clean_session:
        raise ValueError("Forwarder account requires a non-empty session name")
    
    clean_phone = (phone or "").strip() or None
    clean_username = (username or "").strip().lstrip("@") or None
    clean_display = (display_name or "").strip() or None
    active_int = 1 if active else 0
    now_iso = datetime.now(timezone.utc).isoformat()

    with db_session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forward_accounts (
                session_name, phone, username, display_name, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_name) DO UPDATE SET
                phone = COALESCE(excluded.phone, forward_accounts.phone),
                username = COALESCE(excluded.username, forward_accounts.username),
                display_name = COALESCE(excluded.display_name, forward_accounts.display_name),
                active = excluded.active,
                updated_at = excluded.updated_at
            """,
            (clean_session, clean_phone, clean_username, clean_display, active_int, now_iso, now_iso),
        )
        row = conn.execute(
            "SELECT id FROM forward_accounts WHERE session_name = ?", (clean_session,)
        ).fetchone()
        return row["id"] if row else None


def list_forward_accounts(
    active_only: bool = False, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """List all registered forwarder accounts ordered by id."""
    query = "SELECT * FROM forward_accounts"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id ASC"
    with db_session(db_path) as conn:
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]


def get_forward_account_by_session(
    session_name: str, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Look up a forwarder account by session name."""
    clean_session = (session_name or "").strip()
    if not clean_session:
        return None
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM forward_accounts WHERE session_name = ?", (clean_session,)
        ).fetchone()
        return dict(row) if row else None


def set_forward_account_active(
    account_id: int, active: bool, db_path: Optional[str] = None
) -> bool:
    """Enable or disable a forwarder account."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "UPDATE forward_accounts SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, now_iso, account_id),
        )
        return cursor.rowcount > 0


def delete_forward_account(
    account_id: int, db_path: Optional[str] = None
) -> bool:
    """Permanently remove a forwarder account record."""
    with db_session(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM forward_accounts WHERE id = ?", (account_id,)
        )
        return cursor.rowcount > 0
