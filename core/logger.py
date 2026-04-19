"""Logging setup with rotating file + in-memory ring buffer for the UI."""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os
import threading
from typing import Any

from .constants import (
    LOG_FORMAT_DEFAULT,
    LOG_MEMORY_BUFFER,
    LOG_ROTATE_BACKUPS,
    LOG_ROTATE_BYTES,
)


class MemoryRingHandler(logging.Handler):
    """Keeps the last ``capacity`` formatted log lines in RAM.

    The web UI reads this buffer to seed the log viewer. Thread-safe
    via the handler-level lock inherited from :class:`logging.Handler`.
    """

    def __init__(self, capacity: int = LOG_MEMORY_BUFFER):
        super().__init__()
        self.capacity = capacity
        self.buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=capacity)
        self._listeners: list[Any] = []
        self._listener_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            self.buffer.append(entry)
            with self._listener_lock:
                listeners = list(self._listeners)
            for cb in listeners:
                try:
                    cb(entry)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.buffer)

    def add_listener(self, cb: Any) -> None:
        with self._listener_lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: Any) -> None:
        with self._listener_lock:
            if cb in self._listeners:
                self._listeners.remove(cb)


_memory_handler: MemoryRingHandler | None = None


def get_memory_handler() -> MemoryRingHandler:
    if _memory_handler is None:
        raise RuntimeError("logging not initialised - call setup_logging first")
    return _memory_handler


def setup_logging(cfg: dict[str, Any]) -> None:
    """Configure the root logger according to the config dict."""
    global _memory_handler

    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    fmt = cfg.get("format", LOG_FORMAT_DEFAULT)
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # Wipe existing handlers so reload() produces a clean slate.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_path = cfg.get("file")
    rotate = cfg.get("rotate", {}) or {}
    if file_path:
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        if rotate.get("enabled", True):
            fh: logging.Handler = logging.handlers.RotatingFileHandler(
                file_path,
                maxBytes=int(rotate.get("max_bytes", LOG_ROTATE_BYTES)),
                backupCount=int(rotate.get("backup_count", LOG_ROTATE_BACKUPS)),
                encoding="utf-8",
            )
        else:
            fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)

    _memory_handler = MemoryRingHandler(capacity=int(cfg.get("memory_buffer", LOG_MEMORY_BUFFER)))
    _memory_handler.setFormatter(formatter)
    root.addHandler(_memory_handler)
