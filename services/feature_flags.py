"""Feature Flags Infrastructure for Telegram Bot Overhaul (Roadmap §2A, §7.6).

Allows safe incremental rollouts per area without big-bang deployments.
Flags can be set via DB app_settings, environment variables, or programmatic defaults.
"""

import os
from typing import Optional
import db

# Flag names
UI_V2_RENDERER = "ui_v2_renderer"
UI_V2_REVIEW = "ui_v2_review"
UI_V2_POSTS = "ui_v2_posts"
UI_V2_SOURCES = "ui_v2_sources"
UI_V2_DELIVERY = "ui_v2_delivery"
UI_V2_SYSTEM = "ui_v2_system"
UI_V2_SINGLE_PUBLISH = "ui_v2_single_publish"

_DEFAULT_FLAGS = {
    UI_V2_RENDERER: False,  # Opt-in for V2 handlers to ensure legacy test suite compatibility
    UI_V2_REVIEW: True,
    UI_V2_POSTS: True,
    UI_V2_SOURCES: True,
    UI_V2_DELIVERY: True,
    UI_V2_SYSTEM: True,
    UI_V2_SINGLE_PUBLISH: False,  # Kept opt-in until worker-only test validation
}


def is_flag_enabled(flag_name: str, db_path: Optional[str] = None) -> bool:
    """Check if a feature flag is enabled.
    
    Precedence:
    1. Environment variable: UI_V2_<NAME>=1|true|0|false
    2. DB app_settings: ui_v2_<name> = 'true'|'false'
    3. Default value in _DEFAULT_FLAGS
    """
    env_key = flag_name.upper()
    if env_key in os.environ:
        val = os.environ[env_key].strip().lower()
        return val in ("1", "true", "yes", "on")

    try:
        db_val = db.get_setting(flag_name, db_path=db_path)
        if db_val is not None:
            return str(db_val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        pass

    return _DEFAULT_FLAGS.get(flag_name, False)


def set_flag(flag_name: str, enabled: bool, db_path: Optional[str] = None) -> None:
    """Persist a feature flag setting in the database."""
    val_str = "true" if enabled else "false"
    db.set_setting(flag_name, val_str, db_path=db_path)

