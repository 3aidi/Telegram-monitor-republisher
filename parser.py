"""Message formatting and price helpers for the republisher.

The AI (ai_rephraser.analyze_message) handles platform/price/intent/content
extraction and rewriting. This module only formats the final post shell and
provides a small price parser used by the admin edit/preview flow.
"""

import os
import re
import time
from typing import Dict, List, Optional, Tuple
import emoji

import db

# ---------------------------------------------------------------------------
# Custom emoji document IDs from the user's premium packs.
# SkullEmoji pack  : https://t.me/addemoji/SkullEmoji
# SlimeFontEmoji   : https://t.me/addemoji/SlimeFontEmoji
# LedScreenEmoji   : https://t.me/addemoji/LedScreenEmoji
# ---------------------------------------------------------------------------
# Used as header decoration for the header line (buyer-framed, e.g. WTB)
CE_FIRE       = 5375452661036358740   # 🔥  SkullEmoji
# Used as header decoration for the header line
CE_LIGHTNING  = 5404652296845936873   # ⚡  LedScreenEmoji
# Used on price line
CE_MONEYBAG   = 5384105916331202592   # 🤑  SkullEmoji
# Used on order/contact line
CE_PHONE      = 5231197925178089666   # 📞  SkullEmoji


# Placeholder characters that get visually replaced by the entities above
PH_FIRE   = "🔥"
PH_LIGHT  = "⚡"
PH_PRICE  = "🤑"
PH_PHONE  = "📞"

# Message roles -> (default document_id, placeholder char, expected emoji).
# The admin bot can override any role by sending emoji-pack links
# (/addemoji); the overrides are stored in db.emoji_config and hot-picked up.
EMOJI_ROLES: Dict[str, Tuple[int, str, str]] = {
    "fire":      (CE_FIRE,      PH_FIRE,  "🔥"),
    "lightning": (CE_LIGHTNING, PH_LIGHT, "⚡"),
    "moneybag":  (CE_MONEYBAG,  PH_PRICE, "🤑"),
    "phone":     (CE_PHONE,     PH_PHONE, "📞"),
}

# Override cache: role -> document_id. Falls back to the hardcoded defaults.
_EMOJI_OVERRIDES: Optional[Dict[str, int]] = None
_EMOJI_CACHE_AT = 0.0
_EMOJI_TTL = 30.0
# Allow tests / alternate DBs to point the emoji lookups at a specific file.
EMOJI_DB_PATH: Optional[str] = None


def reload_emoji_config() -> None:
    """Drop the cached overrides so the next message build re-reads the DB."""
    global _EMOJI_OVERRIDES, _EMOJI_CACHE_AT
    _EMOJI_OVERRIDES = None
    _EMOJI_CACHE_AT = 0.0


def _ce(role: str) -> int:
    """Document id for a role, using the DB override if set, else the default."""
    global _EMOJI_OVERRIDES, _EMOJI_CACHE_AT
    now = time.time()
    if _EMOJI_OVERRIDES is None or now - _EMOJI_CACHE_AT > _EMOJI_TTL:
        try:
            path = EMOJI_DB_PATH or db.DEFAULT_DB_PATH
            cfg: Dict[str, dict] = {}
            if os.path.exists(path):
                cfg = db.get_emoji_configs(db_path=EMOJI_DB_PATH)
            _EMOJI_OVERRIDES = {r: cfg[r]["document_id"] for r in cfg}
        except Exception:
            _EMOJI_OVERRIDES = {}
        _EMOJI_CACHE_AT = now
    return _EMOJI_OVERRIDES.get(role, EMOJI_ROLES[role][0])


def _make_custom_emoji_entity(offset: int, document_id: int, placeholder: str):
    """Create a MessageEntityCustomEmoji at the given UTF-16 offset."""
    # Import here to avoid circular imports at module level
    from telethon.tl.types import MessageEntityCustomEmoji
    # Custom emoji length = length of the placeholder character in UTF-16 code units
    length = len(placeholder.encode("utf-16-le")) // 2
    return MessageEntityCustomEmoji(
        offset=offset,
        length=length,
        document_id=document_id,
    )


