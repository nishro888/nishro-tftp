"""C engine manager.

Spawns ``nishro_core.exe`` as a child process and talks to it over
stdin/stdout using one-JSON-per-line messages. The Python side only
runs when ``tftp.engine == "c"`` is set in config.yaml -- in that mode
Python hands ALL packet I/O (ARP / ICMP / TFTP / FTP) to the C engine
and only keeps the web UI + config file management.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

from core import daily_stats

log = logging.getLogger("nishro.engine")


def _core_exe_path() -> str:
    """Locate nishro_core.exe. Under PyInstaller it's bundled next to
    other data (``sys._MEIPASS``); in dev it sits in ``c_core/bin/``."""
    name = "nishro_core.exe"
    if getattr(sys, "frozen", False):
        for d in (getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable)):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "c_core", "bin", name)


def _yaml_to_core_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Translate the Python-side YAML schema into what nishro_core's
    config.c expects. The C layout is flatter and uses different names
    (e.g. ``files.rrq_root`` vs ``files.local.root``)."""
    net = cfg.get("network", {}) or {}
    tftp = cfg.get("tftp", {}) or {}
    files = cfg.get("files", {}) or {}
    local = files.get("local", {}) or {}
    ftpp = files.get("ftp", {}) or {}
    prefix = files.get("ftp_prefix", {}) or {}
    sess = cfg.get("sessions", {}) or {}
    sec = cfg.get("security", {}) or {}
    logc = cfg.get("logging", {}) or {}

    defaults = tftp.get("defaults", {}) or {}
    limits = tftp.get("limits", {}) or {}

    def _limpair(key_base: str, key_min: str, key_max: str, dflt_key: str):
        return {
            "min": int(limits.get(key_min, 0) or 0),
            "max": int(limits.get(key_max, 0) or 0),
            "default": int(defaults.get(dflt_key, 0) or 0),
        }

    # ACL: merge vlan_acl + ip_acl lists into a single flat rule list.
    # In the Python schema each list entry may carry its own vlan/ip/
    # services; we pass them through directly.
    rules: list[dict] = []
    for acl in (sec.get("vlan_acl", {}) or {}, sec.get("ip_acl", {}) or {}):
        if (acl.get("mode") or "disabled") == "disabled":
            continue
        for r in acl.get("list", []) or []:
            rules.append(r)
    default_allow = True  # first-match; if empty, allow

    folder_fmt = "{}%0{}u".format(
        prefix.get("folder_prefix", "BDCOM"),
        int(prefix.get("digit_pad", 4) or 4),
    )

    return {
        "network": {
            "nic": net.get("nic", "") or "",
            "virtual_ip": net.get("virtual_ip", "") or "",
            "virtual_mac": net.get("virtual_mac") or "",
            "promiscuous": bool(net.get("promiscuous", True)),
        },
        "tftp": {
            "port": int(tftp.get("listen_port", 69) or 69),
            "wrq_enabled": bool(tftp.get("enable_writes", False)),
            "options": {
                "blksize":    _limpair("blksize",    "blksize_min",    "blksize_max",    "blksize"),
                "windowsize": _limpair("windowsize", "windowsize_min", "windowsize_max", "windowsize"),
                "timeout":    _limpair("timeout",    "timeout_min",    "timeout_max",    "timeout"),
            },
        },
        "files": {
            "rrq_root":      local.get("root", "./tftp_root"),
            "wrq_root":      files.get("write_root", "./tftp_uploads"),
            "rrq_recursive": bool(local.get("recursive", True)),
            "max_rrq_size":  int(files.get("max_rrq_size", 0) or 0),
            "max_wrq_size":  int(files.get("max_wrq_size", 0) or 0),
            "max_ftp_size":  int(files.get("max_ftp_size", 0) or 0),
            "allow_wrq_ftp": bool(files.get("allow_wrq_ftp", False)),
            "prefix": {
                "enabled":    bool(prefix.get("enabled", False)),
                "trigger":    prefix.get("trigger", "f::"),
                "folder_fmt": folder_fmt,
            },
            "ftp": {
                "enabled": bool(ftpp.get("host")),
                "host":    ftpp.get("host", ""),
                "port":    int(ftpp.get("port", 21) or 21),
                "user":    ftpp.get("user", ""),
                "pass":    ftpp.get("password", ""),
                "root":    ftpp.get("root", "/"),
            },
        },
        "sessions": {
            "max": int(sess.get("max_concurrent", 64) or 64),
        },
        "security": {
            "default_allow": default_allow,
            "rules": rules,
        },
        "logging": {
            "level": (logc.get("level", "INFO") or "INFO").lower(),
        },
    }


