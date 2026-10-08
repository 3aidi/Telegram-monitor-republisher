"""Strongly-typed domain models for Telegram Monitor & Republisher.

Provides dataclass representations for core domain entities (Listings, Suppliers,
Destinations, Headers, Forwardings, and Skips) with automatic serialization and
deserialization between database rows (dict / sqlite3.Row / tuple) and Python objects.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union


def _extract_val(row: Union[Mapping[str, Any], Sequence[Any], Any], key: str, index: Optional[int] = None) -> Any:
    """Helper to extract a field whether the row is a dict, sqlite3.Row, or tuple."""
    if isinstance(row, Mapping):
        return row.get(key)
    if hasattr(row, "keys") and callable(row.keys):
        try:
            return row[key]
        except (IndexError, KeyError):
            pass
    if hasattr(row, "__getitem__"):
        try:
            return row[key]
        except (TypeError, KeyError, IndexError):
            if index is not None and isinstance(index, int) and index < len(row):
                return row[index]
    return getattr(row, key, None)


@dataclass(slots=True)
class ListingItem:
    """Domain model for a product listing row."""

    id: int
    supplier_id: Optional[int]
    source_message_id: int
    status: str
    game_name: Optional[str] = None
    rank_tier: Optional[str] = None
    raw_text: Optional[str] = None
    clean_text: Optional[str] = None
    published_message_id: Optional[int] = None
    post_number: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    platform_name: Optional[str] = None
    retry_count: int = 0
    last_error: Optional[str] = None
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[str] = None
    published_at: Optional[str] = None
    fingerprint: Optional[str] = None
    intent: Optional[str] = None
    hold_until: Optional[str] = None
    duplicate_of: Optional[int] = None
    header_id: Optional[int] = None
    supplier_username: Optional[str] = None
    supplier_display_name: Optional[str] = None

    @classmethod
    def from_row(cls, row: Union[Mapping[str, Any], Any]) -> "ListingItem":
        if row is None:
            raise ValueError("Cannot create ListingItem from None")
        if isinstance(row, cls):
            return row

        return cls(
            id=int(_extract_val(row, "id", 0)),
            supplier_id=_extract_val(row, "supplier_id", 1),
            source_message_id=int(_extract_val(row, "source_message_id", 2) or 0),
            game_name=_extract_val(row, "game_name", 3),
            rank_tier=_extract_val(row, "rank_tier", 4),
            status=str(_extract_val(row, "status", 5) or "received"),
            raw_text=_extract_val(row, "raw_text", 6),
            clean_text=_extract_val(row, "clean_text", 7),
            published_message_id=_extract_val(row, "published_message_id", 8),
            post_number=_extract_val(row, "post_number", 9),
            created_at=_extract_val(row, "created_at", 10),
            updated_at=_extract_val(row, "updated_at", 11),
            platform_name=_extract_val(row, "platform_name", 12),
            retry_count=int(_extract_val(row, "retry_count", 13) or 0),
            last_error=_extract_val(row, "last_error", 14),
            reviewed_by=_extract_val(row, "reviewed_by", 15),
            reviewed_at=_extract_val(row, "reviewed_at", 16),
            published_at=_extract_val(row, "published_at", 17),
            fingerprint=_extract_val(row, "fingerprint", 18),
            intent=_extract_val(row, "intent", 19),
            hold_until=_extract_val(row, "hold_until"),
            duplicate_of=_extract_val(row, "duplicate_of"),
            header_id=_extract_val(row, "header_id"),
            supplier_username=_extract_val(row, "supplier_username"),
            supplier_display_name=_extract_val(row, "supplier_display_name"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_published(self) -> bool:
        return self.status == "published" and self.published_message_id is not None

    @property
    def is_held(self) -> bool:
        return self.status == "held"

    @property
    def is_buy_intent(self) -> bool:
        return (self.intent or "").lower() == "buy"


@dataclass(slots=True)
class SupplierItem:
    """Domain model for a supplier/source channel row."""

    id: int
    channel_username: Optional[str] = None
    channel_id: Optional[int] = None
    display_name: Optional[str] = None
    active: bool = True
    added_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Union[Mapping[str, Any], Any]) -> "SupplierItem":
        if row is None:
            raise ValueError("Cannot create SupplierItem from None")
        if isinstance(row, cls):
            return row

        active_raw = _extract_val(row, "active", 4)
        active_bool = bool(active_raw) if active_raw is not None else True

        return cls(
            id=int(_extract_val(row, "id", 0)),
            channel_username=_extract_val(row, "channel_username", 1),
            channel_id=_extract_val(row, "channel_id", 2),
            display_name=_extract_val(row, "display_name", 3),
            active=active_bool,
            added_at=_extract_val(row, "added_at", 5),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["active"] = 1 if self.active else 0
        return d

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        if self.channel_username:
            u = self.channel_username
            return u if u.startswith("@") else f"@{u}"
        if self.channel_id:
            return str(self.channel_id)
        return f"Supplier #{self.id}"


@dataclass(slots=True)
class DestinationItem:
    """Domain model for a destination channel or group."""

    id: int
    chat_id: str
    title: Optional[str] = None
    active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Union[Mapping[str, Any], Any]) -> "DestinationItem":
        if row is None:
            raise ValueError("Cannot create DestinationItem from None")
        if isinstance(row, cls):
            return row

        active_raw = _extract_val(row, "active", 3)
        active_bool = bool(active_raw) if active_raw is not None else True

        return cls(
            id=int(_extract_val(row, "id", 0)),
            chat_id=str(_extract_val(row, "chat_id", 1)),
            title=_extract_val(row, "title", 2),
            active=active_bool,
            created_at=_extract_val(row, "created_at", 4),
            updated_at=_extract_val(row, "updated_at", 5),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["active"] = 1 if self.active else 0
        return d


@dataclass(slots=True)
class HeaderItem:
    """Domain model for an admin-managed custom-emoji header."""

    id: int
    text: str
    emoji: list[tuple[str, int]] = field(default_factory=list)
    created_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Union[Mapping[str, Any], Any]) -> "HeaderItem":
        if row is None:
            raise ValueError("Cannot create HeaderItem from None")
        if isinstance(row, cls):
            return row

        raw_json = _extract_val(row, "emoji_json", 2)
        parsed_emoji: list[tuple[str, int]] = []
        if raw_json:
            try:
                data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            parsed_emoji.append((str(item[0]), int(item[1])))
                        elif isinstance(item, dict):
                            parsed_emoji.append((str(item.get("alt", "")), int(item.get("document_id", 0))))
            except Exception:
                pass

        return cls(
            id=int(_extract_val(row, "id", 0)),
            text=str(_extract_val(row, "text", 1) or ""),
            emoji=parsed_emoji,
            created_at=_extract_val(row, "created_at", 3),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "emoji": self.emoji,
            "emoji_json": json.dumps([{"alt": alt, "document_id": doc_id} for alt, doc_id in self.emoji]),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ForwardItem:
    """Domain model for a queued forwarding job."""

    id: int
    listing_id: Optional[int]
    published_chat_id: str
    published_message_id: int
    destination_id: int
    status: str = "pending"
    error: Optional[str] = None
    retry_count: int = 0
    retry_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    forwarded_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Union[Mapping[str, Any], Any]) -> "ForwardItem":
        if row is None:
            raise ValueError("Cannot create ForwardItem from None")
        if isinstance(row, cls):
            return row

        return cls(
            id=int(_extract_val(row, "id", 0)),
            listing_id=_extract_val(row, "listing_id", 1),
            published_chat_id=str(_extract_val(row, "published_chat_id", 2)),
            published_message_id=int(_extract_val(row, "published_message_id", 3)),
            destination_id=int(_extract_val(row, "destination_id", 4)),
            status=str(_extract_val(row, "status", 5) or "pending"),
            error=_extract_val(row, "error", 6),
            retry_count=int(_extract_val(row, "retry_count", 7) or 0),
            retry_at=_extract_val(row, "retry_at", 8),
            created_at=_extract_val(row, "created_at", 9),
            updated_at=_extract_val(row, "updated_at", 10),
            forwarded_at=_extract_val(row, "forwarded_at", 11),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SkipItem:
    """Domain model for a recorded skipped listing."""

    id: int
    supplier_id: Optional[int]
    message_id: Optional[int]
    reason: str
    raw_text: Optional[str] = None
    timestamp: Optional[str] = None

    @classmethod
    def from_row(cls, row: Union[Mapping[str, Any], Any]) -> "SkipItem":
        if row is None:
            raise ValueError("Cannot create SkipItem from None")
        if isinstance(row, cls):
            return row

        return cls(
            id=int(_extract_val(row, "id", 0)),
            supplier_id=_extract_val(row, "supplier_id", 1),
            message_id=_extract_val(row, "message_id", 2),
            reason=str(_extract_val(row, "reason", 3)),
            raw_text=_extract_val(row, "raw_text", 4),
            timestamp=_extract_val(row, "timestamp", 5),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
