"""Multi-reader / single-writer locks keyed by filename.

TFTP sessions check out a lock for the lifetime of a transfer. Reads
can coexist freely; a write excludes all readers and all other writes
on the same file. The lock manager is asyncio-native - it never blocks
the event loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class FileLockError(Exception):
    """Raised when a lock cannot be acquired without blocking."""


@dataclass
class _Entry:
    readers: int = 0
    writer: bool = False
    waiters: list[asyncio.Future] = field(default_factory=list)


class FileLockManager:
    """Per-path readers-writer locks.

    Usage::

        async with locks.read("firmware.bin"):
            ...

        async with locks.write("upload.bin"):
            ...

    The ``try_read`` / ``try_write`` helpers return a context manager
    that raises :class:`FileLockError` immediately if the lock can't
    be taken - that's what the TFTP dispatcher uses so it can return
    a TFTP error packet rather than stalling the session.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._mu = asyncio.Lock()

    # -- Public API ----------------------------------------------------
    def try_read(self, path: str) -> "_ReadCtx":
        return _ReadCtx(self, path)

    def try_write(self, path: str) -> "_WriteCtx":
        return _WriteCtx(self, path)

    # -- Internals -----------------------------------------------------
    async def _acquire_read(self, path: str) -> None:
        async with self._mu:
            entry = self._entries.setdefault(path, _Entry())
            if entry.writer:
                raise FileLockError(f"file busy (writer active): {path}")
            entry.readers += 1

    async def _release_read(self, path: str) -> None:
        async with self._mu:
            entry = self._entries.get(path)
            if not entry:
                return
            entry.readers = max(0, entry.readers - 1)
            self._gc(path, entry)

    async def _acquire_write(self, path: str) -> None:
        async with self._mu:
            entry = self._entries.setdefault(path, _Entry())
            if entry.writer or entry.readers > 0:
                raise FileLockError(f"file busy: {path}")
            entry.writer = True

    async def _release_write(self, path: str) -> None:
        async with self._mu:
            entry = self._entries.get(path)
            if not entry:
                return
            entry.writer = False
            self._gc(path, entry)

    def _gc(self, path: str, entry: _Entry) -> None:
        if not entry.writer and entry.readers == 0:
            self._entries.pop(path, None)


class _ReadCtx:
    def __init__(self, mgr: FileLockManager, path: str):
        self._mgr = mgr
        self._path = path

    async def __aenter__(self) -> None:
        await self._mgr._acquire_read(self._path)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._mgr._release_read(self._path)


class _WriteCtx:
    def __init__(self, mgr: FileLockManager, path: str):
        self._mgr = mgr
        self._path = path

    async def __aenter__(self) -> None:
        await self._mgr._acquire_write(self._path)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._mgr._release_write(self._path)
