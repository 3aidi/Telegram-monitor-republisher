"""Message formatting and price helpers for the republisher.

The AI (ai_rephraser.analyze_message) handles platform/price/intent/content
extraction and rewriting. This module only formats the final post shell and
provides a small price parser used by the admin edit/preview flow.
"""

import re
import unicodedata
from typing import List, Optional, Tuple
import emoji

import countries

# ---------------------------------------------------------------------------
# Fixed custom-emoji document IDs (hardcoded, never fetched at runtime).
# Countries use the centralized mapping in countries.COUNTRY_EMOJI.
#
# The header line has no hardcoded emoji: it is drawn from the pool of
# admin-managed custom-emoji headers (see db.add_header / the /addheader
# command), so those document ids live in the database, not here.
# ---------------------------------------------------------------------------
# Used on price line
CE_MONEYBAG   = 5384105916331202592   # 🤑 price
# Used on order/contact line
CE_PHONE      = 5231197925178089666   # 📞 contact


# Placeholder characters that get visually replaced by the entities above
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


def _build_custom_emoji_header(
    emoji: List[Tuple[str, int]], cursor: int
) -> Tuple[str, list]:
    """Build the header line from admin-supplied custom emoji.

    ``emoji`` is a list of ``(alt, document_id)`` pairs in the order they should
    appear (one entry per custom emoji, ``alt`` being the glyph Telegram wrote
    into the message text for it). The rendered text is the alts concatenated
    with no separators, and each one is wrapped in a MessageEntityCustomEmoji so
    Telegram swaps in the real emoji.

    ``cursor`` is the absolute UTF-16 offset the header starts at, so entities
    come back already positioned for the whole post rather than local to this
    substring. Returns ("", []) for an empty pool, which renders no header line
    at all.
    """
    if not emoji:
        return "", []
    text_parts: List[str] = []
    entities: list = []
    offset = cursor
    for alt, document_id in emoji:
        entities.append(_make_custom_emoji_entity(offset, document_id, alt))
        text_parts.append(alt)
        # Advance by this glyph's OWN utf-16 width, so a multi-code-unit alt
        # (🔥=2, 🇵🇱=4) never desyncs the next entity from its text.
        offset += _utf16_len(alt)
    return "".join(text_parts) + "\n", entities


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


def build_header_line(emoji: List[Tuple[str, int]]) -> Tuple[str, list]:
    """Render one header on its own, starting at offset 0.

    The same builder the post shell uses, exposed so the admin bot can echo a
    saved header back to the admin (with its real custom emoji attached) to prove
    it renders. Returns ("", []) for an empty pool.
    """
    return _build_custom_emoji_header(emoji, 0)


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
        # path goes through (auto-publish, approve, preview).
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


def body_mentions_platform(lines: List[str], platform: Optional[str]) -> bool:
    """True when any body line already names the platform.

    The post shell no longer prints its own platform line, so the body is the
    single source of truth for the brand. This guard backs that up: if the body
    never says the platform, ``build_ai_message`` prints it as its own line so
    the brand is never lost.

    Matching is case-insensitive and word-boundary anchored on BOTH sides, so
    "netflix" does not match "netflixing" or "un-netflixed". A bare
    substring test would be wrong in both directions: it would suppress the
    platform line for an unrelated brand that merely contains the token, and
    print a half-duplicate when the body spells the platform with different
    punctuation ("safe 2 transact").
    """
    if not platform:
        return False
    token = re.sub(r"[^0-9a-z]+", "", platform.lower())
    if not token:
        return False
    for line in lines:
        if not line:
            continue
        # Collapse the same non-alphanumerics out of the line, then look for the
        # token only where it is a whole word.
        squashed = re.sub(r"[^0-9a-z]+", " ", line.lower())
        if re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", squashed):
            return True
    return False


def _asleep_footer_line() -> Optional[str]:
    """Return the buyer-asleep footer line, or None when the toggle is OFF.

    ``build_ai_message`` is the ONE place every published post is rendered
    (auto worker, admin approve, preview), so this single hook keeps
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
    header_emoji: Optional[List[Tuple[str, int]]] = None,
    post_number: Optional[int] = None,
    sanitize_body: bool = True,
    source_text: Optional[str] = None,
) -> Tuple[str, list]:
    """
    Build the final formatted post from AI-provided clean content lines.

    The AI already produced clean, emoji-free body text; this function only
    wraps it in the fixed emoji shell (header / country lines / footer).
    Emoji are allowed ONLY in the header, the country lines, and the
    footer (price/contact lines) — the body is deliberately kept emoji-free.
    When ``post_number`` is given, a small "#N" banner is prepended so each post
    is individually referenceable.

    The header is the admin's own custom emoji, supplied as ``header_emoji`` — a
    list of ``(alt, document_id)`` pairs from the headers pool in the database
    (see db.resolve_header_for_listing, which picks one at random per listing
    and then keeps it, so the admin preview always matches what publishes). The
    caller passes an already-resolved list, so this function stays a pure
    formatter: with ``header_emoji`` empty or None NO header line is rendered at
    all, which is what happens until the admin has added one.

    The platform name is owned by the BODY, not by this shell: the AI is told to
    open the body with the platform name, so no separate platform line is
    rendered and the brand never appears twice. Only when the body does not
    mention the platform anywhere (see body_mentions_platform) is the platform
    printed as its own line above the body, so the brand is never lost. The
    footer always carries the static "Price DM" line — prices are never
    computed, rendered, or used in publishing decisions here.

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
    reconstruct what was actually published).
    """
    if sanitize_body:
        lines = sanitize_body_lines(content_lines)[:40]
    else:
        lines = [ln.strip() for ln in content_lines if ln and ln.strip()][:40]
    if not lines:
        lines = ["Available"]

    # ── The platform is NOT printed as its own line. The AI is told to open the
    # body with the platform name (see ai_rephraser.ANALYZE_PROMPT), so the body
    # is the single source of truth and the brand can never appear twice. The
    # fallback below only fires when the body genuinely does not name the
    # platform anywhere, so the brand is never lost either.
    platform_display = platform.upper() if platform else "ACCOUNT"
    show_platform_line = not body_mentions_platform(lines, platform)

    parts: List[str] = []
    entities: list = []
    cursor = 0

    # ── Post number banner (lets subscribers + admin reference the exact post)
    if post_number is not None:
        post_num_line = f"#{post_number}\n"
        parts.append(post_num_line)
        cursor += _utf16_len(post_num_line)

    # ── Header line: the admin's custom emoji, already positioned for the whole
    # post because it is built against `cursor` (so a "#N" banner above can never
    # shift an entity off its glyph). Empty pool -> no header line, no entities.
    header_text, header_entities = _build_custom_emoji_header(
        header_emoji or [], cursor
    )
    entities.extend(header_entities)
    if header_text:
        parts.append(header_text)
        cursor += _utf16_len(header_text)
        # ── Empty line separating the header from the body
        parts.append("\n")
        cursor += 1

    # ── Platform fallback: only when the body never names the platform, so a
    # listing that already leads with the brand never shows it twice. It sits
    # directly above the body with no extra blank line, so the post keeps a
    # single blank line (the one under the header) in every layout.
    if show_platform_line:
        parts.append(f"{platform_display}\n")
        cursor += _utf16_len(platform_display) + 1

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