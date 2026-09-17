"""Message formatting and price helpers for the republisher.

The AI (ai_rephraser.analyze_message) handles platform/price/intent/content
extraction and rewriting. This module only formats the final post shell and
provides a small price parser used by the admin edit/preview flow.
"""

import os
import re
import unicodedata
from typing import List, Optional, Tuple
import emoji

import countries

# ---------------------------------------------------------------------------
# Fixed custom-emoji document IDs (hardcoded, never fetched at runtime).
# Countries use the centralized mapping in countries.COUNTRY_EMOJI.
# ---------------------------------------------------------------------------
# Used as header decoration for the header line (buyer-framed, e.g. WTB)
CE_FIRE       = 5375452661036358740   # 🔥 header
# Used as header decoration for the header line
CE_LIGHTNING  = 5404652296845936873   # ⚡ header
# Used on price line
CE_MONEYBAG   = 5384105916331202592   # 🤑 price
# Used on order/contact line
CE_PHONE      = 5231197925178089666   # 📞 contact


# Placeholder characters that get visually replaced by the entities above
PH_FIRE   = "🔥"
PH_LIGHT  = "⚡"
PH_PRICE  = "🤑"
PH_PHONE  = "📞"


def _make_custom_emoji_entity(offset: int, document_id: int, placeholder: str):
    """Create a MessageEntityCustomEmoji with the SAME offset/length semantics
    Telegram requires: both are measured in UTF-16 code units, and the entity
    must wrap EXACTLY ONE regular emoji equal to the custom document's
    ``documentAttributeCustomEmoji.alt``.

    ``offset`` must already be an absolute UTF-16 code-unit position (compute it
    with ``_utf16_len(text_before)``, never Python's ``len``). ``length`` is
    derived here from the placeholder's own UTF-16 size, so multi-code-unit
    anchors (🔥=2, 🇵🇱=4, 🏴󠁧󠁢󠁳󠁣󠁴󠁿=14) are always right. This ONE helper is the
    only place entities are built, so the two systems (header/price/contact
    role-emoji and country flags) can never drift out of sync.
    """
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


# ---------------------------------------------------------------------------
# Body sanitizer: strip leaked source prices, supplier handles and pure DM
# lines out of the AI-written body. The destination channel is the BUYER and
# always carries its OWN price footer, so any price or @contact left in the
# body is a leak of the source's economics/handles (LEAK-1). This runs on the
# AI output as a hard backstop on top of the prompt hardening in ai_rephraser.
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_HANDLE_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(@[A-Za-z0-9_]{3,})|t\.me/[A-Za-z0-9_]{3,}|https?://t\.me/",
    re.IGNORECASE,
)
# Lines that are ONLY "dm me / pm / inbox", optionally wrapped in phrasing.
_DM_ONLY_RE = re.compile(
    r"^\s*(?:for|to|just|or|and)?\s*(?:dm|pm|inbox|message|msg|text|telegram|contact)"
    r"\s*(?:me|us|now|fast|quick|direct)?\s*[.!:\-]*\s*$",
    re.IGNORECASE,
)
# Price-led lines: "PRICE 25$", "GOOD PRICE 200", "PRICE: $30", "RATE 40".
_PRICE_KEYWORD_RE = re.compile(
    r"^\s*(?:price|prix|preis|budget|cost|rate|good price|best price|our price|"
    r"top price|total|amount)\b.*\d",
    re.IGNORECASE,
)
# Whole line is effectively only money: "$25", "25$", "25 USD", "EUR 40".
_MONEY_ONLY_RE = re.compile(
    r"^(?:[$€£]\s*\d[\d.,]*|\d[\d.,]*\s*[$€£€£]"
    r"|\d[\d.,]*\s*(?:usd|usdt|eur|euros?|dollars?|bucks?|gbp|pounds?))$",
    re.IGNORECASE,
)
# Bare short number standing alone ("50" from math-bold "𝟱𝟬" after NFKC).
_BARE_SHORT_NUMBER_RE = re.compile(r"^\d{1,4}(?:[.,]\d+)?$")
# AI-rewritten budget sentences that embed the source's number ("Budget is set
# at 60 dollars for this specific requirement.").
_BUDGET_SENTENCE_RE = re.compile(
    r"\b(?:budget|spend|spending|willing to pay|paying)\b.*?"
    r"\b(?:dollars?|usd|usdt|eur|euros?|[$€£]\s*\d|\d\s*[$€£])\b",
    re.IGNORECASE,
)
# Short lines carrying a compact currency amount ("USA   50$").
_SHORT_CURRENCY_LINE_RE = re.compile(
    r"\d\s*[$€£]|\d[\d.,]*\s*(?:usd|usdt|eur|dollars?|euros?)\b", re.IGNORECASE
)
# Mid-line money blobs like "35$_USDT" / "50$" that follow platform words.
_PRICE_BLOB_RE = re.compile(r"\b\d[\d.,]*\s*[$€£][A-Za-z_]*\b")

