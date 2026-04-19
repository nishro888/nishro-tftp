"""YAML config loader with live reload + change notification."""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Callable

import yaml


class Config:
    """Thread-safe wrapper around a YAML config file.

    The object is treated as immutable snapshots: calls to :meth:`reload`
    or :meth:`save` replace ``self.data`` atomically, and subscribers are
    notified. Readers should hold a local reference to ``cfg.data`` for
    the duration of a logical operation if they need consistency.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._subscribers: list[Callable[["Config"], None]] = []
        self.data: dict[str, Any] = {}
        self.reload()

    # -- I/O -----------------------------------------------------------
    def reload(self) -> None:
        with self._lock:
            if not self.path.exists():
                raise FileNotFoundError(f"config file not found: {self.path}")
            with open(self.path, "r", encoding="utf-8") as fh:
                new_data = yaml.safe_load(fh) or {}
            self.data = new_data
        self._fire()

    def save(self, new_data: dict[str, Any]) -> None:
        """Write ``new_data`` to disk and swap it in."""
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.safe_dump(new_data, fh, sort_keys=False, default_flow_style=False)
            os.replace(tmp, self.path)
            self.data = copy.deepcopy(new_data)
        self._fire()

    # -- Subscriptions -------------------------------------------------
    def subscribe(self, cb: Callable[["Config"], None]) -> None:
        """Register a callback invoked whenever the config changes."""
        with self._lock:
            self._subscribers.append(cb)

    def unsubscribe(self, cb: Callable[["Config"], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(cb)
            except ValueError:
                pass

    def clear_subscribers(self) -> None:
        """Drop all subscribers - used when the app rebuilds itself
        from scratch (restart after a critical config change)."""
        with self._lock:
            self._subscribers.clear()

    def _fire(self) -> None:
        # Copy list under lock, dispatch outside the lock so subscribers
        # can freely call back into the config.
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(self)
            except Exception:  # noqa: BLE001 - subscribers must not kill us
                import traceback
                traceback.print_exc()

    # -- Convenience accessors ----------------------------------------
    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node
