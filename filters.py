"""Duplicate listing detection for the republisher.

Stolen/blocked-content filtering is performed by the AI analysis
(ai_rephraser.analyze_message → blocked flag); this module only handles
fingerprint-based duplicate detection. Every check returns a named reason
string so the caller can log the exact rule that skipped a message.
"""

import logging
import re
from typing import Optional
import emoji
import db

logger = logging.getLogger("filters")

# Named skip reasons produced by this module's filters.
REASON_DUPLICATE = "duplicate"
REASON_NO_CONTENT = "no_content"
REASON_CHATTER = "chatter"


def is_duplicate_listing(
    clean_text: str,
    hours: int = 12,
    price: Optional[float] = None,
    db_path: Optional[str] = None,
) -> bool:
    """
    Check if a similar listing (normalized text + price fingerprint) was
    processed in the last N hours. This is content-based — a re-post with a
    new Telegram message id is still caught, while the same product at a
    different price is NOT.
    """
    if not (clean_text or "").strip():
        return False

    existing = db.find_recent_similar_listing(
        clean_text=clean_text,
        hours=hours,
        price=price,
        db_path=db_path,
    )

    if existing:
        logger.info(
            "Duplicate detected (matches listing #%s created at %s)",
            existing.get("id"),
            existing.get("created_at"),
        )
        return True

    return False


def check_filters(
    clean_text: str,
    hours: int = 12,
    price: Optional[float] = None,
    db_path: Optional[str] = None,
) -> Optional[str]:
    """Run the pre-AI filter chain and return the name of the first rule that
    skips the message, or None if every filter passes.

    Each check returns a specific reason string (never a bare counter) so the
    caller logs which rule fired (see db.log_skip).
    """
    if not (clean_text or "").strip():
        return REASON_NO_CONTENT

    if is_duplicate_listing(
        clean_text, hours=hours, price=price, db_path=db_path
    ):
        return REASON_DUPLICATE

    return None


# ---------------------------------------------------------------------------
# Chatter pre-filter (quota saver, NOT content safety).
# ---------------------------------------------------------------------------
# These fire only on obvious non-listings: rule posts, admin chatter, welcome
# pins, bot tests. Any real listing signal (price, platform name, @contact,
# WTS/WTB wording, URL) vetoes the decision so a real ad is never skipped.
_CHATTER_PATTERNS = [
    re.compile(r"\b(?:group|channel)?\s*(?:rule|rules)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:admin|admins?|moderator|mods?|owner)\b", re.IGNORECASE),
    re.compile(r"\b(?:sticky|pinned|announcement)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:welcome|hello|hi|hey)\b", re.IGNORECASE),
    re.compile(r"\b(?:no spam|don't (?:post|advertise|sell)|advertise here|fv )\b", re.IGNORECASE),
    re.compile(r"\b(?:bot test|test bot|testing)\b", re.IGNORECASE),
    re.compile(r"^\s*please\s*(?:read|check|see|follow)\b", re.IGNORECASE),
]

# Any one of these in the message vetoes the chatter decision.
_LISTING_SIGNALS = [
    re.compile(r"(?:\$|€|\b(?:USD|USDT|EUR)\b)\s?\d", re.IGNORECASE),
    re.compile(r"\b(?:price|rate|payment)\b", re.IGNORECASE),
    re.compile(r"@[\w_]{3,}"),
    re.compile(r"t\.me/|https?://", re.IGNORECASE),
    re.compile(
        r"\b(?:wts|wtb|for sale|selling|buying|available|kyc|verified|account|stock|offer)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:netflix|spotify|bybit|binance|kucoin|okx|crypto(?:\.com)?|revolut|wise|paypal|"
        r"n26|chime|monzo|buddypay|buddybank|indexo|hetzner|chatgpt|openai|aws)\b",
        re.IGNORECASE,
    ),
]

# Concrete platform/listing signals ONLY (no price patterns). Used by the
# deterministic fallback gate: a listing can auto-publish on its own merit
# (platform keyword, WTS/WTB wording, @contact, URL) — price never matters.
_PLATFORM_OR_LISTING_SIGNALS = _LISTING_SIGNALS[2:]


