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


def validate_startup_config() -> list[str]:
    """Validate core environment variables on startup.

    Uses pydantic if installed, with strict deterministic checks as fallback.
    Returns a list of human-readable error messages (empty if valid).
    """
    errors: list[str] = []
    try:
        from pydantic import BaseModel, Field, ValidationError

        class EnvironmentSettings(BaseModel):
            API_ID: int = Field(gt=0, description="Telegram API ID")
            API_HASH: str = Field(min_length=10, description="Telegram API Hash")
            ADMIN_USER_ID: int = Field(gt=0, description="Admin Telegram User ID")
            PUBLISH_INTERVAL: float = Field(ge=0.0, default=0.5)
            PUBLISH_MAX_RETRIES: int = Field(ge=1, le=20, default=4)
            DEDUP_HOURS: int = Field(ge=1, le=168, default=8)

        data = {
            "API_ID": os.environ.get("API_ID", 0),
            "API_HASH": os.environ.get("API_HASH", ""),
            "ADMIN_USER_ID": os.environ.get("ADMIN_USER_ID", 0),
            "PUBLISH_INTERVAL": os.environ.get("PUBLISH_INTERVAL", 0.5),
            "PUBLISH_MAX_RETRIES": os.environ.get("PUBLISH_MAX_RETRIES", 4),
            "DEDUP_HOURS": os.environ.get("DEDUP_HOURS", 8),
        }
        try:
            EnvironmentSettings(**data)
        except ValidationError as val_err:
            for err in val_err.errors():
                loc = ".".join(str(p) for p in err.get("loc", []))
                errors.append(f"{loc}: {err.get('msg', 'invalid value')}")
    except ImportError:
        api_id = get_int_env("API_ID", 0)
        if api_id <= 0:
            errors.append("API_ID must be a positive integer")
        if not os.environ.get("API_HASH", "").strip():
            errors.append("API_HASH must not be empty")
        admin_id = get_int_env("ADMIN_USER_ID", 0)
        if admin_id <= 0:
            errors.append("ADMIN_USER_ID must be a positive integer")

    return errors

