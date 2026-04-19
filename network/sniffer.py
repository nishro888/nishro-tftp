"""Raw L2 sniffer + sender bridge into asyncio.

Scapy's sniffer runs on its own thread (pcap is blocking). We push
parsed packets into an asyncio.Queue via ``call_soon_threadsafe`` so
the event loop can consume them without ever blocking on pcap. Sends
reuse a single persistent L2 socket opened against the chosen NIC,
avoiding the per-call setup cost of ``sendp``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from scapy.config import conf
from scapy.sendrecv import AsyncSniffer
from scapy.packet import Packet

from core.constants import SNIFFER_QUEUE_MAX

from .packet_utils import ParsedPacket, parse

log = logging.getLogger("nishro.sniffer")


class L2Bridge:
    """Owns the scapy sniffer, its thread, and the send socket."""

    def __init__(
        self,
        iface: str,
        loop: asyncio.AbstractEventLoop,
        promisc: bool = True,
        queue_max: int = SNIFFER_QUEUE_MAX,
    ) -> None:
        self.iface = iface
        self.loop = loop
        self.promisc = promisc
        self.queue: asyncio.Queue[ParsedPacket] = asyncio.Queue(maxsize=queue_max)
        self._sniffer: Optional[AsyncSniffer] = None
        self._send_sock = None
        self._send_lock = threading.Lock()
        self._drops = 0

    # -- Lifecycle -----------------------------------------------------
    def start(self) -> None:
        log.info("opening L2 socket on %s (promisc=%s)", self.iface, self.promisc)
        # Persistent send socket - created via scapy's default L2 type
        # which on Windows is an Npcap/WinPcap socket.
        self._send_sock = conf.L2socket(iface=self.iface)

        self._sniffer = AsyncSniffer(
            iface=self.iface,
            prn=self._on_packet,
            store=False,
            promisc=self.promisc,
        )
        self._sniffer.start()
        log.info("sniffer started on %s", self.iface)

    def stop(self) -> None:
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._sniffer = None
        if self._send_sock is not None:
            try:
                self._send_sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._send_sock = None

    # -- Sending -------------------------------------------------------
    def send(self, pkt: Packet) -> None:
        """Fire-and-forget send. Safe from either the asyncio loop or
        a worker thread - scapy's sockets aren't re-entrant so we
        serialise sends under ``_send_lock``."""
        if self._send_sock is None:
            log.warning("send called before sniffer.start()")
            return
        with self._send_lock:
            try:
                self._send_sock.send(pkt)
            except Exception:  # noqa: BLE001
                log.exception("failed to send frame")

    def send_bytes(self, raw: bytes) -> None:
        """Ultra-fast send path that bypasses scapy's Packet serialiser.

        On Windows, ``conf.L2socket`` is ``L2pcapSocket`` whose underlying
        Npcap handle lives at ``self._send_sock.outs``; we can feed it raw
        bytes directly via ``outs.send(raw)`` (which maps straight onto
        ``pcap_sendpacket``). That avoids per-block ``raw(pkt)`` overhead
        which for a 50 MiB transfer at blksize=1468 is tens of thousands
        of scapy builds.
        """
        if self._send_sock is None:
            log.warning("send_bytes called before sniffer.start()")
            return
        with self._send_lock:
            try:
                outs = getattr(self._send_sock, "outs", None)
                if outs is not None and hasattr(outs, "send"):
                    outs.send(raw)
                    return
                pcap_h = getattr(self._send_sock, "pcap", None)
                if pcap_h is not None and hasattr(pcap_h, "sendpacket"):
                    pcap_h.sendpacket(raw)
                    return
                # Last-resort fallback: wrap in scapy Raw (slow)
                from scapy.packet import Raw as _Raw
                self._send_sock.send(_Raw(load=raw))
            except Exception:  # noqa: BLE001
                log.exception("failed to send raw frame")

    # -- Receive path --------------------------------------------------
    def _on_packet(self, pkt: Packet) -> None:
        # Runs on the scapy thread. Parse here so the async consumer
        # never does work on raw scapy packets.
        parsed = parse(pkt)
        if parsed is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._enqueue, parsed)
        except RuntimeError:
            # Loop is shutting down
            pass

    def _enqueue(self, parsed: ParsedPacket) -> None:
        try:
            self.queue.put_nowait(parsed)
        except asyncio.QueueFull:
            self._drops += 1
            if self._drops % 100 == 1:
                log.warning("packet queue full - dropped %d frames so far", self._drops)

    async def packets(self):
        while True:
            yield await self.queue.get()

    @property
    def drops(self) -> int:
        return self._drops