# Fullwidth (０-９) and mathematical (𝟬-𝟵) digits: styled-digit pricing like
# "𝟱𝟬" / "𝟭𝟮 US" is a strong source-price signal in these channels.
_STYLED_DIGIT_RE = re.compile(r"[\uFF10-\uFF19\U0001D7CE-\U0001D7FF]")


def _nfkc(text: str) -> str:
    """Normalize fullwidth / math-bold digits to ASCII for reliable matching."""
    return unicodedata.normalize("NFKC", text or "")


def _is_styled_digit_price(raw: str, probe: str) -> bool:
    """True when a line is essentially styled-digits-only (\"𝟱𝟬\", \"𝟭𝟮 US\")."""
    if not _STYLED_DIGIT_RE.search(raw or ""):
        return False
    remainder = re.sub(r"\s+", " ", re.sub(r"[0-9]", "", probe)).strip(" \t.!@:;,.-")
    return len(remainder) <= 4


def _is_leaky_line(raw: str, probe: str, low: str) -> bool:
    """True when a body line must not ship (leaked price, @handle, pure DM).

    ``probe`` is the NFKC-folded, emoji-stripped, single-spaced line; ``raw``
    is the original. Real email addresses are treated as content so an account
    id like ``user@example.com`` is never dropped as a handle.
    """
    if _EMAIL_RE.search(low):
        return False
    if _is_styled_digit_price(raw, probe):
        return True
    if _HANDLE_RE.search(probe):
        return True
    if _DM_ONLY_RE.match(low):
        return True
    if _PRICE_KEYWORD_RE.match(low):
        return True
    if _MONEY_ONLY_RE.match(low):
        return True
    if _BARE_SHORT_NUMBER_RE.match(low):
        return True
    if _BUDGET_SENTENCE_RE.search(low):
        return True
    if _SHORT_CURRENCY_LINE_RE.search(low) and len(low) <= 24:
        return True
    return False


def sanitize_body_lines(content_lines, max_lines: int = 40) -> List[str]:
    """Drop leaked source prices, supplier @handles and pure DM lines from the
    body, and erase mid-line money blobs. Keeps the original text of every
    line that survives so the AI's wording stays intact."""
    kept: List[str] = []
    for raw_ln in content_lines or []:
        ln = (raw_ln or "").strip()
        if not ln:
            continue
        probe = re.sub(
            r"\s+", " ", emoji.replace_emoji(_nfkc(ln), replace=" ")
        ).strip()
        if not probe:
            continue
        if _is_leaky_line(ln, probe, probe.lower()):
            continue
        stripped = _PRICE_BLOB_RE.sub("", ln)
        # Strip literal markdown-bold syntax ("**ESTY KYC**") and lone single
        # asterisks used the same way. THE published-post send passes
        # formatting_entities WITHOUT parse_mode, so Telethon never interprets
        # "**" as bold — without this, asterisks would leak into the post as
        # literal characters. This runs in the ONE shared sanitizer each render
        # path goes through (auto-publish, approve, preview, /repair).
        stripped = stripped.replace("**", "").replace("*", "")
        stripped = re.sub(r"\s{2,}", " ", stripped).strip()
        if stripped:
            kept.append(stripped)
        if len(kept) >= max_lines:
            break
    return kept


