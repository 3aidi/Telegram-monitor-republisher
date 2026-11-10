"""Edit-in-Place Screen Renderer `show()` (R-05, B-2, §7.1).

Replaces the destructive delete-and-send navigation with edit-in-place as the
primary interface pattern, with clean re-anchoring when arriving from commands,
reply keyboards, or media transitions.
"""

import logging
from typing import Any, List, Optional
import db

logger = logging.getLogger("renderer")

# Key used in app_settings to store the active console message ID
CONSOLE_MSG_ID_KEY = "console_msg_id"


def get_console_msg_id(db_path: Optional[str] = None) -> Optional[int]:
    """Retrieve the ID of the current active control center message."""
    try:
        val = db.get_setting(CONSOLE_MSG_ID_KEY, db_path=db_path)
        return int(val) if val and str(val).isdigit() else None
    except Exception:
        return None


def set_console_msg_id(msg_id: int, db_path: Optional[str] = None) -> None:
    """Store the ID of the current active control center message."""
    try:
        db.set_setting(CONSOLE_MSG_ID_KEY, str(msg_id), db_path=db_path)
    except Exception:
        pass


def _supports_param(fn: Any, param: str) -> bool:
    try:
        import inspect
        sig = inspect.signature(fn)
        return param in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except Exception:
        return False


async def show(
    event: Any,
    text: str,
    buttons: Optional[List[List[Any]]] = None,
    parse_mode: str = "markdown",
    formatting_entities: Optional[list] = None,
    reanchor: bool = False,
    admin_id: Optional[int] = None,
    client: Optional[Any] = None,
    db_path: Optional[str] = None,
) -> Any:
    """Render a screen edit-in-place with automatic re-anchoring and fallback.
    
    Parameters:
    - event: Telethon callback query event or NewMessage event.
    - text: Screen content.
    - buttons: Optional inline buttons matrix.
    - parse_mode: Default "markdown".
    - formatting_entities: Optional raw formatting entities.
    - reanchor: True to force a fresh message and retire the previous console message.
    - admin_id: Target admin user id.
    - client: Telethon bot client instance.
    """
    bot = client or getattr(event, "client", None)
    target_id = admin_id or getattr(event, "sender_id", None)
    send_buttons = buttons if buttons else None

    # Determine if event is an inline callback query that can be edited in place
    is_callback = hasattr(event, "edit") and hasattr(event, "data") and not reanchor

    if is_callback:
        try:
            edit_kw = {}
            if formatting_entities:
                edit_kw["formatting_entities"] = formatting_entities
            else:
                edit_kw["parse_mode"] = parse_mode

            # Attempt edit in-place
            msg = await event.edit(text, buttons=send_buttons, **edit_kw)
            mid = getattr(msg, "id", None) or getattr(event, "message_id", None)
            if mid:
                set_console_msg_id(mid, db_path=db_path)
            return msg
        except Exception as exc:
            err_str = str(exc).lower()
            if "message not modified" in err_str:
                return event
            logger.debug("In-place edit failed (%s), falling back to re-anchor", exc)
            # Fall through to re-anchor

    # Re-anchor path (new message sent, old console retired)
    prev_msg_id = get_console_msg_id(db_path=db_path)
    
    # Try to delete the old message if it's a callback event
    if hasattr(event, "delete") and hasattr(event, "data"):
        try:
            await event.delete()
        except Exception:
            pass

    # If there was a previous console message tracked and different from current, try to clean it
    if bot and target_id and prev_msg_id:
        try:
            if hasattr(bot, "delete_messages"):
                await bot.delete_messages(target_id, [prev_msg_id])
        except Exception:
            pass

    # Send fresh console message
    if bot and target_id:
        send_kw = {}
        if formatting_entities:
            send_kw["formatting_entities"] = formatting_entities
        else:
            send_kw["parse_mode"] = parse_mode

        try:
            sent = await bot.send_message(target_id, text, buttons=send_buttons, **send_kw)
            new_id = getattr(sent, "id", None)
            if new_id:
                set_console_msg_id(new_id, db_path=db_path)
            return sent
        except Exception:
            try:
                sent = await bot.send_message(target_id, text, buttons=None, parse_mode=None)
                new_id = getattr(sent, "id", None)
                if new_id:
                    set_console_msg_id(new_id, db_path=db_path)
                return sent
            except Exception:
                logger.exception("Failed to send screen to admin %s", target_id)
                return None
    elif hasattr(event, "reply"):
        try:
            sent = await event.reply(text, buttons=send_buttons, parse_mode=parse_mode)
            new_id = getattr(sent, "id", None)
            if new_id:
                set_console_msg_id(new_id, db_path=db_path)
            return sent
        except Exception:
            logger.exception("Failed to reply with screen")
            return None

    return None

