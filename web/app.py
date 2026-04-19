"""FastAPI + WebSocket web UI for Nishro TFTP.

All state lives in the ``AppState`` dataclass which is injected at
startup time by ``main.py``. The UI talks to these routes:

  * ``GET  /``                  - static single-page app
  * ``GET  /api/stats``         - JSON stats snapshot
  * ``GET  /api/config``        - current YAML config as JSON
  * ``POST /api/config``        - save new config and hot-reload
  * ``GET  /api/sessions``      - active TFTP sessions
  * ``GET  /api/files``         - list of files in the source
  * ``GET  /api/logs``          - snapshot of the in-memory log ring
  * ``WS   /ws``                - live dashboard + log stream
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import psutil
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from fastapi import Cookie, Request as FastAPIRequest

from core import carryover, daily_stats
from core.auth import AuthStore
from core.config import Config
from core.users import UserStore
from core.constants import (
    WEB_STATS_INTERVAL_DEFAULT,
    WEB_WS_INIT_LOG_COUNT,
    WEB_WS_LOG_QUEUE,
    WEB_WS_LOGS_PER_TICK,
)
from core.logger import get_memory_handler
from core.stats import STATS

log = logging.getLogger("nishro.web")


@dataclass
class AppState:
    config: Config
    tftp_server: Any            # tftp.server.TftpServer | CoreEngine | None
    cache: Any                  # files.cache.FileCache
    source_provider: Any        # callable -> FileSource
    reload_hook: Any            # callable(new_cfg) -> None
    auth: AuthStore | None = None
    users: UserStore | None = None
    # Populated by main.py when the engine fails to start (bad/missing
    # NIC). When set, the UI renders a dashboard banner and session
    # endpoints return empty lists instead of raising.
    nic_error: str | None = None


# -- Cached psutil Process handle + snapshot ----------------------------
# Creating a new psutil.Process() and calling memory_info() / cpu_percent()
# on every WS tick (~1 s) is surprisingly expensive on Windows. Cache the
# Process handle and throttle the calls.

_proc: psutil.Process | None = None
_child_proc: psutil.Process | None = None
_child_pid: int | None = None
_child_primed: bool = False
_proc_cache: dict = {"rss": 0, "cpu": 0.0, "sys_cpu": 0.0, "cores": 1, "ts": 0.0}
_proc_primed: bool = False
_PROC_INTERVAL = 2.0  # seconds between psutil refreshes


def _child_cpu_mem(child_pid: int | None) -> tuple[float, int]:
    """Sample CPU% and RSS for the C engine subprocess, if running.

    Returns (cpu_of_1core, rss_bytes). Primes a fresh ``psutil.Process``
    handle when the PID changes; a meaningless 0.0 is expected on the
    very first call after priming.
    """
    global _child_proc, _child_pid, _child_primed
    if not child_pid:
        _child_proc = None
        _child_pid = None
        _child_primed = False
        return 0.0, 0
    try:
        if _child_proc is None or _child_pid != child_pid:
            _child_proc = psutil.Process(child_pid)
            _child_pid = child_pid
            _child_primed = False
        if not _child_primed:
            _child_proc.cpu_percent(interval=None)
            _child_primed = True
            return 0.0, int(_child_proc.memory_info().rss)
        return float(_child_proc.cpu_percent(interval=None)), int(_child_proc.memory_info().rss)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
        _child_proc = None
        _child_pid = None
        _child_primed = False
        return 0.0, 0
    except Exception:  # noqa: BLE001
        return 0.0, 0


def _proc_stats(child_pid: int | None = None) -> tuple[int, float, float, int]:
    """Return (rss_bytes, process_cpu_of_1core, system_cpu_total, cpu_count).

    ``process_cpu_of_1core`` is psutil's ``Process.cpu_percent`` which is
    not normalized to core count - a value of 100 means one full core.
    Divide by ``cpu_count`` to get "% of total machine".
    ``system_cpu_total`` is machine-wide load from ``psutil.cpu_percent``
    (already 0-100 across all cores).

    When ``child_pid`` is given (C engine mode), the packet path runs in
    a subprocess and the Python parent sits near 0%. CPU and RSS for the
    child are sampled and added on top so the dashboard reflects actual
    work, not just the supervisor.
    """
    global _proc, _proc_primed
    now = time.monotonic()
    if _proc_primed and now - _proc_cache["ts"] < _PROC_INTERVAL:
        return (
            _proc_cache["rss"],
            _proc_cache["cpu"],
            _proc_cache["sys_cpu"],
            _proc_cache["cores"],
        )
    try:
        if _proc is None:
            _proc = psutil.Process()
        if not _proc_primed:
            # psutil.cpu_percent(interval=None) returns a meaningless 0.0
            # on the first call -- it needs a prior sample to compute the
            # delta. Prime both and return last cached values so the UI
            # shows real numbers on the next refresh.
            _proc.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None)
            child_cpu, child_rss = _child_cpu_mem(child_pid)
            _proc_cache["rss"] = _proc.memory_info().rss + child_rss
            _proc_cache["cores"] = psutil.cpu_count(logical=True) or 1
            _proc_cache["ts"] = now
            _proc_primed = True
            return (
                _proc_cache["rss"],
                _proc_cache["cpu"],
                _proc_cache["sys_cpu"],
                _proc_cache["cores"],
            )
        mem = _proc.memory_info().rss
        cpu = _proc.cpu_percent(interval=None)
        sys_cpu = psutil.cpu_percent(interval=None)
        cores = psutil.cpu_count(logical=True) or 1
        child_cpu, child_rss = _child_cpu_mem(child_pid)
        cpu += child_cpu
        mem += child_rss
    except Exception:  # noqa: BLE001
        mem = 0
        cpu = 0.0
        sys_cpu = 0.0
        cores = 1
    _proc_cache["rss"] = mem
    _proc_cache["cpu"] = cpu
    _proc_cache["sys_cpu"] = sys_cpu
    _proc_cache["cores"] = cores
    _proc_cache["ts"] = now
    return mem, cpu, sys_cpu, cores


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Nishro TFTP", version="1.0")

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Cache the index.html content at startup (it's static).
    _index_html: str = ""
    try:
        with open(os.path.join(static_dir, "index.html"), "r", encoding="utf-8") as fh:
            _index_html = fh.read()
    except Exception:  # noqa: BLE001
        log.exception("failed to read index.html")

    @app.middleware("http")
    async def _track_visitors(request: FastAPIRequest, call_next):
        """Mark unique web-UI visitor IPs per day (powers "visitors today").

        Skips /static/* and /ws so polling/asset traffic doesn't inflate
        the count. Errors are swallowed -- tracking must never block a
        request.
        """
        path = request.url.path or ""
        if not path.startswith("/static/") and path != "/ws":
            client = request.client
            if client and client.host:
                try:
                    daily_stats.record_visitor(client.host)
                except Exception:  # noqa: BLE001
                    pass
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_index_html)

    @app.get("/api/stats")
    async def api_stats() -> dict:
        return _stats_payload(state)

    @app.get("/api/config")
    async def api_config_get() -> dict:
        return state.config.data

    @app.post("/api/config")
    async def api_config_post(request: FastAPIRequest, new_cfg: dict) -> dict:
        _require_admin(request)
        try:
            state.config.save(new_cfg)
            if state.reload_hook:
                state.reload_hook(new_cfg)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            log.exception("config save failed")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.get("/api/sessions")
    async def api_sessions() -> list[dict]:
        if state.tftp_server is None:
            return []
        return state.tftp_server.active_sessions()

    @app.get("/api/sessions/history")
    async def api_sessions_history() -> list[dict]:
        if state.tftp_server is None:
            return []
        return list(state.tftp_server.session_history)

    @app.get("/api/sessions/daily")
    async def api_sessions_daily(days: int = 30) -> dict:
        """Per-day session totals for the last ``days`` days.

        Returns ``{days: [{date, total, completed, failed, ...}, ...],
        today: {...}}`` ready for the dashboard chart.
        """
        inst = daily_stats.get()
        if inst is None:
            return {"days": [], "today": {}, "user_counts": {"today": 0, "total": 0},
                    "reach_counts": {"visitors_today": 0, "devices_today": 0}}
        try:
            days = max(1, min(int(days), 180))
        except (TypeError, ValueError):
            days = 30
        return {
            "days": inst.recent(days),
            "today": inst.today(),
            "user_counts": inst.user_counts(),
            "reach_counts": inst.reach_counts(),
        }

    @app.get("/api/cache")
    async def api_cache() -> dict:
        return state.cache.snapshot()

    @app.post("/api/cache/invalidate")
    async def api_cache_invalidate(request: FastAPIRequest, payload: dict) -> dict:
        _require_admin(request)
        state.cache.invalidate(payload.get("filename"))
        with STATS._lock:
            STATS.counters.cache_hits = 0
            STATS.counters.cache_misses = 0
        return {"ok": True}

    @app.get("/api/files")
    async def api_files() -> list[str]:
        src = state.source_provider()
        return await src.list()

    @app.get("/api/files_detailed")
    async def api_files_detailed() -> list[dict]:
        src = state.source_provider()
        try:
            return await src.list_detailed()
        except Exception as e:  # noqa: BLE001
            log.exception("list_detailed failed")
            return [{"name": n, "size": None} for n in await src.list()] + [
                {"error": str(e)}
            ]

    @app.get("/api/files/download")
    async def api_files_download(name: str = Query(..., min_length=1)) -> Response:
        src = state.source_provider()
        try:
            data = await state.cache.get_or_load(name, src)
        except Exception as e:  # noqa: BLE001
            log.exception("file download failed")
            raise HTTPException(status_code=500, detail=str(e))
        if data is None:
            raise HTTPException(status_code=404, detail="not found")
        safe = name.replace("\\", "/").split("/")[-1] or "download.bin"
        return Response(
            content=bytes(data),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe}"'},
        )

    # -- file browser endpoints for the 3-panel UI --------------------
    @app.get("/api/browse/local")
    async def api_browse_local(path: str = "") -> dict:
        root = state.config.get("files", "local", "root", default="./tftp_root")
        return _browse_dir(root, path)

    @app.get("/api/browse/wrq")
    async def api_browse_wrq(path: str = "") -> dict:
        root = state.config.get("files", "write_root", default="./tftp_uploads")
        return _browse_dir(root, path)

    @app.get("/api/browse/ftp")
    async def api_browse_ftp(path: str = "/") -> list[dict]:
        ftp_cfg = state.config.get("files", "ftp", default={}) or {}
        host = ftp_cfg.get("host", "")
        port = int(ftp_cfg.get("port", 21))
        user = ftp_cfg.get("user", "")
        password = ftp_cfg.get("password", "")
        ftp_root = ftp_cfg.get("root", "/")
        if not host:
            raise HTTPException(status_code=400, detail="FTP not configured")
        import aioftp
        try:
            client = aioftp.Client(socket_timeout=30)
            await asyncio.wait_for(client.connect(host, port), timeout=8)
            await asyncio.wait_for(client.login(user, password), timeout=8)
            browse_path = (ftp_root.rstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/"
            entries: list[dict] = []
            async for fpath, info in client.list(browse_path):
                name = str(fpath).rsplit("/", 1)[-1]
                if name in (".", ".."):
                    continue
                is_dir = info.get("type") == "dir"
                size = int(info.get("size", 0)) if not is_dir else None
                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": size,
                })
            await client.quit()
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            return entries
        except Exception as e:
            log.exception("FTP browse failed for path=%s", path)
            raise HTTPException(status_code=500, detail=str(e))

    # -- filesystem mutation endpoints (admin) -------------------------
    def _tree_root(tree: str) -> str:
        if tree == "local":
            return state.config.get("files", "local", "root", default="./tftp_root")
        if tree == "wrq":
            return state.config.get("files", "write_root", default="./tftp_uploads")
        raise HTTPException(status_code=400, detail="invalid tree")

    def _safe_target(tree: str, rel: str) -> tuple[str, str]:
        """Return (abs_base, abs_target). Rejects traversal."""
        root = _tree_root(tree)
        base = os.path.abspath(root)
        rel_norm = (rel or "").replace("\\", "/").lstrip("/")
        target = os.path.abspath(os.path.join(base, rel_norm))
        if not (target == base or target.startswith(base + os.sep)):
            raise HTTPException(status_code=400, detail="path escapes root")
        return base, target

    @app.post("/api/fs/mkdir")
    async def api_fs_mkdir(payload: dict) -> dict:
        tree = str(payload.get("tree", ""))
        path = str(payload.get("path", ""))
        if not path.strip("/"):
            raise HTTPException(status_code=400, detail="empty path")
        _base, target = _safe_target(tree, path)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state.cache.invalidate()
        return {"ok": True}

    @app.post("/api/fs/upload")
    async def api_fs_upload(
        path: str = Form(""),
        file: UploadFile = File(...),
    ) -> dict:
        """Upload a file into the RRQ (local) tree under the given subpath."""
        base, target_dir = _safe_target("local", path)
        if not os.path.isdir(target_dir):
            raise HTTPException(status_code=400, detail="destination is not a directory")
        name = os.path.basename(file.filename or "")
        if not name or name in (".", ".."):
            raise HTTPException(status_code=400, detail="invalid filename")
        dst = os.path.join(target_dir, name)
        _safe_target("local", os.path.relpath(dst, base).replace(os.sep, "/"))
        max_bytes = int(state.config.get("files", "max_upload_size", default=0) or 0)
        written = 0
        try:
            with open(dst, "wb") as fp:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        fp.close()
                        try:
                            os.remove(dst)
                        except OSError:
                            pass
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload size exceeds max_upload_size {max_bytes}",
                        )
                    fp.write(chunk)
        except HTTPException:
            raise
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state.cache.invalidate()
        return {"ok": True, "name": name, "bytes": written}

    @app.post("/api/fs/rename")
    async def api_fs_rename(payload: dict) -> dict:
        tree = str(payload.get("tree", ""))
        path = str(payload.get("path", ""))
        new_name = str(payload.get("new_name", "")).strip()
        if not path or not new_name:
            raise HTTPException(status_code=400, detail="path and new_name required")
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            raise HTTPException(status_code=400, detail="invalid new_name")
        _base, src = _safe_target(tree, path)
        if not os.path.exists(src):
            raise HTTPException(status_code=404, detail="source not found")
        dst = os.path.join(os.path.dirname(src), new_name)
        # Ensure dst still under the tree root
        _safe_target(tree, os.path.relpath(dst, _base).replace(os.sep, "/"))
        try:
            os.replace(src, dst)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state.cache.invalidate()
        return {"ok": True}

    @app.post("/api/fs/delete")
    async def api_fs_delete(payload: dict) -> dict:
        import shutil
        tree = str(payload.get("tree", ""))
        path = str(payload.get("path", ""))
        if not path.strip("/"):
            raise HTTPException(status_code=400, detail="refusing to delete tree root")
        base, target = _safe_target(tree, path)
        if target == base:
            raise HTTPException(status_code=400, detail="refusing to delete tree root")
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail="not found")

        def _do_delete() -> None:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)

        try:
            await asyncio.to_thread(_do_delete)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state.cache.invalidate()
        return {"ok": True}

    @app.post("/api/fs/copy")
    async def api_fs_copy(payload: dict) -> dict:
        """Copy a file or folder from the uploads tree into the local tree.

        Payload: {src_path, dst_dir} - src_path is under wrq root, dst_dir
        is the folder under the local root where the entry is placed with
        the same basename. Overwrites if present. Size-capped by
        files.max_copy_size_local (falls back to legacy files.max_copy_size).
        """
        import shutil
        src_path = str(payload.get("src_path", ""))
        dst_dir = str(payload.get("dst_dir", ""))
        if not src_path:
            raise HTTPException(status_code=400, detail="src_path required")
        _sb, src = _safe_target("wrq", src_path)
        dst_base, dst_parent = _safe_target("local", dst_dir)
        if not os.path.exists(src):
            raise HTTPException(status_code=404, detail="source not found")
        if not os.path.isdir(dst_parent):
            raise HTTPException(status_code=400, detail="destination is not a directory")

        # max_copy_size_local governs uploads-folder -> RRQ-root copies.
        # Falls back to the legacy max_copy_size for back-compat.
        legacy_cap = int(state.config.get("files", "max_copy_size", default=500 * 1024 * 1024) or 0)
        max_bytes = int(state.config.get("files", "max_copy_size_local", default=legacy_cap) or 0)
        total = await asyncio.to_thread(_measure_size, src)
        if max_bytes and total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"copy size {total} exceeds max_copy_size_local {max_bytes}",
            )

        name = os.path.basename(src.rstrip(os.sep))
        dst = os.path.join(dst_parent, name)
        _safe_target("local", os.path.relpath(dst, dst_base).replace(os.sep, "/"))

        def _do_copy() -> None:
            if os.path.isdir(src) and not os.path.islink(src):
                if os.path.exists(dst):
                    if os.path.isdir(dst) and not os.path.islink(dst):
                        shutil.rmtree(dst)
                    else:
                        os.remove(dst)
                shutil.copytree(src, dst)
            else:
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copy2(src, dst)

        try:
            # Run the blocking I/O in a thread so the event loop keeps
            # servicing the TFTP path without stalling on a big copy.
            await asyncio.to_thread(_do_copy)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        state.cache.invalidate()
        return {"ok": True, "bytes": total}

    @app.post("/api/fs/copy_ftp")
    async def api_fs_copy_ftp(payload: dict) -> dict:
        """Copy a file or directory from the FTP server into the local tree.

        Payload: {src_path, is_dir, dst_dir} - src_path is an FTP-relative
        path (under the ftp root). dst_dir is a folder under the local
        tree. Size-capped by files.max_copy_size_ftp (falls back to legacy
        files.max_copy_size). Overwrites on conflict.
        """
        import shutil
        import aioftp
        src_path = str(payload.get("src_path", ""))
        is_dir = bool(payload.get("is_dir", False))
        dst_dir = str(payload.get("dst_dir", ""))
        if not src_path:
            raise HTTPException(status_code=400, detail="src_path required")

        ftp_cfg = state.config.get("files", "ftp", default={}) or {}
        host = ftp_cfg.get("host", "")
        port = int(ftp_cfg.get("port", 21))
        user = ftp_cfg.get("user", "")
        password = ftp_cfg.get("password", "")
        ftp_root = ftp_cfg.get("root", "/")
        if not host:
            raise HTTPException(status_code=400, detail="FTP not configured")

        dst_base, dst_parent = _safe_target("local", dst_dir)
        if not os.path.isdir(dst_parent):
            raise HTTPException(status_code=400, detail="destination is not a directory")

        # max_copy_size_ftp governs FTP-browser -> RRQ-root copies.
        # Falls back to the legacy max_copy_size for back-compat.
        legacy_cap = int(state.config.get("files", "max_copy_size", default=500 * 1024 * 1024) or 0)
        max_bytes = int(state.config.get("files", "max_copy_size_ftp", default=legacy_cap) or 0)
        src_abs = (ftp_root.rstrip("/") + "/" + src_path.lstrip("/")).rstrip("/") or "/"
        name = src_path.rstrip("/").rsplit("/", 1)[-1]
        if not name:
            raise HTTPException(status_code=400, detail="invalid src_path")
        dst = os.path.join(dst_parent, name)
        _safe_target("local", os.path.relpath(dst, dst_base).replace(os.sep, "/"))

        async def _ftp_measure(client, path: str) -> int:
            total = 0
            async for fpath, info in client.list(path, recursive=True):
                if info.get("type") != "dir":
                    try:
                        total += int(info.get("size", 0))
                    except (TypeError, ValueError):
                        pass
            return total

        async def _ftp_download_tree(client, remote: str, local: str) -> None:
            os.makedirs(local, exist_ok=True)
            async for fpath, info in client.list(remote, recursive=True):
                rel = str(fpath).replace("\\", "/")
                base = remote.rstrip("/")
                if rel.startswith(base + "/"):
                    rel = rel[len(base) + 1:]
                elif rel == base:
                    continue
                target = os.path.join(local, rel.replace("/", os.sep))
                if info.get("type") == "dir":
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    await client.download(fpath, target, write_into=True)

        try:
            client = aioftp.Client(socket_timeout=30)
            await asyncio.wait_for(client.connect(host, port), timeout=8)
            await asyncio.wait_for(client.login(user, password), timeout=8)
            try:
                if is_dir:
                    total = await _ftp_measure(client, src_abs)
                    if max_bytes and total > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"copy size {total} exceeds max_copy_size_ftp {max_bytes}",
                        )
                    if os.path.exists(dst):
                        if os.path.isdir(dst) and not os.path.islink(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    await _ftp_download_tree(client, src_abs, dst)
                else:
                    stat = await client.stat(src_abs)
                    try:
                        total = int(stat.get("size", 0))
                    except (TypeError, ValueError):
                        total = 0
                    if max_bytes and total > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"copy size {total} exceeds max_copy_size_ftp {max_bytes}",
                        )
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    await client.download(src_abs, dst, write_into=True)
            finally:
                try:
                    await client.quit()
                except Exception:  # noqa: BLE001
                    pass
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("FTP copy failed for %s", src_path)
            raise HTTPException(status_code=500, detail=str(e))
        state.cache.invalidate()
        return {"ok": True, "bytes": total}

    @app.post("/api/fs/open")
    async def api_fs_open(payload: dict) -> dict:
        """Open a directory in Windows Explorer (server-side, local host).

        For local/wrq trees this opens the resolved folder. For ftp it
        launches Explorer with an ftp:// URL so the user can browse the
        remote tree with the native shell.
        """
        import subprocess
        tree = str(payload.get("tree", ""))
        path = str(payload.get("path", ""))
        try:
            if tree == "ftp":
                ftp_cfg = state.config.get("files", "ftp", default={}) or {}
                host = ftp_cfg.get("host", "")
                port = int(ftp_cfg.get("port", 21))
                user = ftp_cfg.get("user", "")
                password = ftp_cfg.get("password", "")
                ftp_root = ftp_cfg.get("root", "/")
                if not host:
                    raise HTTPException(status_code=400, detail="FTP not configured")
                full = (ftp_root.rstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/"
                # Build ftp:// URL. Omit credentials when anonymous.
                if user:
                    url = f"ftp://{user}:{password}@{host}:{port}{full}"
                else:
                    url = f"ftp://{host}:{port}{full}"
                subprocess.Popen(["explorer.exe", url])
            else:
                _base, target = _safe_target(tree, path)
                if not os.path.isdir(target):
                    target = os.path.dirname(target) or target
                subprocess.Popen(["explorer.exe", target])
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("open-in-explorer failed")
            raise HTTPException(status_code=500, detail=str(e))
        return {"ok": True}

    @app.get("/api/logs")
    async def api_logs(limit: int = 500, level: Optional[str] = None) -> list[dict]:
        handler = get_memory_handler()
        items = handler.snapshot()
        if level and level.upper() != "ALL":
            items = [i for i in items if i["level"] == level.upper()]
        return items[-limit:]

    @app.get("/api/nics")
    async def api_nics() -> list[dict]:
        """Enumerate adapters Npcap can actually open.

        Source of truth is ``scapy.arch.windows.get_windows_if_list()``
        -- every entry there is a real ``\\Device\\NPF_{GUID}`` that
        ``pcap_open_live`` / ``conf.L2socket`` will accept. We then use
        ``psutil`` to annotate each entry (by MAC) with the connection
        name Windows Settings shows (Ethernet / Wi-Fi / ...), the link
        state, and the link speed. This direction avoids the MAC-
        collision hazard of iterating psutil first: two adapters with
        the same MAC (virtual switches, NIC teams, TAPs) would have
        produced ambiguous NPF mappings.
        """
        try:
            from scapy.arch.windows import get_windows_if_list
        except Exception as e:  # noqa: BLE001
            return [{"error": f"scapy unavailable: {e}"}]

        # MAC -> (friendly_name, isup, speed_mbps, mtu)
        psu_by_mac: dict[str, tuple[str, bool, int, int]] = {}
        try:
            stats = psutil.net_if_stats()
            for fname, alist in psutil.net_if_addrs().items():
                mac = ""
                for a in alist:
                    if getattr(a, "family", None) == psutil.AF_LINK:
                        raw = (a.address or "").lower().replace("-", ":")
                        if raw:
                            mac = raw
                            break
                if not mac:
                    continue
                st = stats.get(fname)
                psu_by_mac[mac] = (
                    fname,
                    bool(getattr(st, "isup", False)),
                    int(getattr(st, "speed", 0) or 0),
                    int(getattr(st, "mtu", 0) or 0),
                )
        except Exception:  # noqa: BLE001
            log.exception("psutil enrichment failed")

        out: list[dict] = []
        try:
            for nic in get_windows_if_list():
                guid = nic.get("guid", "") or ""
                if not guid:
                    # No GUID -> pcap can't open it. Skip.
                    continue
                npf = f"\\Device\\NPF_{guid}"
                mac = (nic.get("mac", "") or "").lower().replace("-", ":")
                desc = nic.get("description", "") or ""
                scapy_name = nic.get("name", "") or ""
                friendly, isup, speed, mtu = psu_by_mac.get(mac, ("", False, 0, 0))
                # Drop obvious loopback / tunnel pseudo-interfaces.
                low_desc = desc.lower()
                if "loopback" in low_desc or "pseudo-" in low_desc:
                    continue
                out.append({
                    "npf": npf,
                    "guid": guid,
                    "name": scapy_name,                 # scapy alias (Npcap internal)
                    "friendly_name": friendly or desc or scapy_name,
                    "description": desc,
                    "mac": mac,
                    "ips": nic.get("ips", []) or [],
                    "isup": isup,
                    "speed_mbps": speed,
                    "mtu": mtu,
                    "has_npcap": True,
                    "win_index": int(nic.get("win_index", 0) or 0),
                })
        except Exception as e:  # noqa: BLE001
            log.exception("nic enumeration failed")
            return [{"error": str(e)}]
        # Up / has-IP adapters first; keep Windows' own ordering beyond that.
        out.sort(key=lambda n: (not n["isup"], not n["ips"], n["win_index"]))
        return out

    @app.get("/api/acl")
    async def api_acl() -> dict:
        return state.config.data.get("security", {})

    # -- auth helpers ---------------------------------------------------
    def _get_token(request: FastAPIRequest) -> str | None:
        token = request.cookies.get("nishro_token")
        if token:
            return token
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _auth_required() -> bool:
        """Central auth switch. Reads ``web.require_auth`` from the
        server-side config -- there is no per-browser override. Default
        is ``False`` so a fresh install is configurable without any
        credential dance; flip it on from Admin Config once you've set
        a password. When false, every caller is treated as an admin."""
        try:
            return bool(state.config.get("web", "require_auth", default=False))
        except Exception:  # noqa: BLE001
            return False

    def _require_admin(request: FastAPIRequest) -> None:
        if not _auth_required():
            return
        if state.auth and not state.auth.check_token(_get_token(request)):
            raise HTTPException(status_code=401, detail="authentication required")

    @app.post("/api/login")
    async def api_login(payload: dict) -> Response:
        if not state.auth:
            return JSONResponse({"ok": False, "error": "auth not configured"}, status_code=500)
        username = payload.get("username", "")
        password = payload.get("password", "")
        if not state.auth.verify(username, password):
            return JSONResponse({"ok": False, "error": "invalid credentials"}, status_code=401)
        # Single-admin enforcement: block login while another session is active.
        if state.auth.has_active_session():
            return JSONResponse(
                {"ok": False, "error": "another admin is already logged in - wait for their session to expire or have them log out"},
                status_code=409,
            )
        token = state.auth.create_token()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key="nishro_token",
            value=token,
            httponly=True,
            samesite="strict",
            max_age=10 * 60,
        )
        return resp

    @app.post("/api/logout")
    async def api_logout(request: FastAPIRequest) -> dict:
        token = _get_token(request)
        if token and state.auth:
            state.auth.revoke_token(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("nishro_token")
        return resp

    # -- UI settings (central, not per-browser) ------------------------
    _ALL_VIEW_TABS = ("dashboard", "sessions", "files", "stats", "logs")
    _DEFAULT_CHART_COLORS = {"completed": "#3fb950", "failed": "#1f6feb"}
    _HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

    def _clean_chart_colors(raw: Any) -> dict:
        out = dict(_DEFAULT_CHART_COLORS)
        if isinstance(raw, dict):
            for k in ("completed", "failed"):
                v = raw.get(k)
                if isinstance(v, str) and _HEX_RE.match(v):
                    out[k] = v
        return out

    def _clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def _ui_settings() -> dict:
        raw = state.config.get("web", "visible_tabs", default=None)
        if isinstance(raw, list):
            tabs = [t for t in raw if t in _ALL_VIEW_TABS]
        else:
            tabs = list(_ALL_VIEW_TABS)
        if "dashboard" not in tabs:
            tabs = ["dashboard"] + tabs
        return {
            "visible_tabs": tabs,
            "auth_required": _auth_required(),
            "has_password": bool(state.auth and state.auth.has_password()),
            "daily_chart_colors": _clean_chart_colors(
                state.config.get("web", "daily_chart_colors", default=None)
            ),
            "stats_recent_window_sec": _clamp_int(
                state.config.get("web", "stats_recent_window_sec", default=60), 60, 10, 600
            ),
            "stats_long_window_hours": _clamp_int(
                state.config.get("web", "stats_long_window_hours", default=12), 12, 1, 48
            ),
        }

    @app.get("/api/ui_settings")
    async def api_ui_settings() -> dict:
        return _ui_settings()

    @app.post("/api/ui_settings")
    async def api_ui_settings_post(request: FastAPIRequest, payload: dict) -> dict:
        _require_admin(request)
        new_cfg = copy.deepcopy(state.config.data)
        web_cfg = dict(new_cfg.get("web", {}) or {})

        if "visible_tabs" in payload:
            tabs = payload.get("visible_tabs")
            if not isinstance(tabs, list):
                raise HTTPException(status_code=400, detail="visible_tabs must be a list")
            clean = [t for t in tabs if t in _ALL_VIEW_TABS]
            if "dashboard" not in clean:
                clean = ["dashboard"] + clean
            web_cfg["visible_tabs"] = clean

        if "daily_chart_colors" in payload:
            raw = payload.get("daily_chart_colors")
            if not isinstance(raw, dict):
                raise HTTPException(
                    status_code=400, detail="daily_chart_colors must be an object"
                )
            web_cfg["daily_chart_colors"] = _clean_chart_colors(raw)

        if "stats_recent_window_sec" in payload:
            web_cfg["stats_recent_window_sec"] = _clamp_int(
                payload.get("stats_recent_window_sec"), 60, 10, 600
            )
        if "stats_long_window_hours" in payload:
            web_cfg["stats_long_window_hours"] = _clamp_int(
                payload.get("stats_long_window_hours"), 12, 1, 48
            )
        # Note: require_auth is owned by the main Config form (POST /api/config).
        # It intentionally does NOT flow through /api/ui_settings so there is
        # exactly one place to toggle it and no race between two save paths.

        new_cfg["web"] = web_cfg
        state.config.save(new_cfg)
        if state.reload_hook:
            state.reload_hook(new_cfg)
        return {"ok": True, **_ui_settings()}

    @app.get("/api/auth/status")
    async def api_auth_status(request: FastAPIRequest) -> dict:
        has_pw = bool(state.auth and state.auth.has_password())
        if not _auth_required():
            return {"authenticated": True, "required": False, "has_password": has_pw}
        if not state.auth:
            return {"authenticated": True, "required": False, "has_password": False}
        return {
            "authenticated": state.auth.check_token(_get_token(request)),
            "required": True,
            "has_password": has_pw,
        }

    @app.post("/api/auth/setup")
    async def api_auth_setup(payload: dict) -> Response:
        """First-run credential setup. Only works while no credentials
        exist on disk; refuses otherwise so an attacker can't reset an
        existing admin through this endpoint."""
        if not state.auth:
            return JSONResponse({"ok": False, "error": "auth not configured"}, status_code=500)
        if state.auth.has_password():
            return JSONResponse({"ok": False, "error": "credentials already set"}, status_code=409)
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        if not username or len(password) < 4:
            return JSONResponse(
                {"ok": False, "error": "username required, password must be >= 4 chars"},
                status_code=400,
            )
        if not state.auth.setup_initial(username, password):
            return JSONResponse({"ok": False, "error": "setup failed"}, status_code=500)
        token = state.auth.create_token()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key="nishro_token",
            value=token,
            httponly=True,
            samesite="strict",
            max_age=10 * 60,
        )
        return resp

    @app.post("/api/auth/change-password")
    async def api_change_password(request: FastAPIRequest, payload: dict) -> dict:
        _require_admin(request)
        if not state.auth:
            return {"ok": False, "error": "auth not configured"}
        ok = state.auth.change_password(
            payload.get("old_password", ""),
            payload.get("new_username", ""),
            payload.get("new_password", ""),
        )
        if not ok:
            return JSONResponse({"ok": False, "error": "current password is wrong"}, status_code=400)
        return {"ok": True}

    @app.post("/api/acl")
    async def api_acl_post(request: FastAPIRequest, payload: dict) -> dict:
        _require_admin(request)
        new_cfg = dict(state.config.data)
        new_cfg["security"] = payload
        state.config.save(new_cfg)
        if state.reload_hook:
            state.reload_hook(new_cfg)
        return {"ok": True}

    # -- users (employee ID/Name mapping) --------------------------------
    @app.get("/api/users")
    async def api_users() -> dict:
        if state.users:
            return state.users.all()
        return {}

    @app.post("/api/users")
    async def api_users_post(request: FastAPIRequest, payload: dict) -> dict:
        _require_admin(request)
        if not state.users:
            return JSONResponse({"ok": False, "error": "users store not configured"}, status_code=500)
        state.users.replace_all(payload)
        return {"ok": True}

    # -- websocket -------------------------------------------------------
    @app.websocket("/ws")
    async def ws_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        # Tie the admin session lifetime to this socket: if the cookie
        # carries a valid admin token, bump its refcount; when this WS
        # disconnects (tab closed, network drop) the refcount drops and
        # the token is revoked, freeing the single-admin slot for the
        # next login without waiting for TTL to expire.
        ws_token = websocket.cookies.get("nishro_token") if state.auth else None
        ws_attached = state.auth.ws_attach(ws_token) if (state.auth and ws_token) else False
        interval = float(state.config.get("web", "stats_interval", default=WEB_STATS_INTERVAL_DEFAULT))

        loop = asyncio.get_event_loop()
        log_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=WEB_WS_LOG_QUEUE)

        def on_log(entry: dict) -> None:
            try:
                loop.call_soon_threadsafe(log_queue.put_nowait, entry)
            except Exception:  # noqa: BLE001
                pass

        handler = get_memory_handler()
        handler.add_listener(on_log)

        # Track what we last sent so we can skip unchanged data.
        prev_history_len = -1
        users_snapshot = state.users.all() if state.users else {}
        prev_sessions_empty = False

        def _sessions_safe() -> list[dict]:
            if state.tftp_server is None:
                return []
            try:
                return state.tftp_server.active_sessions()
            except Exception:  # noqa: BLE001
                return []

        def _history_safe() -> list[dict]:
            if state.tftp_server is None:
                return []
            try:
                return list(state.tftp_server.session_history)
            except Exception:  # noqa: BLE001
                return []

        try:
            # Initial burst - send everything including users and full history
            history = _history_safe()
            prev_history_len = len(history)
            prev_ui = _ui_settings()
            await websocket.send_text(json.dumps({
                "type": "init",
                "logs": handler.snapshot()[-WEB_WS_INIT_LOG_COUNT:],
                "stats": _stats_payload(state),
                "sessions": _sessions_safe(),
                "session_history": history,
                "users": users_snapshot,
                "ui_settings": prev_ui,
            }))

            while True:
                await asyncio.sleep(interval)

                # Drain logs
                logs_batch: list[dict] = []
                while not log_queue.empty() and len(logs_batch) < WEB_WS_LOGS_PER_TICK:
                    logs_batch.append(log_queue.get_nowait())

                # Build tick payload - only include heavy data when changed
                sessions_now = _sessions_safe()
                tick: dict[str, Any] = {
                    "type": "tick",
                    "stats": _stats_payload(state),
                    "logs": logs_batch,
                }
                # Sessions: always send when non-empty (bytes progress
                # every tick); when empty, send once then skip identical
                # empty payloads until activity resumes.
                if sessions_now:
                    tick["sessions"] = sessions_now
                    prev_sessions_empty = False
                elif not prev_sessions_empty:
                    tick["sessions"] = sessions_now
                    prev_sessions_empty = True

                # Session history: only send when it changed
                history = _history_safe()
                cur_len = len(history)
                if cur_len != prev_history_len:
                    tick["session_history"] = history
                    prev_history_len = cur_len

                # Users map: only send when it changed
                if state.users:
                    cur_users = state.users.all()
                    if cur_users != users_snapshot:
                        tick["users"] = cur_users
                        users_snapshot = cur_users

                # UI settings: push when changed so non-admin viewers
                # react to admin-config saves without reloading.
                cur_ui = _ui_settings()
                if cur_ui != prev_ui:
                    tick["ui_settings"] = cur_ui
                    prev_ui = cur_ui

                await websocket.send_text(json.dumps(tick))
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("websocket error")
        finally:
            handler.remove_listener(on_log)
            if ws_attached and state.auth:
                state.auth.ws_detach(ws_token)

    return app


def _measure_size(path: str) -> int:
    """Total bytes of a file, or recursive sum of a directory tree."""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _browse_dir(root: str, sub_path: str) -> dict:
    """List files and folders in a local directory, safely joined."""
    import stat as _stat
    base = os.path.abspath(root)
    target = os.path.abspath(os.path.join(base, sub_path))
    if not target.startswith(base):
        return {"error": "invalid path", "entries": []}
    if not os.path.isdir(target):
        return {"error": "not a directory", "entries": []}
    entries: list[dict] = []
    try:
        for name in os.listdir(target):
            full = os.path.join(target, name)
            try:
                st = os.stat(full)
                is_dir = _stat.S_ISDIR(st.st_mode)
                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": int(st.st_size) if not is_dir else None,
                    "mtime": float(st.st_mtime),
                })
            except OSError:
                entries.append({"name": name, "is_dir": False, "size": None, "mtime": None})
    except OSError as e:
        return {"error": str(e), "entries": []}
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": os.path.relpath(target, base).replace(os.sep, "/"), "entries": entries}


def _stats_payload(state: AppState) -> dict:
    s = STATS.snapshot()
    engine = (state.config.get("tftp", "engine", default="python") or "python").lower()

    # C-engine mode: the packet path is in a subprocess so the Python
    # STATS counters stay at zero. Overlay the C engine's latest `stat`
    # event and sample the child PID for CPU + memory accounting.
    child_pid: int | None = None
    if engine == "c" and state.tftp_server is not None:
        core_proc = getattr(state.tftp_server, "proc", None)
        if core_proc is not None:
            child_pid = getattr(core_proc, "pid", None)
        if hasattr(state.tftp_server, "get_stats"):
            try:
                c = state.tftp_server.get_stats() or {}
            except Exception:  # noqa: BLE001
                c = {}
            c_to_py = {
                "bytes_sent":          "bytes_sent",
                "bytes_received":      "bytes_received",
                "rrq":                 "tftp_rrq",
                "wrq":                 "tftp_wrq",
                "errors":              "tftp_errors",
                "sessions_total":      "tftp_sessions_total",
                "sessions_completed":  "tftp_sessions_completed",
                "sessions_failed":     "tftp_sessions_failed",
                "acl_denied":          "acl_denied",
                "arp_requests":        "arp_requests",
                "arp_replies":         "arp_replies",
                "icmp_requests":       "icmp_requests",
                "icmp_replies":        "icmp_replies",
            }
            for src_key, dst_key in c_to_py.items():
                if src_key in c:
                    s[dst_key] = c[src_key]

    # Fold in totals from any previously-stopped engines so an engine
    # swap doesn't reset what the dashboard shows.
    s = carryover.apply(s)
    # cache_hit_rate is derived -- recompute against the merged counts.
    total = int(s.get("cache_hits", 0) or 0) + int(s.get("cache_misses", 0) or 0)
    s["cache_hit_rate"] = (int(s.get("cache_hits", 0) or 0) / total) if total else 0.0

    mem, cpu, sys_cpu, cores = _proc_stats(child_pid)
    s["process_rss"] = mem
    s["process_cpu"] = cpu           # % of 1 core (0-100*cores)
    s["system_cpu"] = sys_cpu        # whole-machine (0-100)
    s["cpu_cores"] = cores
    s["cache"] = state.cache.snapshot()
    try:
        s["session_count"] = len(state.tftp_server.sessions) if state.tftp_server else 0
    except Exception:  # noqa: BLE001
        s["session_count"] = 0
    s["engine"] = engine
    s["nic_error"] = state.nic_error
    s["server_status"] = "degraded" if state.nic_error else "ready"
    return s
