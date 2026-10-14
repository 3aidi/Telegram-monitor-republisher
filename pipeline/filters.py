"""Duplicate listing detection for the republisher.

Stolen/blocked-content filtering is performed by the AI analysis
(ai_rephraser.analyze_message → blocked flag); this module only handles
fingerprint-based duplicate detection. Every check returns a named reason
string so the caller can log the exact rule that skipped a message.
"""

import logging
import os
import re
from typing import Optional, Set
import emoji
import db

logger = logging.getLogger("filters")

# Named skip reasons produced by this module's filters.
REASON_DUPLICATE = "duplicate"
REASON_NO_CONTENT = "no_content"
REASON_CHATTER = "chatter"

# ---------------------------------------------------------------------------
# Sanitizer patterns (Task 2): strip URLs, handles, emails, phones, crypto
# addresses and long hex/base58 strings.
# ---------------------------------------------------------------------------
URL_PATTERN = re.compile(
    r"\b(?:https?://|ftp://|www\.)\S+|(?:^|[\s/])(?:t|telegram)\.me/[a-zA-Z0-9_+/-]+",
    re.IGNORECASE,
)
HANDLE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])@[A-Za-z0-9_]{3,}\b"
)
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
PHONE_PATTERN = re.compile(
    r"(?:\+[0-9]{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b"
    r"|\+(?:[0-9][ -]?){7,14}[0-9]"
    r"|\b0\d{2,4}[-.\s]?\d{5,8}\b"
)
CRYPTO_ADDRESS_PATTERN = re.compile(
    r"\b0x[a-fA-F0-9]{40}\b"  # ETH / EVM
    r"|\b(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{38,59})\b"  # BTC
    r"|\bT[A-Za-z1-9]{33}\b"  # TRX / TRC20
    r"|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"  # SOL / Base58 (min 32 chars)
    r"|\b[0-9a-fA-F]{32,64}\b"  # Long hex
)

# ---------------------------------------------------------------------------
# Known Platforms Allowlist & Category Mapping (Task 3 & Task 11)
# ---------------------------------------------------------------------------
KNOWN_PLATFORMS: Set[str] = {
    # Streaming
    "netflix", "spotify", "youtube", "disney", "disney+", "hulu", "prime", "prime video",
    "apple tv", "iptv", "deezer", "tidal", "crunchyroll", "paramount+",
    # Crypto / Exchange
    "bybit", "binance", "kucoin", "okx", "crypto.com", "coinbase", "kraken", "bitget",
    "mexc", "gate.io", "huobi", "htx",
    # Neobanks & Payment
    "revolut", "wise", "buddybank", "buddypay", "indexo", "paypal", "stripe", "payoneer",
    "skrill", "neteller", "n26", "chime", "monzo", "cashapp", "venmo", "curve",
    "norisbank", "postbank", "sparkasse", "ing",
    # Cloud & VPS
    "hetzner", "aws", "digitalocean", "ovh", "chatgpt", "openai", "vultr", "linode",
}

PLATFORM_CATEGORIES = {
    "kyc": {"sumsub", "veriff", "jumio", "onfido", "shufti", "kyc"},
    "exchange": {"bybit", "binance", "kucoin", "okx", "crypto.com", "coinbase", "kraken", "bitget", "mexc", "gate.io", "huobi", "htx"},
    "crypto": {"bybit", "binance", "kucoin", "okx", "crypto.com", "coinbase", "kraken", "bitget", "mexc", "gate.io", "huobi", "htx"},
    "neobank": {"revolut", "wise", "buddybank", "buddypay", "indexo", "paypal", "stripe", "payoneer", "skrill", "neteller", "n26", "chime", "monzo", "cashapp", "venmo", "curve", "norisbank", "postbank", "sparkasse", "ing"},
    "bank": {"revolut", "wise", "buddybank", "buddypay", "indexo", "paypal", "stripe", "payoneer", "skrill", "neteller", "n26", "chime", "monzo", "cashapp", "venmo", "curve", "norisbank", "postbank", "sparkasse", "ing"},
    "streaming": {"netflix", "spotify", "youtube", "disney", "disney+", "hulu", "prime", "prime video", "apple tv", "iptv", "deezer", "tidal", "crunchyroll", "paramount+"},
    "vps": {"hetzner", "aws", "digitalocean", "ovh", "vultr", "linode"},
    "cloud": {"hetzner", "aws", "digitalocean", "ovh", "chatgpt", "openai", "vultr", "linode"},
}

def detect_platform_from_text(text: str) -> Optional[str]:
    """Extract platform name from text if it matches known platforms."""
    t = (text or "").lower()
    for p in sorted(KNOWN_PLATFORMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(p)}\b", t):
            return p
    return None

def check_category_blocked(text: str, platform: Optional[str] = None, blocked_categories: Optional[str] = None) -> Optional[str]:
    """Check if platform or text belongs to any blocked category in BLOCKED_PLATFORM_CATEGORIES."""
    raw_cats = blocked_categories if blocked_categories is not None else os.environ.get("BLOCKED_PLATFORM_CATEGORIES", "")
    if isinstance(raw_cats, (list, set, tuple)):
        cats = [str(c).strip().lower() for c in raw_cats if str(c).strip()]
    elif isinstance(raw_cats, str):
        cats = [c.strip().lower() for c in raw_cats.split(",") if c.strip()]
    else:
        cats = []
    if not cats:
        return None

    t = (text or "").lower()
    p = (platform or "").lower() or detect_platform_from_text(t) or ""

    for cat in cats:
        if cat == "kyc":
            if re.search(r"\b(?:kyc|verified|verification|verify|identity|document|passport|id card|sumsub|veriff)\b", t):
                return "kyc"
        known_in_cat = PLATFORM_CATEGORIES.get(cat, set())
        if p in known_in_cat:
            return cat
        if known_in_cat and any(re.search(rf"\b{re.escape(item)}\b", t) for item in known_in_cat):
            return cat
    return None


def is_duplicate_listing(
    clean_text: str,
    hours: int = 8,
    price: Optional[float] = None,
    exclude_listing_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> bool:
    """
    Check if a similar listing (normalized text + price fingerprint) was
    processed in the last N hours. This is content-based — a re-post with a
    new Telegram message id is still caught, while the same product at a
    different price is NOT.

    ``exclude_listing_id`` skips the caller's own row: the current listing
    carries the same fingerprint from insert time, and must never match itself.
    """
    if not (clean_text or "").strip():
        return False

    existing = db.find_recent_similar_listing(
        clean_text=clean_text,
        hours=hours,
        price=price,
        exclude_listing_id=exclude_listing_id,
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
    hours: int = 8,
    price: Optional[float] = None,
    exclude_listing_id: Optional[int] = None,
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
        clean_text,
        hours=hours,
        price=price,
        exclude_listing_id=exclude_listing_id,
        db_path=db_path,
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


def detect_buy_intent(text: str) -> bool:
    """True if text has explicit buy-intent keywords (WTB, want to buy, need, etc.)."""
    from pipeline.rules import _EXPLICIT_BUY_RE
    return bool(_EXPLICIT_BUY_RE.search(text or ""))


def detect_sell_intent(text: str) -> bool:
    """True if text has sell-phrase match (WTS, selling, for sale, stock, available, etc.)."""
    from pipeline.rules import _SELL_PHRASE_RE
    return bool(_SELL_PHRASE_RE.search(text or ""))