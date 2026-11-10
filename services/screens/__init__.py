"""Screens package for V2 Telegram Bot UI."""
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class ScreenPayload:
    """Standard payload for rendering a screen in the Telegram bot."""
    text: str
    buttons: Optional[List[List[Any]]] = None
    parse_mode: Optional[str] = "markdown"
    formatting_entities: Optional[List[Any]] = None

