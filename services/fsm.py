"""Finite State Machine and session persistence services."""

import json
from typing import Any

import db


class _TrackedDict(dict):
    """A dictionary wrapper that propagates in-place mutations to its parent PersistentDict."""

    def __init__(self, parent: "PersistentDict", parent_key: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._parent = parent
        self._key = parent_key

    def _sync(self) -> None:
        if self._parent and getattr(self._parent, "_sync_enabled", False):
            self._parent._persist_key(self._key, self)

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, value)
        self._sync()

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        self._sync()

    def pop(self, key: Any, *args: Any) -> Any:
        res = super().pop(key, *args)
        self._sync()
        return res

    def clear(self) -> None:
        super().clear()
        self._sync()

    def update(self, *args: Any, **kwargs: Any) -> None:
        super().update(*args, **kwargs)
        self._sync()


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

    def _persist_key(self, key: Any, value: Any) -> None:
        if self._sync_enabled:
            try:
                db.set_setting(f"{self._prefix}{key}", json.dumps(value))
            except Exception:
                pass

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
                    if isinstance(val, dict) and not isinstance(val, _TrackedDict):
                        val = _TrackedDict(self, int_k, val)
                    super().__setitem__(int_k, val)
                except (ValueError, TypeError, json.JSONDecodeError):
                    try:
                        val = json.loads(v)
                        if isinstance(val, dict) and not isinstance(val, _TrackedDict):
                            val = _TrackedDict(self, clean_k, val)
                        super().__setitem__(clean_k, val)
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
        if isinstance(value, dict) and not isinstance(value, _TrackedDict):
            value = _TrackedDict(self, key, value)
        super().__setitem__(key, value)
        self._persist_key(key, value)

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
        temp = dict(*args, **kwargs)
        for k, v in temp.items():
            self[k] = v