class CoreEngine:
    """Supervises the nishro_core.exe child process."""

    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stats: Dict[str, Any] = {}
        self._stats_lock = threading.Lock()
        self._sessions: Dict[int, Dict[str, Any]] = {}
        # Expose the same shape the web UI expects from TftpServer.
        self.sessions: Dict[int, Dict[str, Any]] = self._sessions
        self.session_history: "collections.deque[dict]" = collections.deque(maxlen=100)
        self._ready = asyncio.Event()

    def active_sessions(self) -> list[dict]:
        with self._stats_lock:
            out = []
            for s in self._sessions.values():
                self._compute_derived(s)
                # strip private tracking keys before handing to the web layer
                out.append({k: v for k, v in s.items() if not k.startswith("_")})
            return out

    async def start(self, cfg: Dict[str, Any]) -> None:
        exe = _core_exe_path()
        if not os.path.exists(exe):
            raise RuntimeError(f"nishro_core.exe not found at {exe}")
        log.info("spawning C engine: %s", exe)
        self.proc = await asyncio.create_subprocess_exec(
            exe,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(self._reader(), name="core-reader")
        await self.push_config(cfg)

    async def push_config(self, cfg: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            return
        msg = {"op": "config", "data": _yaml_to_core_cfg(cfg)}
        line = (json.dumps(msg) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.warning("C engine pipe closed on config push")

    async def stop(self) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            try:
                self.proc.stdin.write(b'{"op":"stop"}\n')
                await self.proc.stdin.drain()
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        if self.proc:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                log.warning("C engine didn't exit; killing")
                self.proc.kill()
                await self.proc.wait()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.proc = None

    def get_stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            return dict(self._stats)

    def get_sessions(self) -> list[dict]:
        with self._stats_lock:
            return list(self._sessions.values())

    async def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                log.info("C engine stdout closed")
                return
            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                # Core uses stderr→stdout merging, so non-JSON is a log line.
                log.info("core: %s", line.decode("utf-8", errors="replace").rstrip())
                continue
            self._on_event(obj)

    def _compute_derived(self, s: Dict[str, Any]) -> None:
        """Fill in ``progress`` (0..1), ``speed`` (bytes/sec, smoothed
        over a 2-second window), ``max_speed`` (peak observed on this
        session) and ``duration_ms``."""
        total = int(s.get("total_bytes") or 0)
        done = int(s.get("bytes_transferred") or 0)
        s["progress"] = (done / total) if total > 0 else 0

        now = time.time()
        started = float(s.get("started_at") or now)
        s["duration_ms"] = int(max(now - started, 0) * 1000)

        # Smoothed speed: average over the last ~2s of samples. Trims
        # high-frequency variance from the C engine's 200 ms progress
        # events while still reacting to actual rate changes.
        samples = s.get("_spd_samples")
        if samples is None:
            samples = [(started, 0)]
            s["_spd_samples"] = samples
        samples.append((now, done))
        cutoff = now - 2.0
        while len(samples) > 2 and samples[1][0] < cutoff:
            samples.pop(0)
        t0, b0 = samples[0]
        dt = now - t0
        s["speed"] = int(max(done - b0, 0) / dt) if dt > 0 else 0

        peak = int(s.get("max_speed") or 0)
        if s["speed"] > peak:
            s["max_speed"] = s["speed"]
        else:
            s["max_speed"] = peak

    def _on_event(self, ev: Dict[str, Any]) -> None:
        kind = ev.get("ev")
        if kind == "stat":
            with self._stats_lock:
                self._stats = ev
        elif kind == "session_start":
            with self._stats_lock:
                self._sessions[int(ev["id"])] = ev
                self._compute_derived(self._sessions[int(ev["id"])])
        elif kind == "session_progress":
            with self._stats_lock:
                s = self._sessions.get(int(ev["id"]))
                if s:
                    s.update(ev)
                    self._compute_derived(s)
        elif kind == "session_end":
            with self._stats_lock:
                s = self._sessions.pop(int(ev["id"]), None)
                if s:
                    s.update(ev)
                    # Finalize speed: show peak, not the last sliding value
                    # (which often collapses to 0 on the final short block).
                    s["speed"] = int(s.get("max_speed") or s.get("speed") or 0)
                    self.session_history.appendleft(
                        {k: v for k, v in s.items() if not k.startswith("_")}
                    )
                    success = bool(ev.get("ok"))
                    server_fault = bool(ev.get("server_fault"))
                    bt = int(s.get("bytes_transferred") or 0)
                    kind_up = str(s.get("kind", "")).upper()
                    daily_stats.record(
                        success=success,
                        server_fault=server_fault,
                        bytes_sent=(bt if kind_up == "RRQ" else 0),
                        bytes_received=(bt if kind_up == "WRQ" else 0),
                        filename=s.get("filename"),
                        client_mac=s.get("client_mac"),
                    )
        elif kind == "ready":
            self._ready.set()
            log.info("C engine ready: device=%s vip=%s",
                     ev.get("device"), ev.get("virtual_ip"))
        elif kind == "hello":
            log.info("C engine hello: version=%s", ev.get("version"))
        elif kind == "bye":
            log.info("C engine bye")
        else:
            log.debug("core event: %s", ev)
