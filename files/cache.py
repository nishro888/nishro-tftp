"""LRU file cache keyed by logical filename.

Holds decoded file bytes in RAM with a byte-budget cap. On a miss we
load through the configured :class:`FileSource`; concurrent requests
for the same missing file coalesce onto a single load task so 100
devices downloading the same firmware hit disk / FTP exactly once.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from typing import Optional

from core.stats import STATS

from .source import FileSource

log = logging.getLogger("nishro.cache")


class FileCache:
    def __init__(self, max_bytes: int, max_file_bytes: int, enabled: bool = True):
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes
        self.enabled = enabled
        self._entries: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._bytes = 0
        self._mu = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[Optional[bytes]]] = {}

    # -- Config hot-reload --------------------------------------------
    def update(self, max_bytes: int, max_file_bytes: int, enabled: bool) -> None:
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes
        self.enabled = enabled
        # Drop everything if the cache has been disabled; otherwise
        # just trim until we're back under the new budget.
        if not enabled:
            self._entries.clear()
            self._bytes = 0

    # -- Core ----------------------------------------------------------
    async def get_or_load(self, filename: str, source: FileSource) -> Optional[bytes]:
        if self.enabled:
            cached = self._peek(filename)
            if cached is not None:
                STATS.bump("cache_hits")
                return cached

        # Coalesce concurrent loaders
        async with self._mu:
            if filename in self._inflight:
                fut = self._inflight[filename]
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[filename] = fut
                asyncio.create_task(self._load(filename, source, fut))

        try:
            return await fut
        except Exception:  # noqa: BLE001
            return None

    async def _load(self, filename: str, source: FileSource, fut: asyncio.Future) -> None:
        try:
            data = await source.read(filename)
            if data is None:
                STATS.bump("cache_misses")
                fut.set_result(None)
                return
            STATS.bump("cache_misses")
            if self.enabled and len(data) <= self.max_file_bytes:
                await self._store(filename, data)
            fut.set_result(data)
        except Exception as e:  # noqa: BLE001
            log.exception("cache load failed for %s", filename)
            if not fut.done():
                fut.set_exception(e)
        finally:
            async with self._mu:
                self._inflight.pop(filename, None)

    async def _store(self, filename: str, data: bytes) -> None:
        async with self._mu:
            if filename in self._entries:
                self._bytes -= len(self._entries[filename])
                del self._entries[filename]
            self._entries[filename] = data
            self._bytes += len(data)
            # Evict LRU until we fit
            while self._bytes > self.max_bytes and self._entries:
                _k, v = self._entries.popitem(last=False)
                self._bytes -= len(v)

    def _peek(self, filename: str) -> Optional[bytes]:
        # Called on the async loop thread - dict ops are atomic enough
        # for our needs and we avoid taking the lock on every hit.
        data = self._entries.get(filename)
        if data is None:
            return None
        # Promote to most-recently-used
        try:
            self._entries.move_to_end(filename)
        except KeyError:
            pass
        return data

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "max_bytes": self.max_bytes,
            "used_bytes": self._bytes,
            "entries": len(self._entries),
            "filenames": list(self._entries.keys()),
        }

    def invalidate(self, filename: Optional[str] = None) -> None:
        if filename is None:
            self._entries.clear()
            self._bytes = 0
        elif filename in self._entries:
            self._bytes -= len(self._entries[filename])
            del self._entries[filename]
