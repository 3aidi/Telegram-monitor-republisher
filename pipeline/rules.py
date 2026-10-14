import os
import re
from typing import Any, List, Optional, Tuple, Union

import filters
import parser

# Regex for seller phrasing in body/content
_SELLER_PHRASE_RE = re.compile(
    r"\b(?:wts|selling|for\s+sale|stock\s+available|available\s+stock|in\s+stock|ready\s+stock|"
    r"have\s+stock|dm\s+to\s+buy|supplier|vendor|my\s+channel|our\s+channel)\b",
    re.IGNORECASE,
)

# Regex for explicit buy intent
_EXPLICIT_BUY_RE = re.compile(
    r"\b(?:wtb|want\s+to\s+buy|w\.t\.b|buying|need|looking\s+for|in\s+need\s+of|who\s+sells?)\b",
    re.IGNORECASE,
)

# Regex for sell-phrase match in raw text or body for fallback check
_SELL_PHRASE_RE = re.compile(
    r"\b(?:wts|selling|for\s+sale|stock|available|w\.t\.s|seller|supplier)\b",
    re.IGNORECASE,
)


def check_buyer_framing(body_lines_or_text: Union[str, List[str], Tuple[str, ...]]) -> bool:
    """True if text/lines respect buyer-only framing (no WTS/Selling/For Sale/Stock phrasing).

    Called before publishing from BOTH the AI path and fallback path.
    """
    if isinstance(body_lines_or_text, (list, tuple)):
        text = " \n ".join(body_lines_or_text)
    else:
        text = str(body_lines_or_text or "")
    if not text.strip():
        return False
    return not bool(_SELLER_PHRASE_RE.search(text))


class ListingPipeline:
    """Evaluates inbound listing candidates against safety, intent, and quality gates."""

    @staticmethod
    def inspect_safety(raw_text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Run pre-AI safety checks on raw supplier text.

        Returns:
            Tuple of (is_safe, risky_keyword, payment_proof)
        """
        risky_keyword = filters.contains_blocked_keyword(raw_text)
        payment_proof = filters.detect_payment_proof(raw_text)
        is_safe = (not risky_keyword) and (not payment_proof)
        return is_safe, risky_keyword, payment_proof

    @staticmethod
    def can_auto_publish(
        intent: str,
        paused: bool,
        body_ok: bool,
        has_payment_proof: Optional[str] = None,
        rendered_body: Optional[Any] = None,
    ) -> bool:
        """Evaluate if an analyzed listing qualifies for direct auto-publishing."""
        if rendered_body is not None and not check_buyer_framing(rendered_body):
            return False
        return (
            (intent or "").lower() == "buy"
            and not paused
            and body_ok
            and not bool(has_payment_proof)
        )

    @staticmethod
    def can_fallback_publish(
        fallback_autopublish: bool,
        paused: bool,
        risky_keyword: Optional[str],
        has_payment_proof: Optional[str],
        raw_text: str,
        body_ok: bool,
        rendered_body: Optional[Any] = None,
        platform: Optional[str] = None,
    ) -> bool:
        """Evaluate if an unanalyzed listing qualifies for deterministic fallback publication.

        Default is 0 (never auto-publish). When FALLBACK_AUTOPUBLISH=1, requires ALL:
        explicit buy intent, no sell phrases, clean keyword screen, no payment proof,
        substantive body, known platform, and buyer framing check.
        """
        if not fallback_autopublish:
            return False
        if paused or risky_keyword or has_payment_proof or not body_ok:
            return False
        # Explicit buy-intent keywords matched deterministically
        if not bool(_EXPLICIT_BUY_RE.search(raw_text)):
            return False
        # No sell-phrase match (WTS, selling, for sale, stock, available, supplier handle)
        if bool(_SELL_PHRASE_RE.search(raw_text)):
            return False
        # Platform matched from a known allowlist
        p = (platform or "").lower() or filters.detect_platform_from_text(raw_text)
        if not p or p not in filters.KNOWN_PLATFORMS:
            return False
        # Buyer framing check
        if rendered_body is not None and not check_buyer_framing(rendered_body):
            return False
        return True

    @staticmethod
    def prepare_listing_body(raw_text: str, fallback_clean_text: Optional[str] = None) -> Tuple[list[str], bool]:
        """Strip emojis and format body lines for fallback rendering."""
        clean = fallback_clean_text or parser.strip_all_emoji(raw_text)
        lines = [ln.strip() for ln in clean.split("\n") if ln.strip()] or ["Available"]
        return parser.prepare_body(lines, raw_text)
