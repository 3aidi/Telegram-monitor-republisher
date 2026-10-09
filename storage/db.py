"""Database layer for Telegram Monitor & Republisher using SQLite.

This module serves as the primary facade, re-exporting domain repositories:
- storage.repositories.base (Connection, sessions, and schema management)
- storage.repositories.suppliers (Suppliers and source channels)
- storage.repositories.listings (Listings lifecycle, quarantine, dedup, and publish claims)
- storage.repositories.destinations (Destinations and forwarding queue)
- storage.repositories.headers (Custom emoji headers pool)
- storage.repositories.audit (Audit logs, skips, settings, and cache)
"""

from storage.repositories.base import *
from storage.repositories.audit import *
from storage.repositories.suppliers import *
from storage.repositories.headers import *
from storage.repositories.destinations import *
from storage.repositories.listings import *

# Explicitly re-export module-private functions and constants
from storage.repositories.base import (
    _commit_with_retry,
    _migrate,
    _REQUIRED_LISTINGS_COLUMNS,
    _SOURCE_HWM_PREFIX,
)
from storage.repositories.listings import (
    _ACTIVE_DEDUP_STATUSES,
    _RECEIVED_DEDUP_MINUTES,
    _REJECT_MEMORY_MINUTES,
)
from storage.repositories.audit import (
    _RETENTION_INFLIGHT_LISTING_STATUSES,
)
from storage.repositories.destinations import (
    _destination_chat_ref,
    _forward_retry_at,
)
from storage.repositories.headers import (
    _row_to_header,
)
from storage.repositories.suppliers import (
    _move_listings_to_supplier,
)