def obvious_non_listing(text: str, max_len: int = 300) -> Optional[str]:
    """Detect clearly non-listing chatter that would otherwise burn an AI call.

    Conservative by design: any listing signal (price, platform keyword,
    @username contact, WTS/WTB wording, URL) vetoes the decision so a real ad
    is never skipped. Returns REASON_CHATTER when the message looks like admin
    commentary/rule chatter, else None. Longer messages are always sent to AI.
    """
    t = (text or "").strip()
    if not t:
        return REASON_NO_CONTENT
    if len(t) > max_len:
        return None
    # Chatter markers are often decorated with emoji (e.g. "🔥 Welcome"); strip
    # them before matching so the decision is text-signal based only.
    t = emoji.replace_emoji(t, replace=" ").strip()
    for signal in _LISTING_SIGNALS:
        if signal.search(t):
            return None
    for pattern in _CHATTER_PATTERNS:
        if pattern.search(t):
            return REASON_CHATTER
    return None


# ---------------------------------------------------------------------------
# Blocked-content keyword screen (deterministic fallback safety valve).
# ---------------------------------------------------------------------------
# Used ONLY when the AI is unavailable (deterministic fallback path): listings
# containing these stolen/hacked/illicit signals still route to manual review
# instead of being auto-published without AI judgment.
_BLOCKED_KEYWORDS = [
    r"hacked",
    r"cracked",
    r"crack login",
    r"no email changed",
    r"no recovery",
    r"stolen",
    r"combolist",
    r"cc dump",
    r"carding",
    r"fullz",
    r"unauthorized access",
    r"push the av",
]
_BLOCKED_PATTERNS = [re.compile(kw, re.IGNORECASE) for kw in _BLOCKED_KEYWORDS]


def contains_blocked_keyword(text: str) -> Optional[str]:
    """Return the first blocked-content keyword matched in ``text``, or None.

    This mirrors the AI's ``blocked`` signal so the deterministic fallback
    (AI down) keeps risky content in the manual-review queue.
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    for pattern in _BLOCKED_PATTERNS:
        m = pattern.search(t)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Payment-proof detection — the single most dangerous miss-classification.
# ---------------------------------------------------------------------------
# A payment screenshot / "I paid $X, here is the proof" message is NOT a
# listing and must NEVER be auto-published (it exposes the buyer's payment
# details to the destination audience). These patterns are deliberately narrow:
# they need an explicit proof/receipt/confirmed vocabulary, which real listing
# bodies almost never contain.
_PAYMENT_PROOF_PATTERNS = [
    re.compile(r"\bproof\s+of\s+(?:payment|transfer|transaction|deposit)\b", re.IGNORECASE),
    re.compile(r"\b(?:payment|transfer|transaction|deposit|wire)\s+(?:proof|receipt)\b", re.IGNORECASE),
    re.compile(r"\bscreenshot\s+of\s+(?:the\s+)?(?:payment|bank|transaction|transfer)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:payment|transfer|wire|transaction)\s+(?:received|confirmed|completed|made|sent)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:paid|payed|sent|received|credited|transferred)\b.{0,40}"
        r"\b(?:proof|receipt|screenshot|confirmed|completed|success)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bamount\s+of\s+\$?\s?\d[\d.,]*\s+(?:paid|sent|received|credited)\b",
        re.IGNORECASE,
    ),
]


def detect_payment_proof(text: str) -> Optional[str]:
    """Return the matched payment-proof pattern, or None when it's a normal message."""
    t = (text or "").strip()
    if not t:
        return None
    for pattern in _PAYMENT_PROOF_PATTERNS:
        m = pattern.search(t)
        if m:
            return m.group(0)
    return None


def has_clear_listing_signal(text: str) -> bool:
    """Minimum bar the deterministic (AI-down) fallback demands before it is
    allowed to auto-publish: at least one concrete listing signal (platform
    keyword, WTS/WTB wording, @contact, URL). Prices are deliberately NOT
    signals — pricing never influences publishing decisions. Everything below
    the bar routes to manual review instead — no blind auto-publishes."""
    t = (text or "").strip()
    if not t:
        return False
    for signal in _PLATFORM_OR_LISTING_SIGNALS:
        if signal.search(t):
            return True
    return False