"""Pipeline evaluation and gating rules for Telegram Monitor & Republisher.

Encapsulates decision logic for pre-filters, safety screens, AI analysis gating,
and publishing qualification.
"""

from typing import Optional, Tuple

import filters
import parser


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
    ) -> bool:
        """Evaluate if an analyzed listing qualifies for direct auto-publishing."""
        return (
            (intent or "").lower() == "buy"
            and not paused
            and body_ok
            and not bool(has_payment_proof)
        )

    @staticmethod
    def can_fallback_publish(
        fallback_enabled: bool,
        paused: bool,
        risky_keyword: Optional[str],
        has_payment_proof: Optional[str],
        has_clear_signal: bool,
        body_ok: bool,
    ) -> bool:
        """Evaluate if an unanalyzed listing qualifies for deterministic fallback publication."""
        return (
            fallback_enabled
            and not paused
            and not bool(risky_keyword)
            and not bool(has_payment_proof)
            and has_clear_signal
            and body_ok
        )

    @staticmethod
    def prepare_listing_body(raw_text: str, fallback_clean_text: Optional[str] = None) -> Tuple[list[str], bool]:
        """Strip emojis and format body lines for fallback rendering."""
        clean = fallback_clean_text or parser.strip_all_emoji(raw_text)
        lines = [ln.strip() for ln in clean.split("\n") if ln.strip()] or ["Available"]
        return parser.prepare_body(lines, raw_text)
