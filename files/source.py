"""Abstract file source interface + routing factory.

Routing model
-------------
RRQ filenames are dispatched purely by shape:

* ``ftp://HOST[:PORT]/path``  -> explicit FTP. Credentials come from the
  primary FTP config block when ``HOST`` matches ``files.ftp.host``;
  otherwise an anonymous login is attempted.
* ``<trigger>NNN/path``       -> short-form FTP (default trigger ``f::``).
  The digit block is zero-padded and combined with ``folder_prefix`` to
  form a path on the configured prefix-FTP server. This exists for
  clients that can't hold a full ``ftp://`` URL in their filename field.
* anything else               -> local folder at ``files.local.root``.

There is no "default source" setting. The pattern of the name is the
instruction; the config only supplies credentials + roots.
"""
from __future__ import annotations

import abc
import logging
from typing import Optional
from urllib.parse import urlparse

from core.constants import (
    FTP_DEFAULT_PASSWORD,
    FTP_DEFAULT_PORT,
    FTP_DEFAULT_ROOT,
    FTP_DEFAULT_USER,
    LOCAL_ROOT_DEFAULT,
)

log = logging.getLogger("nishro.files.router")


class FileSource(abc.ABC):
    """Something that can resolve and read files by logical name."""

    @abc.abstractmethod
    async def read(self, filename: str) -> Optional[bytes]: ...

    @abc.abstractmethod
    async def size(self, filename: str) -> Optional[int]: ...

    @abc.abstractmethod
    async def list(self) -> list[str]:
        """List all files available. Best effort."""

    async def list_detailed(self) -> list[dict]:
        out: list[dict] = []
        for name in await self.list():
            sz = None
            try:
                sz = await self.size(name)
            except Exception:  # noqa: BLE001
                pass
            out.append({"name": name, "size": sz})
        return out

    def close(self) -> None:
        pass


def _parse_ftp_url(url: str) -> Optional[tuple[str, int, str]]:
    """Return ``(host, port, path)`` for an ``ftp://...`` URL, or ``None``.

    The path keeps its leading slash so FtpSource's ``root`` joining
    works correctly (an empty string means "server root").
    """
    try:
        u = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    if u.scheme.lower() != "ftp" or not u.hostname:
        return None
    port = int(u.port) if u.port else FTP_DEFAULT_PORT
    path = u.path or "/"
    return (u.hostname, port, path)


