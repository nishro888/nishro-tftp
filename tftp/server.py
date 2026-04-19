"""TFTP dispatcher - accepts packets from the L2 bridge, routes them
to the right session, allocates per-session server TIDs, enforces the
concurrent-session cap."""
from __future__ import annotations

import asyncio
import collections
import logging
import random
import time
from typing import Optional

from core import daily_stats
from core.acl import ACL
from core.constants import (
    SESSIONS_MAX_CONCURRENT_DEFAULT,
    SESSIONS_OVERFLOW_POLL_INTERVAL,
    SESSIONS_QUEUE_TIMEOUT_DEFAULT,
    TFTP_DEFAULT_LISTEN_PORT,
    TFTP_TID_MAX,
    TFTP_TID_MIN,
    WRITE_ROOT_DEFAULT,
)
from core.file_lock import FileLockManager
from core.stats import STATS
from files.cache import FileCache
from files.source import FileSource
from network.packet_utils import ParsedPacket, build_udp_frame
from network.sniffer import L2Bridge

from . import protocol as tp
from .session import SessionKey, TftpSession

log = logging.getLogger("nishro.tftp")


class TftpServer:
    def __init__(
        self,
        bridge: L2Bridge,
        virtual_ip: str,
        virtual_mac: str,
        cfg: dict,
        source: FileSource,
        cache: FileCache,
        locks: FileLockManager,
        acl: ACL,
    ):
        self.bridge = bridge
        self.virtual_ip = virtual_ip
        self.virtual_mac = virtual_mac
        self.cfg = cfg
        self.source = source
        self.cache = cache
        self.locks = locks
        self.acl = acl

        self.sessions: dict[SessionKey, TftpSession] = {}
        self._tid_pool: set[int] = set()
        self._tid_lock = asyncio.Lock()
        # Ring buffer of last 100 completed/failed session snapshots.
        self.session_history: collections.deque[dict] = collections.deque(maxlen=100)

    # -- Config updates ------------------------------------------------
    def update(self, virtual_ip: str, virtual_mac: str, cfg: dict) -> None:
        self.virtual_ip = virtual_ip
        self.virtual_mac = virtual_mac
        self.cfg = cfg

    # -- Introspection -------------------------------------------------
    def active_sessions(self) -> list[dict]:
        out = []
        for sess in list(self.sessions.values()):
            info = sess.info
            out.append({
                "id": info.id,
                "kind": info.kind,
                "filename": info.filename,
                "client_mac": info.client_mac,
                "client_ip": info.client_ip,
                "client_port": info.client_port,
                "vlan_id": info.vlan_id,
                "blksize": info.blksize,
                "windowsize": info.windowsize,
                "bytes_transferred": info.bytes_transferred,
                "total_bytes": info.total_bytes,
                "progress": info.progress(),
                "speed": info.speed(),
                "max_speed": info.max_speed,
                "state": info.state,
                "started_at": info.started_at,
            })
        return out

    # -- Packet entry point -------------------------------------------
    def handle(self, p: ParsedPacket) -> bool:
        """Return ``True`` if the packet was for the TFTP service."""
        if not (p.is_udp and p.ip_dst == self.virtual_ip):
            return False

        listen_port = int(self.cfg.get("listen_port", TFTP_DEFAULT_LISTEN_PORT))

        # Known session? (running transfer on an allocated TID)
        probe = SessionKey(
            client_mac=p.eth_src,
            client_ip=p.ip_src,
            client_port=p.udp_sport,
            server_port=p.udp_dport,
            vlan_id=p.vlan_id,
        )
        sess = self.sessions.get(probe)
        if sess is not None:
            sess.feed(bytes(p.udp_payload))
            return True

        # New request?
        if p.udp_dport != listen_port:
            return True  # Not ours, but on our IP - swallow so it doesn't hit anything else

        denial = self.acl.check("tftp", p.vlan_id, p.ip_src)
        if denial:
            STATS.bump("acl_denied")
            STATS.bump("vlan_denied" if denial == "vlan" else "ip_denied")
            log.debug("TFTP %s ACL denied src=%s vlan=%s", denial, p.ip_src, p.vlan_id)
            return True

        try:
            req = tp.parse_request(bytes(p.udp_payload))
        except tp.TFTPError as e:
            log.info("malformed request from %s: %s", p.ip_src, e.message)
            self._send_error_on_request(p, e.code, e.message)
            return True

        asyncio.create_task(self._accept(p, req))
        return True

    # -- Session bootstrap --------------------------------------------
    async def _accept(self, p: ParsedPacket, req: tp.Request) -> None:
        # self.cfg is a merged view: tftp fields at the top level, with
        # "sessions" and "files" sub-dicts hanging off it (see main.py).
        sess_cfg = self.cfg.get("sessions", {}) or {}
        files_cfg = self.cfg.get("files", {}) or {}

        max_conc = int(sess_cfg.get("max_concurrent", SESSIONS_MAX_CONCURRENT_DEFAULT))
        policy = str(sess_cfg.get("overflow_policy", "reject")).lower()
        queue_timeout = float(sess_cfg.get("queue_timeout", SESSIONS_QUEUE_TIMEOUT_DEFAULT))

        if len(self.sessions) >= max_conc:
            if policy == "reject":
                log.warning("session cap reached - rejecting %s", p.ip_src)
                self._send_error_on_request(p, tp.ERR_UNDEFINED, "server busy")
                return
            # Queue mode: hold the request until a slot opens or we
            # hit the wait-limit, then reject.
            start = asyncio.get_event_loop().time()
            while len(self.sessions) >= max_conc:
                if asyncio.get_event_loop().time() - start > queue_timeout:
                    log.warning("queue timeout - rejecting %s", p.ip_src)
                    self._send_error_on_request(p, tp.ERR_UNDEFINED, "server busy")
                    return
                await asyncio.sleep(SESSIONS_OVERFLOW_POLL_INTERVAL)

        tid = await self._alloc_tid()
        key = SessionKey(
            client_mac=p.eth_src,
            client_ip=p.ip_src,
            client_port=p.udp_sport,
            server_port=tid,
            vlan_id=p.vlan_id,
        )

        write_root = str(files_cfg.get("write_root", WRITE_ROOT_DEFAULT))
        enable_writes = bool(self.cfg.get("enable_writes", False))
        # Back-compat: if the old single max_file_size is set and the
        # new split keys are absent, fall back to it for all three caps.
        legacy = int(files_cfg.get("max_file_size", 0) or 0)
        max_rrq_size = int(files_cfg.get("max_rrq_size", legacy) or 0)
        max_wrq_size = int(files_cfg.get("max_wrq_size", legacy) or 0)
        max_ftp_size = int(files_cfg.get("max_ftp_size", legacy) or 0)
        allow_wrq_ftp = bool(files_cfg.get("allow_wrq_ftp", False))
        from core.constants import FTP_PREFIX_TRIGGER
        prefix_cfg = files_cfg.get("ftp_prefix", {}) or {}
        ftp_trigger = str(prefix_cfg.get("trigger", FTP_PREFIX_TRIGGER)) if prefix_cfg.get("enabled") else ""
        allow_rollover = bool(self.cfg.get("allow_block_rollover", True))

        sess = TftpSession(
            key=key,
            first_request=p,
            parsed=req,
            bridge=self.bridge,
            our_mac=self.virtual_mac,
            our_ip=self.virtual_ip,
            cfg_tftp=self.cfg,
            source=self.source,
            cache=self.cache,
            locks=self.locks,
            write_root=write_root,
            enable_writes=enable_writes,
            max_rrq_size=max_rrq_size,
            max_wrq_size=max_wrq_size,
            max_ftp_size=max_ftp_size,
            allow_wrq_ftp=allow_wrq_ftp,
            ftp_trigger=ftp_trigger,
            allow_rollover=allow_rollover,
            on_complete=self._on_complete,
        )
        self.sessions[key] = sess
        STATS.bump("tftp_sessions_total")
        sess.start()

    async def _alloc_tid(self) -> int:
        listen = int(self.cfg.get("listen_port", TFTP_DEFAULT_LISTEN_PORT))
        async with self._tid_lock:
            for _ in range(1000):
                tid = random.randint(TFTP_TID_MIN, TFTP_TID_MAX)
                if tid not in self._tid_pool and tid != listen:
                    self._tid_pool.add(tid)
                    return tid
            raise RuntimeError("exhausted TID pool")

    def _on_complete(self, sess: TftpSession) -> None:
        # Capture a final snapshot before removing.
        info = sess.info
        success = (info.state == "done")
        self.session_history.appendleft({
            "id": info.id,
            "kind": info.kind,
            "filename": info.filename,
            "client_mac": info.client_mac,
            "client_ip": info.client_ip,
            "client_port": info.client_port,
            "vlan_id": info.vlan_id,
            "blksize": info.blksize,
            "windowsize": info.windowsize,
            "bytes_transferred": info.bytes_transferred,
            "total_bytes": info.total_bytes,
            "progress": info.progress(),
            # Show the peak observed speed on the completed row, not the
            # instantaneous final-block sliding value (which collapses to 0).
            "speed": int(info.max_speed or info.speed()),
            "max_speed": int(info.max_speed),
            "state": info.state,
            "server_fault": bool(info.server_fault),
            "started_at": info.started_at,
            "ended_at": time.time(),
        })
        self.sessions.pop(sess.key, None)
        self._tid_pool.discard(sess.key.server_port)

        # Daily chart accounting: "completed" tracks real success, "failed"
        # tracks only genuine server faults. Client dropouts and policy
        # rejections still bump "total" but land in neither bar.
        bytes_sent = info.bytes_transferred if info.kind == "RRQ" else 0
        bytes_received = info.bytes_transferred if info.kind == "WRQ" else 0
        daily_stats.record(
            success=success,
            server_fault=bool(info.server_fault),
            bytes_sent=bytes_sent,
            bytes_received=bytes_received,
            filename=info.filename,
            client_mac=info.client_mac,
        )

    # -- Error shortcut for non-session replies -----------------------
    def _send_error_on_request(self, p: ParsedPacket, code: int, msg: str) -> None:
        payload = tp.pack_error(code, msg)
        frame = build_udp_frame(
            dst_mac=p.eth_src,
            src_mac=self.virtual_mac,
            vlan_id=p.vlan_id,
            vlan_pcp=p.vlan_pcp,
            vlan_dei=p.vlan_dei,
            src_ip=self.virtual_ip,
            dst_ip=p.ip_src,
            sport=int(self.cfg.get("listen_port", TFTP_DEFAULT_LISTEN_PORT)),
            dport=p.udp_sport,
            payload=payload,
        )
        self.bridge.send(frame)
        STATS.bump("tftp_errors")

    def shutdown(self) -> None:
        for sess in list(self.sessions.values()):
            sess.cancel()
        self.sessions.clear()