def _utf16_len(s: str) -> int:
    """Length of string in UTF-16 code units (what Telegram uses for entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def _build_header(platform_display: str, mode_word: str,
                  first_doc_id: int, second_doc_id: int) -> Tuple[str, list]:
    """
    '🔥 PLATFORM MODE 🔥\n' with both emoji replaced by custom emoji entities.

    Offsets are derived from the *actual* UTF-16 length of the literal text that
    precedes each emoji, so they can never drift.
    """
    body = f" {platform_display} {mode_word} "
    text = f"{PH_FIRE}{body}{PH_FIRE}\n"
    entities = [
        _make_custom_emoji_entity(0, first_doc_id, PH_FIRE),
        _make_custom_emoji_entity(_utf16_len(PH_FIRE) + _utf16_len(body), second_doc_id, PH_FIRE),
    ]
    return text, entities


# Regex to match prices in various international reseller formats:
# $40, 40$, 40 USD, USD 40, 40 USDT, USDT 40, €40, 40€, 40 EUR,
# Price: 40, Rate $40, 1,200 -> 1200, 12,50 -> 12.5, 1,200.50 -> 1200.5
_SIMPLE_NUMBER = r"(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?"
PRICE_PATTERN = re.compile(
    rf"(?:(?:\$|€|USD|USDT|EUR)\s*(?P<a1>{_SIMPLE_NUMBER}))"
    rf"|(?:(?P<a2>{_SIMPLE_NUMBER})\s*(?:\$|€|USD|USDT|EUR))"
    rf"|(?:(?:price|rate|instant price)\s*[:?]?\s*\$?\s*(?P<a3>{_SIMPLE_NUMBER})\s*(?:\$|€|USD|USDT|EUR)?)",
    re.IGNORECASE,
)


def strip_all_emoji(text: str) -> str:
    """Strip all standard emoji characters and trim whitespace."""
    if not text:
        return ""
    return emoji.replace_emoji(text, replace="").strip()


# Buyer-side header hint words (the channel is always the buyer).
_BUYER_HINTS = re.compile(
    r"\b(WTB|WANTED|WANT|BUY|BUYING|DM|FAST|PAY|PAYING|LOOKING|NEED|SEEKING|PURCHASE|PROCURE)\b",
    re.IGNORECASE,
)
# Seller-side words that must never appear in our header.
_SELLER_HINTS = re.compile(
    r"\b(FOR SALE|SELLING|WTS|OFFER|OFFERING|IN STOCK|AVAILABLE|STOCK)\b",
    re.IGNORECASE,
)


def sanitize_buyer_header(raw) -> Optional[str]:
    """Validate an AI-suggested header tagline so it never reads as a seller.

    The destination channel always acts as the BUYER, so any tagline implying
    an offer for sale (FOR SALE, SELLING, WTS, OFFERING, AVAILABLE, IN STOCK)
    is rejected and the caller falls back to the default buyer header. Returns
    None when the tagline is missing, unsafe, or not buyer-framed, else a
    cleaned emoji-free string.
    """
    if raw is None:
        return None
    text = re.sub(r"\s+", " ", (strip_all_emoji(str(raw)) or "").strip())
    if not text:
        return None
    text = text[:40].rstrip()
    if _SELLER_HINTS.search(text):
        return None
    if not _BUYER_HINTS.search(text):
        return None
    return text


def _parse_amount(raw: str) -> float:
    """
    Convert a matched numeric string to a float, handling thousands separators
    and European decimal-comma ('1,200' -> 1200.0, '12,50' -> 12.5).
    """
    s = raw.strip().replace(" ", "")
    if not s:
        return 0.0
    if re.match(r"^\d{1,3},\d{3}(?:\.\d+)?$", s):
        return float(s.replace(",", ""))
    if "," in s:
        s = s.replace(",", ".")
    return float(s)


def _extract_price_from_text(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract the first price amount and its matched text span."""
    if not text:
        return None, None
    match = PRICE_PATTERN.search(text)
    if not match:
        return None, None

    for group_name in ("a1", "a2", "a3"):
        val = match.group(group_name)
        if val is not None:
            return _parse_amount(val), match.group(0)

    return None, None