def prepare_body(
    content_lines: List[str], raw_text: str = ""
) -> Tuple[List[str], bool]:
    """Sanitize the body and decide whether it is safe to AUTO-publish.

    Returns (lines, ok). ``ok`` is False when the cleaned body is too thin to
    represent a real listing (fewer than two lines, or nothing substantive) —
    such messages must go to admin approval instead of the auto-publish path.
    """
    lines = sanitize_body_lines(content_lines)
    ok = len(lines) >= 2 and any(len(ln.strip()) >= 8 for ln in lines)
    return lines, ok


def _asleep_footer_line() -> Optional[str]:
    """Return the buyer-asleep footer line, or None when the toggle is OFF.

    ``build_ai_message`` is the ONE place every published post is rendered
    (auto worker, admin approve, preview, /repair), so this single hook keeps
    every path consistent. ``db`` is imported lazily and every failure degrades
    to no footer, keeping parser a leaf module that runs standalone (unit tests,
    tooling). A missing default DB is never created here either — the line is
    skipped instead of side-effecting a new ``monitor.db`` file.
    """
    try:
        import db
    except Exception:
        return None
    if not os.path.exists(db.DEFAULT_DB_PATH):
        return None
    try:
        if not db.is_buyer_asleep():
            return None
        footer = db.get_buyer_asleep_footer() or "Buyer away, back shortly"
        return footer.strip()
    except Exception:
        return None


