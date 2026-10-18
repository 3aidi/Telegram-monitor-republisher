import re
from typing import List, Tuple, Union

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