def extract_price(text: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Extract price amount and matched string span from text.
    Returns (price_float, matched_span_str) or (None, None).
    """
    return _extract_price_from_text(text)


def apply_pricing_rule(original_price: float, multiplier: float = 0.75) -> int:
    """
    Apply the multiplier (e.g. 0.75x) and round up to the nearest integer
    if the resulting price has a decimal part.

    Uses Decimal arithmetic so financial calculations never hit binary-float
    precision drift (e.g. 7 * 0.1 is not exactly 0.7 as a float) — FIN-1.
    Examples:
      - $7 * 0.75 = 5.25 -> 6
      - $130 * 0.75 = 97.5 -> 98
      - $50 * 0.75 = 37.5 -> 38
      - $100 * 0.75 = 75.0 -> 75
    """
    from decimal import Decimal, ROUND_CEILING

    amount = Decimal(str(original_price)) * Decimal(str(multiplier))
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def _format_price(price: float) -> str:
    """Render a price cleanly: '$38' not '$38.0'; keeps decimals like '$12.50'."""
    if price is None:
        return ""
    if float(price).is_integer():
        return f"{int(price)}"
    return f"{float(price):g}"


def build_ai_message(
    content_lines: List[str],
    our_price: Optional[float],
    platform: Optional[str] = None,
    contact_username: Optional[str] = None,
    intent: str = "sell",
    header_word: Optional[str] = None,
    has_price: Optional[bool] = None,
    listing_seed: int = 0,
    post_number: Optional[int] = None,
) -> Tuple[str, list]:
    """
    Build the final formatted post from AI-provided clean content lines.

    The AI already produced clean, emoji-free body text; this function only
    wraps it in the premium emoji shell (header / price / contact). Emoji are
    allowed ONLY in the header and the footer (price/contact lines) — the body
    is deliberately kept emoji-free. The header emoji rotates between the fire
    and lightning pools, seeded by listing_seed so the admin preview always
    matches the post that gets published. When ``post_number`` is given, a
    small "Post #N" banner is prepended so each post is individually referenceable.

    The channel always reads as the BUYER: the header label is the AI-suggested
    ``header_word`` when it is buyer-framed (see sanitize_buyer_header), else a
    buyer default — "WTB ✦ DM FAST" with a price, "WANTED" without.

    Returns (text, entities) — pass both to Telethon send_message().
    """
    lines = [ln.strip() for ln in content_lines if ln and ln.strip()][:40]
    if not lines:
        lines = ["Available"]

    has_price = (
        bool(our_price is not None and our_price > 0)
        if has_price is None
        else bool(has_price)
    )

    platform_display = platform.upper() if platform else "ACCOUNT"
    header_word = sanitize_buyer_header(header_word) or (
        "WTB ✦ DM FAST" if has_price else "WANTED"
    )
    # Header emoji rotates between fire and lightning so posts don't all share
    # one fixed decoration. Seeded deterministically per listing id (preview == published).
    header_pool = ("fire", "lightning")
    header_emoji = header_pool[listing_seed % len(header_pool)]

    parts: List[str] = []
    entities: list = []
    cursor = 0

    # ── Post number banner (lets subscribers + admin reference the exact post)
    if post_number is not None:
        post_num_line = f"🗂 Post  #{post_number}\n"
        parts.append(post_num_line)
        cursor += _utf16_len(post_num_line)

    # ── Header line
    header_text, header_entities = _build_header(
        platform_display, header_word, _ce(header_emoji), _ce(header_emoji)
    )
    parts.append(header_text)
    entities.extend(header_entities)
    cursor += _utf16_len(header_text)

    # ── Divider
    divider = "─────────────────────\n"
    parts.append(divider)
    cursor += _utf16_len(divider)

    # ── Content lines (emoji-free body: emoji allowed only in header/footer)
    for ln in lines:
        line_str = f"{ln}\n"
        parts.append(line_str)
        cursor += _utf16_len(line_str)

    # ── Divider
    parts.append(divider)
    cursor += _utf16_len(divider)

    # ── Price line
    if has_price:
        price_str = f"{PH_PRICE} Price  : ${_format_price(our_price)}\n"
        entities.append(_make_custom_emoji_entity(cursor, _ce("moneybag"), PH_PRICE))
        parts.append(price_str)
        cursor += _utf16_len(price_str)

    # ── Contact line
    if contact_username:
        clean_contact = contact_username.strip().lstrip("@")
        if clean_contact:
            contact_placeholder = PH_PHONE
            contact_str = f"{contact_placeholder} Order  : @{clean_contact}"
            entities.append(_make_custom_emoji_entity(cursor, _ce("phone"), contact_placeholder))
            parts.append(contact_str)
            cursor += _utf16_len(contact_str)

    return "".join(parts), entities