def build_ai_message(
    content_lines: List[str],
    platform: Optional[str] = None,
    contact_username: Optional[str] = None,
    intent: str = "sell",
    header_word: Optional[str] = None,
    listing_seed: int = 0,
    post_number: Optional[int] = None,
    sanitize_body: bool = True,
    source_text: Optional[str] = None,
) -> Tuple[str, list]:
    """
    Build the final formatted post from AI-provided clean content lines.

    The AI already produced clean, emoji-free body text; this function only
    wraps it in the fixed emoji shell (header / country lines / footer). Emoji
    are allowed ONLY in the header, the country lines, and the footer
    (price/contact lines) — the body is deliberately kept emoji-free. The
    header emoji rotates between the fire and lightning pools, seeded by
    listing_seed so the admin preview always matches the post that gets
    published. When ``post_number`` is given, a small "#N" banner is
    prepended so each post is individually referenceable.

    The channel always reads as the BUYER: the header label is the AI-suggested
    ``header_word`` when it is buyer-framed (see sanitize_buyer_header), else the
    buyer default "WTB ✦ DM FAST". The footer always carries the static
    "Price DM" line — prices are never computed, rendered, or used in
    publishing decisions here.

    Every country mentioned in the body is flagged on its OWN line where it
    appears: the country's real flag emoji (e.g. 🇵🇱 — the exact
    ``documentAttributeCustomEmoji.alt`` glyph) is appended after the country
    name on that same line and wrapped by a MessageEntityCustomEmoji entity.
    Telegram renders the custom emoji only over that exact glyph, so the anchor
    is never a generic placeholder. Countries in ``source_text`` that the body
    does not already mention get a generated country line below the body, so no
    country goes unflagged and none is ever duplicated. Callers without raw
    text can omit ``source_text``; source-derived extras are then skipped.
    All entity offsets/lengths are UTF-16 code units (see
    _make_custom_emoji_entity).

    Returns (text, entities) — pass both to Telethon send_message(). When
    ``sanitize_body`` is False the body is wrapped verbatim (used only to
    reconstruct what was actually published, for /repair diffing).
    """
    if sanitize_body:
        lines = sanitize_body_lines(content_lines)[:40]
    else:
        lines = [ln.strip() for ln in content_lines if ln and ln.strip()][:40]
    if not lines:
        lines = ["Available"]

    platform_display = platform.upper() if platform else "ACCOUNT"
    header_word = sanitize_buyer_header(header_word) or "WTB ✦ DM FAST"
    # Header emoji rotates between fire and lightning so posts don't all share
    # one fixed decoration. Seeded deterministically per listing id (preview == published).
    header_doc = CE_FIRE if listing_seed % 2 == 0 else CE_LIGHTNING

    parts: List[str] = []
    entities: list = []
    cursor = 0

    # ── Post number banner (lets subscribers + admin reference the exact post)
    if post_number is not None:
        post_num_line = f"#{post_number}\n"
        parts.append(post_num_line)
        cursor += _utf16_len(post_num_line)

    # ── Header line
    # _build_header returns offsets LOCAL to the header substring; rebase them
    # onto the absolute UTF-16 cursor so a preceding post-number banner (or any
    # future prefix) can never shift the flame entities out of place.
    header_text, header_entities = _build_header(
        platform_display, header_word, header_doc, header_doc
    )
    for header_entity in header_entities:
        header_entity.offset += cursor
    entities.extend(header_entities)
    parts.append(header_text)
    cursor += _utf16_len(header_text)

    # ── Empty line separating the header from the body
    parts.append("\n")
    cursor += 1

    # ── Content lines (emoji-free body: emoji allowed only in header/footer).
    # Country flags are attached HERE, after sanitization: a body line that
    # mentions a country gets that country's custom flag appended to the SAME
    # line. The anchor is the country's real alt emoji (e.g. 🇵🇱), which is
    # exactly what the custom document's alt contains — Telegram renders the
    # custom emoji only over that exact glyph. Because flags are appended to the
    # final text AFTER sanitize_body_lines ran, the body emoji-strip can never
    # touch them (see countries.flag_body_lines).
    flagged_lines, body_countries = countries.flag_body_lines(lines)
    for line_text, flags in flagged_lines:
        line_str = f"{line_text}\n"
        parts.append(line_str)
        pos = 0
        for alt, doc_id in flags:
            idx = line_text.find(alt, pos)
            if idx < 0:  # generated lines always carry their anchor; be safe
                continue
            entities.append(
                _make_custom_emoji_entity(
                    cursor + _utf16_len(line_text[:idx]), doc_id, alt
                )
            )
            pos = idx + len(alt)
        cursor += _utf16_len(line_str)

    # ── Extra country lines: countries from the RAW source message that the
    # body does NOT already mention. A country already represented in a body
    # line is skipped here — that single check is what stops the "NO POLAND /
    # POLAND 🇵🇱" duplicate from ever leaking into a published post.
    for name in countries.detect_countries(source_text or ""):
        if countries.canonical_of(name) in body_countries:
            continue
        flag = countries.flag_for(name)
        if flag is None:  # unmapped / unknown anchor — never guess a flag
            continue
        alt, doc_id = flag
        leader = f"{name}  "
        parts.append(leader + alt + "\n")
        entities.append(_make_custom_emoji_entity(cursor + _utf16_len(leader), doc_id, alt))
        cursor += _utf16_len(leader) + _utf16_len(alt) + 1

    # ── Divider
    divider = "──────────\n"
    parts.append(divider)
    cursor += _utf16_len(divider)

    # ── Price line (always the static "Price DM" footer — prices never render)
    price_str = f"{PH_PRICE} Price  DM\n"
    entities.append(_make_custom_emoji_entity(cursor, CE_MONEYBAG, PH_PRICE))
    parts.append(price_str)
    cursor += _utf16_len(price_str)

    # ── Contact line
    if contact_username:
        clean_contact = contact_username.strip().lstrip("@")
        if clean_contact:
            contact_placeholder = PH_PHONE
            contact_str = f"{contact_placeholder} Contact  : @{clean_contact}"
            entities.append(_make_custom_emoji_entity(cursor, CE_PHONE, contact_placeholder))
            parts.append(contact_str)
            cursor += _utf16_len(contact_str)

    # ── Buyer-asleep footer (only while the 'I'm Asleep' toggle is ON)
    asleep_footer = _asleep_footer_line()
    if asleep_footer:
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")
            cursor += _utf16_len("\n")
        asleep_str = f"{asleep_footer}\n"
        parts.append(asleep_str)
        cursor += _utf16_len(asleep_str)

    return "".join(parts), entities