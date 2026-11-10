"""Structured Callback Token Architecture and Legacy Token Map (R-13, B-1, §7.2, §10.3).

Provides typed parsing, building, and validation of callback tokens:
`<area>:<action>[:<arg1>[:<arg2>...]]` <= 64 bytes.

Maintains bidirectional routing and backwards compatibility for existing Telegram buttons.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import re

MAX_TOKEN_BYTES: int = 64

class Area:
    NAV = "nav"
    REV = "rev"
    POST = "post"
    SKP = "skp"
    SRC = "src"
    DLV = "dlv"
    GRP = "grp"
    HDR = "hdr"
    FWD = "fwd"
    SYS = "sys"
    EDT = "edt"
    WIZ = "wiz"


@dataclass(frozen=True)
class CallbackToken:
    area: str
    action: str
    args: Tuple[str, ...] = ()

    @property
    def raw(self) -> str:
        parts = [self.area, self.action] + list(self.args)
        return ":".join(parts)

    def __str__(self) -> str:
        return self.raw


def build_token(area: str, action: str, *args: Any) -> str:
    """Build and validate a structured callback token string."""
    str_args = [str(a).strip() for a in args if a is not None]
    parts = [str(area).strip(), str(action).strip()] + str_args
    token = ":".join(parts)
    raw_bytes = token.encode("utf-8")
    if len(raw_bytes) > MAX_TOKEN_BYTES:
        raise ValueError(
            f"Callback token {token!r} exceeds {MAX_TOKEN_BYTES} bytes ({len(raw_bytes)} bytes)"
        )
    return token


def parse_token(token_str: str) -> Optional[CallbackToken]:
    """Parse a callback token string into a CallbackToken object."""
    if not token_str:
        return None
    parts = str(token_str).strip().split(":")
    if len(parts) < 2:
        return None
    area, action = parts[0], parts[1]
    args = tuple(parts[2:])
    return CallbackToken(area=area, action=action, args=args)


# §10.3 Legacy Callback Compatibility Map
LEGACY_CALLBACK_MAP: Dict[str, str] = {
    # Navigation
    "menu:home": "nav:home",
    "menu:main": "nav:home",
    
    # Review queue
    "home:pending": "rev:card",
    "pend:page:0": "rev:card",
    "skip_all_pending": "rev:skall",
    "confirm_skip_all_pending": "rev:skally",
    "cancel_skip_all": "rev:cancelskall",
    
    # Sources / Suppliers
    "home:sources": "src:list:0",
    "menu:sources": "src:list:0",
    "supadd": "src:add",
    "supaddunresolved": "src:addall",
    
    # Delivery & Groups
    "menu:destinations": "dlv:list:0",
    "desthealth:view": "dlv:list:0",
    "destadd": "grp:add",
    "destaddunresolved": "grp:add",
    "destdel:all_dead": "grp:rmdead",
    "destdel:confirm_dead": "grp:rmdeady",
    "mainchan:view": "dlv:main",
    "mainchan:edit": "dlv:mainedit",
    
    # Headers
    "home:headers": "hdr:list",
    "headeradd": "hdr:add",
    
    # Forwarders
    "forwarders:view": "fwd:list",
    "fwdadd:start": "fwd:add",
    "fwdadd:phone": "fwd:phone",
    "fwdadd:qr": "fwd:qr",
    "fwdadd:resendsms": "fwd:resend",
    "fwdjoin:all": "fwd:joinall",
    
    # Posts
    "home:published": "post:list:0",
    "published:search": "post:find",
    
    # Skipped
    "home:skipped": "skp:list:0",
    
    # System & Actions
    "home:toggle": "sys:toggle_pause",
    "home:asleep": "sys:toggle_away",
    "wiz:cancel": "wiz:x",
    "reseed:yes": "sys:reseed_y",
}

# Parameterized regex patterns for legacy callbacks:
# (pattern, target_format_str)
LEGACY_REGEX_MAP: List[Tuple[re.Pattern, str]] = [
    # Review
    (re.compile(r"^approve:(\d+)$"), "rev:ok:{0}"),
    (re.compile(r"^skip:(\d+)$"), "rev:skip:{0}"),
    (re.compile(r"^edit:(\d+)$"), "rev:edit:{0}"),
    (re.compile(r"^preview:(\d+)$"), "rev:card:{0}"),
    
    # Posts
    (re.compile(r"^pub:page:(-?\d+)$"), "post:list:{0}"),
    (re.compile(r"^sold:(\d+)$"), "post:fill:{0}"),
    (re.compile(r"^sold_y:(\d+)$"), "post:filly:{0}"),
    (re.compile(r"^sold_cancel:(\d+)$"), "post:fillx:{0}"),
    (re.compile(r"^sold_info:(\d+)$"), "post:fill:{0}"),
    
    # Sources
    (re.compile(r"^sup:page:(-?\d+)$"), "src:list:{0}"),
    (re.compile(r"^sup:(\d+)$"), "src:v:{0}:0"),
    (re.compile(r"^suptoggle:(\d+)$"), "src:tg:{0}"),
    (re.compile(r"^supdel:(\d+)$"), "src:rm:{0}"),
    (re.compile(r"^del:yes:(\d+)$"), "src:rmy:{0}"),
    
    # Groups / Destinations
    (re.compile(r"^dest:page:(-?\d+)$"), "dlv:list:{0}"),
    (re.compile(r"^dest:(\d+)$"), "grp:v:{0}:0"),
    (re.compile(r"^desttoggle:(\d+)$"), "grp:tg:{0}"),
    (re.compile(r"^destlink:(\d+)$"), "grp:lnk:{0}"),
    (re.compile(r"^destjoin:(\d+)$"), "grp:join:{0}"),
    (re.compile(r"^destdel:(\d+)$"), "grp:rm:{0}"),
    (re.compile(r"^destdelyes:(\d+)$"), "grp:rmy:{0}"),
    
    # Headers
    (re.compile(r"^delheader:(\d+)$"), "hdr:del:{0}"),
    (re.compile(r"^undoheader:(\d+)$"), "hdr:undo:{0}"),
    
    # Forwarders
    (re.compile(r"^fwddel:(\d+)$"), "fwd:rm:{0}"),
    (re.compile(r"^fwddelyes:(\d+)$"), "fwd:rmy:{0}"),
    
    # Skipped
    (re.compile(r"^skip:page:(-?\d+)$"), "skp:list:{0}"),
    (re.compile(r"^reskip:(\d+)$"), "skp:re:{0}"),
    (re.compile(r"^reskip_lid:(\d+)$"), "skp:re:{0}"),
    (re.compile(r"^review_open:(\d+)$"), "skp:re:{0}"),
    
    # Edits
    (re.compile(r"^apply_edit:(\d+)$"), "edt:apply:{0}"),
    (re.compile(r"^ignore_edit:(\d+)$"), "edt:keep:{0}"),
]


def resolve_callback(raw_str: str) -> str:
    """Resolve a raw callback string (legacy or modern) to its canonical callback string."""
    clean = (raw_str or "").strip()
    if not clean:
        return ""
    
    # Exact legacy match
    if clean in LEGACY_CALLBACK_MAP:
        return LEGACY_CALLBACK_MAP[clean]
    
    # Regex parameterized legacy match
    for pattern, target_fmt in LEGACY_REGEX_MAP:
        m = pattern.match(clean)
        if m:
            return target_fmt.format(*m.groups())
            
    # Already a modern token or unmapped
    return clean

