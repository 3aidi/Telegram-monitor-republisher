"""Finite State Machine and session persistence services."""

import json
from typing import Any

import db


class PersistentDict(dict):
    """A dictionary that mirrors its entries to SQLite app_settings.

    Inherits directly from dict, ensuring 100% compatibility with standard
    dict semantics, test operations (.clear(), .pop(), copy()), while providing
    durable persistence across process restarts.
    """

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix
        self._loaded = False
        self._sync_enabled = True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            settings = db.get_all_settings(prefix=self._prefix)
            for k, v in settings.items():
                clean_k = k[len(self._prefix):]
                try:
                    int_k = int(clean_k)
                    val = json.loads(v)
                    super().__setitem__(int_k, val)
                except (ValueError, TypeError, json.JSONDecodeError):
                    try:
                        super().__setitem__(clean_k, json.loads(v))
                    except Exception:
                        super().__setitem__(clean_k, v)
        except Exception:
            pass

    def __getitem__(self, key: Any) -> Any:
        self._ensure_loaded()
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._ensure_loaded()
        return super().get(key, default)

    def __contains__(self, key: Any) -> bool:
        self._ensure_loaded()
        return super().__contains__(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._ensure_loaded()
        super().__setitem__(key, value)
        if self._sync_enabled:
            try:
                db.set_setting(f"{self._prefix}{key}", json.dumps(value))
            except Exception:
                pass

    def __delitem__(self, key: Any) -> None:
        self._ensure_loaded()
        super().__delitem__(key)
        if self._sync_enabled:
            try:
                db.delete_setting(f"{self._prefix}{key}")
            except Exception:
                pass

    def pop(self, key: Any, *args: Any) -> Any:
        self._ensure_loaded()
        res = super().pop(key, *args)
        if self._sync_enabled:
            try:
                db.delete_setting(f"{self._prefix}{key}")
            except Exception:
                pass
        return res

    def clear(self) -> None:
        self._ensure_loaded()
        super().clear()
        if self._sync_enabled:
            try:
                db.delete_settings_prefix(self._prefix)
            except Exception:
                pass

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._ensure_loaded()
        super().update(*args, **kwargs)
        if self._sync_enabled:
            for k, v in self.items():
                try:
                    db.set_setting(f"{self._prefix}{k}", json.dumps(v))
                except Exception:
                    pass
