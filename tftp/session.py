"""TFTP session state machines - one coroutine per transfer.

A session is created in ``tftp.server`` when an RRQ or WRQ arrives on
port 69. It owns its own inbound queue of decoded TFTP packets and
runs until the transfer finishes, errors out, or is cancelled.

The transport uses raw L2 frames via :class:`network.sniffer.L2Bridge`,
so the session crafts reply bytes itself and asks the bridge to send
them. Source / destination MAC, IP, VLAN tag and UDP TID are captured
at construction time and reused for every outbound packet in the
flow - which is what preserves the 802.1Q tag on the response path.
"""
from __future__ import annotations

import asyncio
import logging
import socket as _socket
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from core.constants import (
    ETHERTYPE_DOT1Q,
    ETHERTYPE_IPV4,
    IP_DEFAULT_TTL,
    IP_FLAGS_DF,
    TFTP_MAX_RETRIES_DEFAULT,
    TFTP_SESSION_QUEUE_MAX,
)
from core.file_lock import FileLockError, FileLockManager
from core.stats import STATS
from files.cache import FileCache
from files.source import FileSource
from network.packet_utils import ParsedPacket, build_udp_frame
from network.sniffer import L2Bridge

from . import protocol as tp
from .options import Negotiated, negotiate

log = logging.getLogger("nishro.session")


def _is_server_fault(code: int, msg: str) -> bool:
    """Decide whether a TFTPError represents a real server-side failure.

    Server-fault = something is wrong on OUR side and we should flag it
    (disk write failed, FTP upload pipeline failed, etc.). Everything
    else -- client never acked, socket timeout, file not found, access
    denied, unsupported mode, oversized request -- is either the client
    going away or a policy rejection. Those still end the session and
    are shown in history with their reason, but they are NOT counted as
    ``sessions_failed`` and do not move the daily "failed" bar.
    """
    m = (msg or "").lower()
    if code == tp.ERR_UNDEFINED:
        if "timeout" in m or "client never acked" in m:
            return False
        # ERR_UNDEFINED from our own code paths (FTP upload failure,
        # bridge crash, unexpected exception) is server-side.
        return True
    # All categorised errors (NOT_FOUND, ACCESS, DISK_FULL, ILLEGAL_OP,
    # UNKNOWN_TID, FILE_EXISTS, NO_USER, OPTION) are client-facing and
    # not a server failure.
    return False


# -- Fast-path frame builder -------------------------------------------
#
# Each session caches a precomputed Ethernet + optional 802.1Q + IPv4 +
# UDP header template. Per outbound block we only recompute the IP total
# length + header checksum and the UDP length; the rest is a single
# ``bytes`` concatenation. Scapy's per-block Packet build is the biggest
# single CPU hog in the hot path, so skipping it speeds transfers ~10x.

def _mac_to_bytes(mac: str) -> bytes:
    return bytes(int(b, 16) for b in mac.replace("-", ":").split(":"))


def _ip_csum(header: bytes) -> int:
    s = 0
    # Sum 16-bit big-endian words
    for i in range(0, len(header) - 1, 2):
        s += (header[i] << 8) | header[i + 1]
    if len(header) & 1:
        s += header[-1] << 8
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


@dataclass
class SessionKey:
    client_mac: str
    client_ip: str
    client_port: int
    server_port: int
    vlan_id: Optional[int]

    def __hash__(self) -> int:
        return hash((self.client_mac, self.client_ip, self.client_port, self.server_port, self.vlan_id))


@dataclass
class SessionInfo:
    """Snapshot of session state for the web UI."""
    id: str
    kind: str  # "read" or "write"
    filename: str
    client_mac: str
    client_ip: str
    client_port: int
    vlan_id: Optional[int]
    blksize: int
    windowsize: int
    bytes_transferred: int = 0
    total_bytes: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    state: str = "init"
    # True if the session ended because of a genuine server-side problem
    # (unhandled crash, disk write failure, FTP upload pipeline error).
    # False for client-side dropouts / policy rejections (file not found,
    # access denied, client timeout, unsupported mode, etc.). Drives the
    # ``sessions_failed`` counter and the daily-chart "failed" bar.
    server_fault: bool = False
    max_speed: float = 0.0
    # Sliding-window samples: [(t, bytes), ...] trimmed to last 2s.
    _spd_samples: list = field(default_factory=list)

    def speed(self) -> float:
        # 2-second sliding window — smooths out per-ACK burstiness while
        # still reacting to genuine rate changes.
        now = self.last_update
        samples = self._spd_samples
        if not samples:
            samples.append((self.started_at, 0))
        samples.append((now, self.bytes_transferred))
        cutoff = now - 2.0
        while len(samples) > 2 and samples[1][0] < cutoff:
            samples.pop(0)
        t0, b0 = samples[0]
        dt = now - t0
        spd = (self.bytes_transferred - b0) / dt if dt > 0 else 0.0
        if spd > self.max_speed:
            self.max_speed = spd
        return spd

    def progress(self) -> float:
        if self.total_bytes:
            return self.bytes_transferred / self.total_bytes
        return 0.0