class RouterSource(FileSource):
    """Dispatches by filename pattern.

    Holds a LocalSource for the default case and a single FtpSource for
    the primary FTP block. Prefix (``f::``) routing is an additional
    FtpSource. ``ftp://`` URLs that target the primary host reuse its
    credentials; otherwise an anonymous FtpSource is created on demand.
    """

    def __init__(
        self,
        local,                          # LocalSource
        primary_ftp,                    # Optional[FtpSource]
        prefix_cfg: Optional[dict],     # files.ftp_prefix block
    ) -> None:
        self.local = local
        self.primary_ftp = primary_ftp
        self.prefix_ftp = None
        self.trigger = ""
        self.folder_prefix = ""
        self.digit_pad = 4
        if prefix_cfg and prefix_cfg.get("enabled"):
            from .ftp_source import FtpSource
            from core.constants import FTP_PREFIX_DIGIT_PAD, FTP_PREFIX_FOLDER, FTP_PREFIX_TRIGGER
            self.trigger = str(prefix_cfg.get("trigger", FTP_PREFIX_TRIGGER))
            self.folder_prefix = str(prefix_cfg.get("folder_prefix", FTP_PREFIX_FOLDER))
            self.digit_pad = int(prefix_cfg.get("digit_pad", FTP_PREFIX_DIGIT_PAD))
            self.prefix_ftp = FtpSource(
                host=str(prefix_cfg.get("host", "")),
                port=int(prefix_cfg.get("port", FTP_DEFAULT_PORT)),
                user=str(prefix_cfg.get("user", FTP_DEFAULT_USER)),
                password=str(prefix_cfg.get("password", FTP_DEFAULT_PASSWORD)),
                root=str(prefix_cfg.get("root", FTP_DEFAULT_ROOT)),
            )

    # -- dispatch ------------------------------------------------------
    def _resolve(self, filename: str):
        """Pick the target FileSource + rewritten path for ``filename``.

        Returns ``(source, inner_filename)`` or ``(None, None)`` when
        the name looks like an FTP reference we can't resolve.
        """
        # 1. Full ftp:// URL - host is in the name itself.
        low = filename.lower()
        if low.startswith("ftp://"):
            parsed = _parse_ftp_url(filename)
            if parsed is None:
                log.info("router: malformed ftp url %r", filename)
                return (None, None)
            host, port, path = parsed
            src = self._ftp_for_host(host, port)
            # FtpSource already handles leading-slash root joining.
            return (src, path.lstrip("/"))

        # 2. Short-form trigger (f::NNN/file).
        if self.prefix_ftp and self.trigger and filename.startswith(self.trigger):
            rewritten = self._rewrite_prefix(filename)
            if rewritten is None:
                return (None, None)
            return (self.prefix_ftp, rewritten)

        # 3. Default - local folder.
        return (self.local, filename)

    def _rewrite_prefix(self, filename: str) -> Optional[str]:
        rest = filename[len(self.trigger):].replace("\\", "/")
        parts = rest.split("/", 1)
        if len(parts) != 2:
            log.debug("prefix rewrite rejected (no separator): %r", filename)
            return None
        digits, tail = parts
        if not digits or not digits.isdigit():
            log.debug("prefix rewrite rejected (non-digit block %r)", digits)
            return None
        padded = digits.zfill(self.digit_pad)
        return f"{self.folder_prefix}{padded}/{tail}"

    def _ftp_for_host(self, host: str, port: int):
        """Return an FtpSource targetting ``host:port``.

        Reuses primary_ftp (with its credentials) when the host matches;
        otherwise builds a one-shot anonymous FtpSource. The anon source
        isn't cached - a bare ``ftp://`` RRQ is rare enough that the
        simpler code wins.
        """
        from .ftp_source import FtpSource
        if (
            self.primary_ftp is not None
            and host.lower() == (self.primary_ftp.host or "").lower()
            and port == int(self.primary_ftp.port)
        ):
            return self.primary_ftp
        log.info("router: anonymous FTP for %s:%d", host, port)
        return FtpSource(host=host, port=port, user="anonymous", password="anonymous@", root="/")

    # -- FileSource API -----------------------------------------------
    async def read(self, filename: str) -> Optional[bytes]:
        src, inner = self._resolve(filename)
        if src is None:
            return None
        return await src.read(inner)

    async def size(self, filename: str) -> Optional[int]:
        src, inner = self._resolve(filename)
        if src is None:
            return None
        return await src.size(inner)

    async def upload(self, filename: str, local_path: str) -> bool:
        """Route a WRQ upload to its FTP destination.

        Only FTP-shaped filenames (``ftp://`` or the trigger) are
        accepted; local-targeted WRQs are handled by the session
        writing directly to ``write_root``. Returns True on success.
        """
        low = filename.lower()
        if low.startswith("ftp://"):
            parsed = _parse_ftp_url(filename)
            if parsed is None:
                log.info("router upload: malformed ftp url %r", filename)
                return False
            host, port, path = parsed
            src = self._ftp_for_host(host, port)
            return await src.upload_file(path.lstrip("/"), local_path)
        if self.prefix_ftp and self.trigger and filename.startswith(self.trigger):
            rewritten = self._rewrite_prefix(filename)
            if rewritten is None:
                return False
            return await self.prefix_ftp.upload_file(rewritten, local_path)
        log.warning("router upload: %r is not FTP-shaped", filename)
        return False

    async def list(self) -> list[str]:
        # Only list the local folder. The FTP targets can be browsed
        # via the Files tab; we deliberately don't crawl them from here.
        return await self.local.list()

    def close(self) -> None:
        for s in (self.local, self.primary_ftp, self.prefix_ftp):
            if s is None:
                continue
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def build_source(cfg: dict) -> FileSource:
    """Factory for the routing source.

    The config no longer carries a "source kind" field: routing is
    decided at request time by the shape of the filename (see module
    docstring). This factory just wires up the pieces needed for each
    possible branch.
    """
    files_cfg = cfg.get("files", {}) or {}

    from .local_source import LocalSource
    local_cfg = files_cfg.get("local", {}) or {}
    local = LocalSource(
        root=local_cfg.get("root", LOCAL_ROOT_DEFAULT),
        recursive=bool(local_cfg.get("recursive", True)),
    )

    from .ftp_source import FtpSource
    ftp_cfg = files_cfg.get("ftp", {}) or {}
    primary_ftp = None
    if ftp_cfg.get("host"):
        primary_ftp = FtpSource(
            host=str(ftp_cfg.get("host", "")),
            port=int(ftp_cfg.get("port", FTP_DEFAULT_PORT)),
            user=str(ftp_cfg.get("user", FTP_DEFAULT_USER)),
            password=str(ftp_cfg.get("password", FTP_DEFAULT_PASSWORD)),
            root=str(ftp_cfg.get("root", FTP_DEFAULT_ROOT)),
        )

    prefix_cfg = files_cfg.get("ftp_prefix", {}) or {}
    return RouterSource(local=local, primary_ftp=primary_ftp, prefix_cfg=prefix_cfg)
