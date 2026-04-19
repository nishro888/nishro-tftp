"""Global counters + rolling stats exposed to the web UI."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Counters:
    arp_requests: int = 0
    arp_replies: int = 0
    icmp_requests: int = 0
    icmp_replies: int = 0
    tftp_rrq: int = 0
    tftp_wrq: int = 0
    tftp_errors: int = 0
    tftp_sessions_total: int = 0
    tftp_sessions_completed: int = 0
    tftp_sessions_failed: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    acl_denied: int = 0
    vlan_denied: int = 0
    ip_denied: int = 0


class Stats:
    """Thread-safe counter store used by every subsystem."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters = Counters()
        self.started_at = time.time()

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self.counters, name, getattr(self.counters, name) + amount)

    def reset_counters(self) -> dict[str, int]:
        """Capture the current cumulative counters and zero them out.

        Called on engine swap: the outgoing totals are folded into the
        carryover module so the dashboard keeps showing them, and the
        new engine starts from a clean slate.
        """
        with self._lock:
            old = {
                "arp_requests": self.counters.arp_requests,
                "arp_replies": self.counters.arp_replies,
                "icmp_requests": self.counters.icmp_requests,
                "icmp_replies": self.counters.icmp_replies,
                "tftp_rrq": self.counters.tftp_rrq,
                "tftp_wrq": self.counters.tftp_wrq,
                "tftp_errors": self.counters.tftp_errors,
                "tftp_sessions_total": self.counters.tftp_sessions_total,
                "tftp_sessions_completed": self.counters.tftp_sessions_completed,
                "tftp_sessions_failed": self.counters.tftp_sessions_failed,
                "bytes_sent": self.counters.bytes_sent,
                "bytes_received": self.counters.bytes_received,
                "cache_hits": self.counters.cache_hits,
                "cache_misses": self.counters.cache_misses,
                "acl_denied": self.counters.acl_denied,
                "vlan_denied": self.counters.vlan_denied,
                "ip_denied": self.counters.ip_denied,
            }
            self.counters = Counters()
            return old

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            c = self.counters
            total = c.cache_hits + c.cache_misses
            hit_rate = (c.cache_hits / total) if total else 0.0
            return {
                "uptime": time.time() - self.started_at,
                "arp_requests": c.arp_requests,
                "arp_replies": c.arp_replies,
                "icmp_requests": c.icmp_requests,
                "icmp_replies": c.icmp_replies,
                "tftp_rrq": c.tftp_rrq,
                "tftp_wrq": c.tftp_wrq,
                "tftp_errors": c.tftp_errors,
                "tftp_sessions_total": c.tftp_sessions_total,
                "tftp_sessions_completed": c.tftp_sessions_completed,
                "tftp_sessions_failed": c.tftp_sessions_failed,
                "bytes_sent": c.bytes_sent,
                "bytes_received": c.bytes_received,
                "cache_hits": c.cache_hits,
                "cache_misses": c.cache_misses,
                "cache_hit_rate": hit_rate,
                "acl_denied": c.acl_denied,
                "vlan_denied": c.vlan_denied,
                "ip_denied": c.ip_denied,
            }


# Global singleton - every subsystem imports this.
STATS = Stats()
