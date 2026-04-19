"""Cross-engine counter accumulator.

The Python ``STATS`` singleton and the C engine each maintain their own
cumulative counters that reset when their engine stops. When the user
swaps between engines we don't want dashboard numbers to appear to
reset, so we fold the outgoing engine's totals into a process-local
carryover before the engine is torn down; the web layer adds this
carryover on top of the current engine's live counts.

The carryover lives in-memory only: engine swaps happen inside the same
Python process (via the ``_amain`` restart loop), so a module-level
dict survives them. A hard process exit drops the carryover -- that
matches the user's expectation that restarting the program is a fresh
session.
"""
from __future__ import annotations

import threading
from typing import Dict, Iterable, Mapping

# Cumulative counters that should survive an engine swap. Gauges and
# derived rates (cache_hit_rate, uptime, session_count) are excluded --
# they're computed fresh every snapshot.
_KEYS: tuple[str, ...] = (
    "bytes_sent", "bytes_received",
    "tftp_rrq", "tftp_wrq", "tftp_errors",
    "tftp_sessions_total", "tftp_sessions_completed", "tftp_sessions_failed",
    "acl_denied", "vlan_denied", "ip_denied",
    "arp_requests", "arp_replies",
    "icmp_requests", "icmp_replies",
    "cache_hits", "cache_misses",
)

_lock = threading.Lock()
_carry: Dict[str, int] = {k: 0 for k in _KEYS}


def keys() -> Iterable[str]:
    return _KEYS


def add(src: Mapping[str, object]) -> None:
    """Fold ``src`` into the carryover. Unknown keys are ignored."""
    with _lock:
        for k in _KEYS:
            if k in src:
                try:
                    _carry[k] += int(src[k] or 0)
                except (TypeError, ValueError):
                    continue


def apply(dst: Dict[str, object]) -> Dict[str, object]:
    """Return a copy of ``dst`` with carryover added into each tracked key."""
    with _lock:
        out = dict(dst)
        for k, v in _carry.items():
            if v:
                try:
                    out[k] = int(out.get(k, 0) or 0) + v
                except (TypeError, ValueError):
                    out[k] = v
        return out


def snapshot() -> Dict[str, int]:
    with _lock:
        return dict(_carry)


def reset() -> None:
    with _lock:
        for k in _carry:
            _carry[k] = 0
