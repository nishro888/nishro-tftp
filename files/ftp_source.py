"""FTP source - transparent TFTP -> FTP proxy via aioftp.

Instantiated by :class:`RouterSource` for the primary FTP block and for
the ``f::`` short-form trigger. Supports read/size/list and an optional
``upload_file`` used by the WRQ->FTP path when that's enabled in config.

Connection handling
-------------------
Each read/size/list opens a fresh control channel, performs its work,
and quits. TFTP transfers are infrequent on the backend side so this
keeps the code simple and dodges idle-disconnect edge cases without a
connection pool. All network ops are wrapped in :func:`asyncio.wait_for`
using the timeouts in :mod:`core.constants`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aioftp

from core.constants import (
    FTP_CONNECT_TIMEOUT,
    FTP_DOWNLOAD_CHUNK,
    FTP_SOCKET_TIMEOUT,
)

from .source import FileSource

log = logging.getLogger("nishro.files.ftp")


class FtpSource(FileSource):
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        root: str = "/",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.root = root if root.startswith("/") else "/" + root

    # -- Path helpers -------------------------------------------------
    def _join(self, filename: str) -> str:
        """Join ``filename`` onto :attr:`root`, rejecting ``..`` segments."""
        cleaned = filename.replace("\\", "/").lstrip("/")
        for part in cleaned.split("/"):
            if part == "..":
                raise ValueError("path traversal rejected")
        root = self.root.rstrip("/")
        return f"{root}/{cleaned}" if root else f"/{cleaned}"

    # -- Connection helper --------------------------------------------
    async def _connect(self) -> aioftp.Client:
        """Open a control channel, login, and return the client.

        Wrapped in ``wait_for`` because aioftp otherwise blocks
        indefinitely on an unresponsive server, which would wedge the
        TFTP session behind it.
        """
        client = aioftp.Client(socket_timeout=FTP_SOCKET_TIMEOUT)
        await asyncio.wait_for(
            client.connect(self.host, self.port), timeout=FTP_CONNECT_TIMEOUT
        )
        await asyncio.wait_for(
            client.login(self.user, self.password), timeout=FTP_CONNECT_TIMEOUT
        )
        return client

    async def _safe_quit(self, client: aioftp.Client) -> None:
        try:
            await asyncio.wait_for(client.quit(), timeout=FTP_CONNECT_TIMEOUT)
        except Exception:  # noqa: BLE001 - quit is best-effort
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # -- FileSource API -----------------------------------------------
    async def read(self, filename: str) -> Optional[bytes]:
        try:
            path = self._join(filename)
        except ValueError as e:
            log.info("ftp read rejected %r: %s", filename, e)
            return None

        log.debug("ftp GET %s@%s:%d%s", self.user, self.host, self.port, path)
        try:
            client = await self._connect()
        except (asyncio.TimeoutError, OSError, aioftp.StatusCodeError) as e:
            log.warning("ftp connect failed (%s:%d): %s", self.host, self.port, e)
            return None

        try:
            buf = bytearray()
            try:
                async with client.download_stream(path) as stream:
                    async for chunk in stream.iter_by_block(FTP_DOWNLOAD_CHUNK):
                        buf.extend(chunk)
            except aioftp.StatusCodeError as e:
                log.info("ftp download %s failed: %s", path, e)
                return None
            except asyncio.TimeoutError:
                log.warning("ftp download %s timed out", path)
                return None
            log.info("ftp GET %s -> %d bytes", path, len(buf))
            return bytes(buf)
        finally:
            await self._safe_quit(client)

    async def size(self, filename: str) -> Optional[int]:
        try:
            path = self._join(filename)
        except ValueError:
            return None
        try:
            client = await self._connect()
        except (asyncio.TimeoutError, OSError, aioftp.StatusCodeError) as e:
            log.warning("ftp connect failed (%s:%d): %s", self.host, self.port, e)
            return None
        try:
            try:
                info = await client.stat(path)
            except aioftp.StatusCodeError:
                return None
            try:
                return int(info.get("size")) if info else None
            except (TypeError, ValueError):
                return None
        finally:
            await self._safe_quit(client)

    async def upload_file(self, filename: str, local_path: str) -> bool:
        """Stream a local file up to ``filename`` relative to ``root``.

        Returns True on success, False on any error. Called from the
        WRQ path when ``files.allow_wrq_ftp`` is enabled and the target
        name is FTP-shaped.
        """
        try:
            path = self._join(filename)
        except ValueError as e:
            log.info("ftp upload rejected %r: %s", filename, e)
            return False
        log.info("ftp PUT %s@%s:%d%s", self.user, self.host, self.port, path)
        try:
            client = await self._connect()
        except (asyncio.TimeoutError, OSError, aioftp.StatusCodeError) as e:
            log.warning("ftp connect failed (%s:%d): %s", self.host, self.port, e)
            return False
        try:
            try:
                # make_parents creates missing directories along the way.
                await client.upload(local_path, path, write_into=True)
            except aioftp.StatusCodeError as e:
                log.info("ftp upload %s failed: %s", path, e)
                return False
            except asyncio.TimeoutError:
                log.warning("ftp upload %s timed out", path)
                return False
            log.info("ftp PUT %s -> ok", path)
            return True
        finally:
            await self._safe_quit(client)

    async def list(self) -> list[str]:
        try:
            client = await self._connect()
        except (asyncio.TimeoutError, OSError, aioftp.StatusCodeError) as e:
            log.warning("ftp connect failed (%s:%d): %s", self.host, self.port, e)
            return []
        out: list[str] = []
        try:
            try:
                async for path, info in client.list(self.root, recursive=True):
                    if str(info.get("type", "")) != "file":
                        continue
                    rel = str(path).lstrip("/")
                    root = self.root.lstrip("/")
                    if root and rel.startswith(root):
                        rel = rel[len(root):].lstrip("/")
                    out.append(rel)
            except aioftp.StatusCodeError as e:
                log.info("ftp list failed: %s", e)
            except asyncio.TimeoutError:
                log.warning("ftp list timed out")
        finally:
            await self._safe_quit(client)
        return sorted(out)