class TftpSession:
    """One RRQ or WRQ transfer.

    The ``run()`` coroutine owns the entire lifecycle. ``feed()`` is
    called from the server dispatcher to hand DATA/ACK/ERROR packets
    to the session.
    """

    def __init__(
        self,
        key: SessionKey,
        first_request: ParsedPacket,
        parsed: tp.Request,
        *,
        bridge: L2Bridge,
        our_mac: str,
        our_ip: str,
        cfg_tftp: dict,
        source: FileSource,
        cache: FileCache,
        locks: FileLockManager,
        write_root: str,
        enable_writes: bool,
        max_rrq_size: int,
        max_wrq_size: int,
        max_ftp_size: int,
        allow_wrq_ftp: bool,
        ftp_trigger: str,
        allow_rollover: bool,
        on_complete,
    ) -> None:
        self.key = key
        self.id = uuid.uuid4().hex[:12]
        self.request_pkt = first_request
        self.request = parsed
        self.bridge = bridge
        self.our_mac = our_mac
        self.our_ip = our_ip
        self.cfg = cfg_tftp
        self.source = source
        self.cache = cache
        self.locks = locks
        self.write_root = write_root
        self.enable_writes = enable_writes
        self.max_rrq_size = int(max_rrq_size) if max_rrq_size else 0
        self.max_wrq_size = int(max_wrq_size) if max_wrq_size else 0
        self.max_ftp_size = int(max_ftp_size) if max_ftp_size else 0
        self.allow_wrq_ftp = bool(allow_wrq_ftp)
        self.ftp_trigger = str(ftp_trigger or "")
        self.allow_rollover = bool(allow_rollover)
        self.on_complete = on_complete

        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=TFTP_SESSION_QUEUE_MAX)

        # Precompute the L2/L3/L4 header template for the fast-path
        # sender. Everything up to the UDP length / IP checksum / payload
        # is identical for every block; we only patch those three fields
        # per outbound frame.
        self._build_fastpath_template()
        self.info = SessionInfo(
            id=self.id,
            kind="read" if parsed.opcode == tp.OP_RRQ else "write",
            filename=parsed.filename,
            client_mac=key.client_mac,
            client_ip=key.client_ip,
            client_port=key.client_port,
            vlan_id=key.vlan_id,
            blksize=512,
            windowsize=1,
        )
        self.task: Optional[asyncio.Task] = None

    # -- External API --------------------------------------------------
    def start(self) -> None:
        self.task = asyncio.create_task(self._safe_run(), name=f"tftp-{self.id}")

    def feed(self, payload: bytes) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("session %s queue full - dropping packet", self.id)

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()

    # -- Send helpers --------------------------------------------------
    def _build_fastpath_template(self) -> None:
        """Pre-build the static prefix and header pieces we reuse for
        every outbound frame. Only UDP length / IP total length / IP
        checksum and the payload itself change between calls to
        :meth:`_send`."""
        dst_mac = _mac_to_bytes(self.key.client_mac)
        src_mac = _mac_to_bytes(self.our_mac)

        parts: list[bytes] = [dst_mac, src_mac]
        if self.key.vlan_id is not None:
            tci = (
                ((self.request_pkt.vlan_pcp & 0x7) << 13)
                | ((self.request_pkt.vlan_dei & 0x1) << 12)
                | (int(self.key.vlan_id) & 0xFFF)
            )
            # 802.1Q TPID + TCI, then inner ethertype = IPv4
            parts.append(struct.pack(">HHH", ETHERTYPE_DOT1Q, tci, ETHERTYPE_IPV4))
        else:
            parts.append(struct.pack(">H", ETHERTYPE_IPV4))

        self._eth_prefix: bytes = b"".join(parts)
        self._src_ip_b: bytes = _socket.inet_aton(self.our_ip)
        self._dst_ip_b: bytes = _socket.inet_aton(self.key.client_ip)
        self._sport: int = int(self.key.server_port)
        self._dport: int = int(self.key.client_port)

    def _build_frame_fast(self, payload: bytes) -> bytes:
        udp_len = 8 + len(payload)
        ip_total = 20 + udp_len
        # IPv4 header with checksum field zeroed for the checksum calc
        ip_hdr = struct.pack(
            ">BBHHHBBH4s4s",
            0x45,              # version=4, ihl=5
            0x00,              # DSCP / ECN
            ip_total,          # total length
            0x0000,            # id
            IP_FLAGS_DF,       # flags=DF, fragment offset=0
            IP_DEFAULT_TTL,    # ttl
            17,                # proto = UDP
            0x0000,            # checksum placeholder
            self._src_ip_b,
            self._dst_ip_b,
        )
        csum = _ip_csum(ip_hdr)
        ip_hdr = ip_hdr[:10] + struct.pack(">H", csum) + ip_hdr[12:]

        # UDP header with checksum = 0 (allowed for IPv4)
        udp_hdr = struct.pack(">HHHH", self._sport, self._dport, udp_len, 0)
        return self._eth_prefix + ip_hdr + udp_hdr + payload

    def _send(self, payload: bytes, *, count: bool = True) -> None:
        try:
            frame = self._build_frame_fast(payload)
            self.bridge.send_bytes(frame)
        except Exception:  # noqa: BLE001 - fall back to the scapy path
            log.exception("fast-path send failed, falling back to scapy")
            frame_pkt = build_udp_frame(
                dst_mac=self.key.client_mac,
                src_mac=self.our_mac,
                vlan_id=self.key.vlan_id,
                vlan_pcp=self.request_pkt.vlan_pcp,
                vlan_dei=self.request_pkt.vlan_dei,
                src_ip=self.our_ip,
                dst_ip=self.key.client_ip,
                sport=self.key.server_port,
                dport=self.key.client_port,
                payload=payload,
            )
            self.bridge.send(frame_pkt)
        if count:
            STATS.bump("bytes_sent", len(payload))

    def _send_error(self, code: int, msg: str) -> None:
        # ERROR packets are protocol signalling, not user data -- do not
        # count them against the throughput / bytes_sent totals.
        self._send(tp.pack_error(code, msg), count=False)
        STATS.bump("tftp_errors")

    # -- helpers -------------------------------------------------------
    def _is_ftp_name(self, filename: str) -> bool:
        """True if ``filename`` resolves via FTP (``ftp://`` or trigger)."""
        low = filename.lower()
        if low.startswith("ftp://"):
            return True
        if self.ftp_trigger and filename.startswith(self.ftp_trigger):
            return True
        return False

    def _rrq_limit(self, filename: str) -> int:
        """Size cap for a read. FTP-routed names get the FTP cap."""
        return self.max_ftp_size if self._is_ftp_name(filename) else self.max_rrq_size

    # -- Main coroutine ------------------------------------------------
    async def _safe_run(self) -> None:
        try:
            if self.request.opcode == tp.OP_RRQ:
                await self._run_rrq()
            else:
                await self._run_wrq()
        except asyncio.CancelledError:
            log.info("session %s cancelled", self.id)
            raise
        except tp.TFTPError as e:
            self._send_error(e.code, e.message)
            self.info.state = e.message
            if _is_server_fault(e.code, e.message):
                self.info.server_fault = True
                STATS.bump("tftp_sessions_failed")
                log.warning("session %s server fault: %s", self.id, e.message)
            else:
                # Client-side dropout / policy rejection -- not a server
                # failure. Do not increment the failed counter.
                log.info("session %s ended: %s", self.id, e.message)
        except Exception as e:  # noqa: BLE001
            log.exception("session %s crashed", self.id)
            self._send_error(tp.ERR_UNDEFINED, f"server error: {e}")
            self.info.state = f"server error: {e}"
            self.info.server_fault = True
            STATS.bump("tftp_sessions_failed")
        finally:
            try:
                self.on_complete(self)
            except Exception:  # noqa: BLE001
                log.exception("on_complete callback failed")

    # ----- RRQ (read from server, client downloads) ------------------
    async def _run_rrq(self) -> None:
        STATS.bump("tftp_rrq")
        if self.request.mode not in ("octet", "netascii"):
            raise tp.TFTPError(tp.ERR_ILLEGAL_OP, f"unsupported mode {self.request.mode}")

        filename = self.request.filename
        log.info("RRQ session %s file=%s vlan=%s client=%s",
                 self.id, filename, self.key.vlan_id, self.key.client_ip)

        try:
            async with self.locks.try_read(filename):
                data = await self.cache.get_or_load(filename, self.source)
                if data is None:
                    raise tp.TFTPError(tp.ERR_NOT_FOUND, f"file not found: {filename}")

                rrq_limit = self._rrq_limit(filename)
                if rrq_limit and len(data) > rrq_limit:
                    raise tp.TFTPError(
                        tp.ERR_DISK_FULL,
                        f"file exceeds size limit ({len(data)} > {rrq_limit})",
                    )

                self.info.total_bytes = len(data)
                neg = negotiate(self.request.options, self.cfg, len(data), is_read=True)
                self.info.blksize = neg.blksize
                self.info.windowsize = neg.windowsize

                # Reject up front if the 16-bit block counter would
                # wrap and rollover isn't permitted.
                total_blocks = (len(data) // neg.blksize) + 1
                if not self.allow_rollover and total_blocks > 65535:
                    raise tp.TFTPError(
                        tp.ERR_UNDEFINED,
                        f"transfer needs {total_blocks} blocks; block-number rollover disabled",
                    )

                if neg.reply:
                    # OACK then wait for ACK 0
                    ok = await self._send_oack_await_ack(neg)
                    if not ok:
                        raise tp.TFTPError(tp.ERR_UNDEFINED, "client never acked OACK")

                await self._send_file(data, neg)

            STATS.bump("tftp_sessions_completed")
            self.info.state = "done"
            log.info("RRQ session %s done (%d bytes)", self.id, self.info.bytes_transferred)

        except FileLockError as e:
            raise tp.TFTPError(tp.ERR_ACCESS, str(e))

    async def _send_oack_await_ack(self, neg: Negotiated) -> bool:
        oack = tp.pack_oack(neg.reply)
        max_retries = int(self.cfg.get("max_retries", TFTP_MAX_RETRIES_DEFAULT))
        for attempt in range(max_retries):
            self._send(oack)
            try:
                pkt = await asyncio.wait_for(self.queue.get(), timeout=neg.timeout)
            except asyncio.TimeoutError:
                continue
            op = tp.peek_opcode(pkt)
            if op == tp.OP_ACK and tp.parse_ack(pkt).block == 0:
                return True
            if op == tp.OP_ERROR:
                err = tp.parse_error(pkt)
                raise tp.TFTPError(err.code, err.message)
        return False

    async def _send_file(self, data: bytes, neg: Negotiated) -> None:
        self.info.state = "transferring"
        blksize = neg.blksize
        windowsize = max(1, neg.windowsize)
        timeout = neg.timeout
        max_retries = int(self.cfg.get("max_retries", TFTP_MAX_RETRIES_DEFAULT))

        total = len(data)
        # Block numbers are 1-indexed and wrap at 65535 -> 0 per RFC 7440
        total_blocks = (total // blksize) + 1  # +1 for final (possibly empty) block
        # The terminator is a block with len < blksize. If total % blksize == 0
        # we must send an extra empty block to signal EOF.

        # Last block successfully ACKed (0 means: we're about to send block 1).
        last_ack = 0
        retries = 0

        def slice_block(n: int) -> bytes:
            offset = (n - 1) * blksize
            return data[offset:offset + blksize]

        while last_ack < total_blocks:
            # Send up to windowsize blocks starting at last_ack + 1
            window_start = last_ack + 1
            window_end = min(window_start + windowsize - 1, total_blocks)
            for bn in range(window_start, window_end + 1):
                chunk = slice_block(bn)
                # bn mod 65536 for wire encoding
                self._send(tp.pack_data(bn % 65536, chunk))

            try:
                pkt = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                retries += 1
                if retries > max_retries:
                    raise tp.TFTPError(tp.ERR_UNDEFINED, "timeout waiting for ACK")
                log.debug("session %s retransmit window %d-%d (retry %d)",
                          self.id, window_start, window_end, retries)
                continue

            op = tp.peek_opcode(pkt)
            if op == tp.OP_ERROR:
                err = tp.parse_error(pkt)
                raise tp.TFTPError(err.code, err.message)
            if op != tp.OP_ACK:
                # Ignore spurious
                continue
            ack = tp.parse_ack(pkt)
            # Map the 16-bit ACK back to the absolute block number. We
            # accept any ACK in (last_ack, window_end]; anything else is
            # a duplicate and just shrinks the window.
            for candidate in range(window_start, window_end + 1):
                if (candidate % 65536) == ack.block:
                    last_ack = candidate
                    retries = 0
                    self.info.bytes_transferred = min(total, last_ack * blksize)
                    self.info.last_update = time.time()
                    break

    # ----- WRQ (write to server, client uploads) ---------------------
    async def _run_wrq(self) -> None:
        STATS.bump("tftp_wrq")
        if not self.enable_writes:
            raise tp.TFTPError(tp.ERR_ACCESS, "writes disabled")
        if self.request.mode not in ("octet", "netascii"):
            raise tp.TFTPError(tp.ERR_ILLEGAL_OP, f"unsupported mode {self.request.mode}")

        filename = self.request.filename
        is_ftp_target = self._is_ftp_name(filename)
        if is_ftp_target and not self.allow_wrq_ftp:
            raise tp.TFTPError(
                tp.ERR_ACCESS,
                "writes to FTP are disabled (enable files.allow_wrq_ftp to permit)",
            )
        # For FTP-bound WRQs we still receive into a temp file under
        # write_root, then upload post-transfer. Using a synthetic name
        # keeps the ftp:// URL from becoming a weird on-disk path.
        if is_ftp_target:
            safe_path = _safe_join(self.write_root, f".wrq_ftp_{self.id}.bin")
        else:
            safe_path = _safe_join(self.write_root, filename)
        log.info("WRQ session %s file=%s vlan=%s client=%s ftp_target=%s",
                 self.id, filename, self.key.vlan_id, self.key.client_ip, is_ftp_target)

        try:
            async with self.locks.try_write(filename):
                neg = negotiate(self.request.options, self.cfg, None, is_read=False)
                self.info.blksize = neg.blksize
                self.info.windowsize = neg.windowsize

                # If the client advertised a tsize, enforce max_file_size
                # up front. If they didn't, we still enforce it while
                # data arrives in _recv_file().
                advertised: Optional[int] = None
                if neg.reply.get("tsize"):
                    try:
                        advertised = int(neg.reply["tsize"])
                        self.info.total_bytes = advertised
                    except ValueError:
                        pass

                if advertised is not None and self.max_wrq_size and advertised > self.max_wrq_size:
                    raise tp.TFTPError(
                        tp.ERR_DISK_FULL,
                        f"upload exceeds max_wrq_size ({advertised} > {self.max_wrq_size})",
                    )

                if advertised is not None and not self.allow_rollover:
                    total_blocks = (advertised // neg.blksize) + 1
                    if total_blocks > 65535:
                        raise tp.TFTPError(
                            tp.ERR_UNDEFINED,
                            f"upload needs {total_blocks} blocks; block-number rollover disabled",
                        )

                # Send OACK (if any options) else plain ACK 0
                if neg.reply:
                    self._send(tp.pack_oack(neg.reply))
                else:
                    self._send(tp.pack_ack(0))

                await self._recv_file(safe_path, neg)

                # Post-receipt FTP upload for ftp:// and f:: targets.
                # The client has already been final-ACKed by _recv_file;
                # if the upload fails, the local temp stays on disk and
                # the failure is logged (no way to notify the client at
                # this point in the protocol).
                if is_ftp_target:
                    try:
                        ok = await self.source.upload(filename, safe_path)
                    except Exception:  # noqa: BLE001
                        log.exception("WRQ %s FTP upload crashed", self.id)
                        ok = False
                    # Clean up the temp regardless - partial upload is
                    # the FTP layer's problem to report via logs.
                    try:
                        import os as _os
                        if _os.path.exists(safe_path):
                            _os.remove(safe_path)
                    except OSError:
                        pass
                    if not ok:
                        raise tp.TFTPError(tp.ERR_UNDEFINED, "FTP upload failed (see logs)")

            STATS.bump("tftp_sessions_completed")
            self.info.state = "done"
            log.info("WRQ session %s done (%d bytes)", self.id, self.info.bytes_transferred)

        except FileLockError as e:
            raise tp.TFTPError(tp.ERR_ACCESS, str(e))

    async def _recv_file(self, dest_path: str, neg: Negotiated) -> None:
        import os
        import aiofiles

        self.info.state = "receiving"
        blksize = neg.blksize
        windowsize = max(1, neg.windowsize)
        timeout = neg.timeout
        max_retries = int(self.cfg.get("max_retries", TFTP_MAX_RETRIES_DEFAULT))

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        tmp_path = dest_path + ".part"

        expected = 1
        retries = 0
        # Tracks blocks received within the current window
        window_received: dict[int, bytes] = {}

        async with aiofiles.open(tmp_path, "wb") as fh:
            while True:
                try:
                    pkt = await asyncio.wait_for(self.queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    retries += 1
                    if retries > max_retries:
                        raise tp.TFTPError(tp.ERR_UNDEFINED, "timeout waiting for DATA")
                    # Re-send last ACK to prompt client retransmit
                    self._send(tp.pack_ack((expected - 1) % 65536))
                    continue

                op = tp.peek_opcode(pkt)
                if op == tp.OP_ERROR:
                    err = tp.parse_error(pkt)
                    raise tp.TFTPError(err.code, err.message)
                if op != tp.OP_DATA:
                    continue
                data = tp.parse_data(pkt)
                retries = 0

                # Map the 16-bit wire block back to the absolute number
                # by searching the small current window.
                matched = None
                for candidate in range(expected, expected + windowsize):
                    if (candidate % 65536) == data.block:
                        matched = candidate
                        break
                if matched is None:
                    # Duplicate or out-of-window - re-ACK the last good
                    # absolute block so the client can resync.
                    self._send(tp.pack_ack((expected - 1) % 65536))
                    continue

                if matched == expected:
                    if self.max_wrq_size and (self.info.bytes_transferred + len(data.payload)) > self.max_wrq_size:
                        raise tp.TFTPError(
                            tp.ERR_DISK_FULL,
                            f"upload exceeded max_wrq_size ({self.max_wrq_size})",
                        )
                    await fh.write(data.payload)
                    self.info.bytes_transferred += len(data.payload)
                    STATS.bump("bytes_received", len(data.payload))
                    self.info.last_update = time.time()
                    expected += 1
                    if not self.allow_rollover and expected > 65536:
                        raise tp.TFTPError(
                            tp.ERR_UNDEFINED,
                            "upload exceeded 65535 blocks; rollover disabled",
                        )

                    # Drain any already-received contiguous blocks
                    while expected in window_received:
                        buf = window_received.pop(expected)
                        await fh.write(buf)
                        self.info.bytes_transferred += len(buf)
                        STATS.bump("bytes_received", len(buf))
                        expected += 1

                    # ACK cumulative (last contiguous absolute block)
                    self._send(tp.pack_ack((expected - 1) % 65536))

                    if len(data.payload) < blksize:
                        break  # EOF
                else:
                    # Future block inside the window - buffer it and
                    # re-ACK the last in-order block so the client
                    # knows to retransmit the gap (RFC 7440 §3).
                    window_received[matched] = data.payload
                    self._send(tp.pack_ack((expected - 1) % 65536))

        import os as _os
        _os.replace(tmp_path, dest_path)


def _safe_join(root: str, filename: str) -> str:
    """Join ``filename`` under ``root`` without allowing directory
    traversal. We also strip any leading slashes / drive letters so
    buggy clients can't escape."""
    import os
    cleaned = filename.replace("\\", "/").lstrip("/")
    # Reject traversal segments outright
    for part in cleaned.split("/"):
        if part in ("..",):
            raise tp.TFTPError(tp.ERR_ACCESS, "path traversal rejected")
    joined = os.path.abspath(os.path.join(root, cleaned))
    root_abs = os.path.abspath(root)
    if not (joined == root_abs or joined.startswith(root_abs + os.sep)):
        raise tp.TFTPError(tp.ERR_ACCESS, "path escapes write root")
    return joined
