"""Backward-compatibility facade for domain.models.

Re-exports domain models so existing modules and tests importing `models`
continue to function seamlessly while code is organized into the `domain` package.
"""

from domain.models import (
    DestinationItem,
    ForwardItem,
    HeaderItem,
    ListingItem,
    SkipItem,
    SupplierItem,
)

__all__ = [
    "ListingItem",
    "SupplierItem",
    "DestinationItem",
    "HeaderItem",
    "ForwardItem",
    "SkipItem",
]
