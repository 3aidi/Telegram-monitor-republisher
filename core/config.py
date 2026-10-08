"""Core runtime configuration and validation helpers."""

import os
from typing import Optional


def get_required_env(key: str, default: Optional[str] = None) -> str:
    """Retrieve an environment variable or raise ValueError if unset."""
    val = os.environ.get(key, default)
    if val is None:
        raise ValueError(f"Environment variable {key} is required but unset.")
    return val


def get_int_env(key: str, default: int = 0) -> int:
    """Retrieve an integer environment variable with fallback."""
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def get_bool_env(key: str, default: bool = False) -> bool:
    """Retrieve a boolean environment variable ('1', 'true', 'yes')."""
    raw = os.environ.get(key)
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
