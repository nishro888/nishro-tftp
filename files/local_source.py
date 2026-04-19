"""Local folder source with recursive filename resolution."""
from __future__ import annotations

import logging
import os
from typing import Optional

import aiofiles

from .source import FileSource

log = logging.getLogger("nishro.files.local")


class LocalSource(FileSource):
    def __init__(self, root: str, recursive: bool = True):
        self.root = os.path.abspath(root)
        self.recursive = recursive
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, filename: str) -> Optional[str]:
        # Normalise client input and block traversal.
        cleaned = filename.replace("\\", "/").lstrip("/")
        parts = [p for p in cleaned.split("/") if p not in ("", ".")]
        for part in parts:
            if part == "..":
                return None
        if not parts:
            return None

        # Direct lookup first - handles both flat ("firmware.bin") and
        # nested paths ("d/abc", "vendor/model/firmware.bin").
        native = os.path.join(self.root, *parts)
        direct = os.path.abspath(native)
        if (direct == self.root or direct.startswith(self.root + os.sep)) and os.path.isfile(direct):
            return direct

        if not self.recursive:
            return None

        # Fall back to a recursive basename search so PXE clients that
        # ask for a flat "firmware.bin" still find it when the file
        # actually lives in a nested vendor folder.
        basename = parts[-1]
        for dirpath, _dirs, files in os.walk(self.root):
            if basename in files:
                candidate = os.path.join(dirpath, basename)
                return os.path.abspath(candidate)
        return None

    async def read(self, filename: str) -> Optional[bytes]:
        path = self._resolve(filename)
        if path is None:
            return None
        try:
            async with aiofiles.open(path, "rb") as fh:
                return await fh.read()
        except OSError as e:
            log.warning("failed to read %s: %s", path, e)
            return None

    async def size(self, filename: str) -> Optional[int]:
        path = self._resolve(filename)
        if path is None:
            return None
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    async def list(self) -> list[str]:
        out: list[str] = []
        for dirpath, _dirs, files in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            for name in files:
                if rel == ".":
                    out.append(name)
                else:
                    out.append(os.path.join(rel, name).replace(os.sep, "/"))
            if not self.recursive:
                break
        return sorted(out)

    async def list_detailed(self) -> list[dict]:
        """Single-pass walk that produces (name, size, mtime) per file.

        This is significantly cheaper than the generic default which
        does an ``os.stat`` per entry after the walk, because we already
        have the full path in hand.
        """
        out: list[dict] = []
        for dirpath, _dirs, files in os.walk(self.root):
            rel = os.path.relpath(dirpath, self.root)
            for name in files:
                logical = name if rel == "." else os.path.join(rel, name).replace(os.sep, "/")
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                    out.append({"name": logical, "size": int(st.st_size), "mtime": float(st.st_mtime)})
                except OSError:
                    out.append({"name": logical, "size": None, "mtime": None})
            if not self.recursive:
                break
        out.sort(key=lambda e: e["name"])
        return out
