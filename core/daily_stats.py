"""Daily session counter with on-disk persistence.

Records per-day aggregates for every finished TFTP session (success or
failure) from both engines. Powers the daily-sessions chart on the web
dashboard.

The file format is a JSON object keyed by ISO date (YYYY-MM-DD) with
aggregate values:

    {
      "2026-04-15": {"total": 42, "completed": 40, "failed": 2,
                      "bytes_sent": 1234567, "bytes_received": 0},
      ...
    }

Writes are rate-limited to avoid hammering the disk; in-memory state is
always authoritative and written atomically via a temp file + rename.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set

# Employee ID appears in FTP-prefixed filenames like ``f::42/path/to/file``.
# Matches the regex used client-side in util.js resolveUser().
_USER_ID_RE = re.compile(r"^f::(\d{2,3})/")

log = logging.getLogger("nishro.daily")

_FLUSH_INTERVAL = 5.0   # seconds between disk flushes
_RETAIN_DAYS    = 180   # keep roughly 6 months of history


_INT_KEYS = ("total", "completed", "failed", "bytes_sent", "bytes_received")


class DailyStats:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, int]] = {}
        # Parallel per-day sets of unique user IDs. Kept separate from
        # _data so numeric serialisation stays simple.
        self._users: Dict[str, Set[str]] = {}
        # Per-day set of unique TFTP client MAC addresses -- drives the
        # "devices requested TFTP today" rail chip. Normalised to lower
        # hex with colons before insertion.
        self._devices: Dict[str, Set[str]] = {}
        # Per-day set of unique web-UI visitor IPs -- drives the
        # "web visitors today" rail chip. Populated by a FastAPI
        # middleware on every HTTP/WS request.
        self._visitors: Dict[str, Set[str]] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    # -- Load / save --------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh) or {}
            if not isinstance(raw, dict):
                return
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                self._data[k] = {kk: int(v.get(kk, 0) or 0) for kk in _INT_KEYS}
                users = v.get("users")
                if isinstance(users, list):
                    self._users[k] = {str(u) for u in users if u}
                devices = v.get("devices")
                if isinstance(devices, list):
                    self._devices[k] = {str(m).lower() for m in devices if m}
                visitors = v.get("visitors")
                if isinstance(visitors, list):
                    self._visitors[k] = {str(ip) for ip in visitors if ip}
        except Exception:  # noqa: BLE001
            log.exception("daily_stats: load failed; starting empty")

    def _flush_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # Merge user/device/visitor sets back into each day bucket.
            out: Dict[str, Dict[str, Any]] = {}
            all_days = (set(self._data)
                        | set(self._devices)
                        | set(self._visitors)
                        | set(self._users))
            for k in all_days:
                bucket: Dict[str, Any] = dict(self._data.get(k) or {})
                users = self._users.get(k)
                if users:
                    bucket["users"] = sorted(users)
                devices = self._devices.get(k)
                if devices:
                    bucket["devices"] = sorted(devices)
                visitors = self._visitors.get(k)
                if visitors:
                    bucket["visitors"] = sorted(visitors)
                out[k] = bucket
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(out, fh, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_flush = time.monotonic()
        except Exception:  # noqa: BLE001
            log.exception("daily_stats: flush failed")

    def _maybe_flush_locked(self, force: bool = False) -> None:
        if not self._dirty:
            return
        if force or (time.monotonic() - self._last_flush) >= _FLUSH_INTERVAL:
            self._flush_locked()

    # -- Public API ---------------------------------------------------
    def record(self, *, success: bool, server_fault: bool = False,
               bytes_sent: int = 0, bytes_received: int = 0,
               filename: Optional[str] = None,
               client_mac: Optional[str] = None) -> None:
        """Record one finished session.

        ``success=True`` -> transfer completed (-> ``completed`` bucket).
        ``success=False`` + ``server_fault=True`` -> real server failure
        (-> ``failed`` bucket). ``success=False`` + ``server_fault=False``
        -> client-side dropout / policy rejection: still counted in
        ``total`` so session history stays honest, but NOT in ``failed``.
        """
        today = date.today().isoformat()
        with self._lock:
            bucket = self._data.setdefault(today, {
                "total": 0, "completed": 0, "failed": 0,
                "bytes_sent": 0, "bytes_received": 0,
            })
            bucket["total"] += 1
            if success:
                bucket["completed"] += 1
            elif server_fault:
                bucket["failed"] += 1
            bucket["bytes_sent"] += int(bytes_sent or 0)
            bucket["bytes_received"] += int(bytes_received or 0)
            uid = _extract_user_id(filename)
            if uid is not None:
                self._users.setdefault(today, set()).add(uid)
            if client_mac:
                self._devices.setdefault(today, set()).add(str(client_mac).lower())
            self._dirty = True
            self._prune_locked()
            self._maybe_flush_locked()

    def record_visitor(self, ip: Optional[str]) -> None:
        """Mark a unique web-UI visitor for today (keyed by client IP)."""
        if not ip:
            return
        today = date.today().isoformat()
        with self._lock:
            bucket = self._visitors.setdefault(today, set())
            if ip in bucket:
                return  # already counted; skip the flush scheduling
            bucket.add(ip)
            self._dirty = True
            self._prune_locked()
            self._maybe_flush_locked()

    def _prune_locked(self) -> None:
        """Drop buckets older than _RETAIN_DAYS."""
        cutoff = (date.today() - timedelta(days=_RETAIN_DAYS)).isoformat()
        stale = [k for k in self._data if k < cutoff]
        for k in stale:
            self._data.pop(k, None)
            self._users.pop(k, None)
            self._devices.pop(k, None)
            self._visitors.pop(k, None)

    def recent(self, days: int = 30) -> List[Dict[str, Any]]:
        """Return a list of ``days`` buckets ending today, oldest first.

        Missing days are zero-filled so the chart renders a continuous
        timeline.
        """
        days = max(1, min(days, _RETAIN_DAYS))
        today = date.today()
        out: List[Dict[str, Any]] = []
        with self._lock:
            for i in range(days - 1, -1, -1):
                d = (today - timedelta(days=i)).isoformat()
                b = self._data.get(d) or {}
                out.append({
                    "date": d,
                    "total": int(b.get("total", 0)),
                    "completed": int(b.get("completed", 0)),
                    "failed": int(b.get("failed", 0)),
                    "bytes_sent": int(b.get("bytes_sent", 0)),
                    "bytes_received": int(b.get("bytes_received", 0)),
                    "users": len(self._users.get(d) or ()),
                })
        return out

    def today(self) -> Dict[str, int]:
        today = date.today().isoformat()
        with self._lock:
            b = self._data.get(today) or {}
            return {
                "date": today,
                "total": int(b.get("total", 0)),
                "completed": int(b.get("completed", 0)),
                "failed": int(b.get("failed", 0)),
                "bytes_sent": int(b.get("bytes_sent", 0)),
                "bytes_received": int(b.get("bytes_received", 0)),
                "users": len(self._users.get(today) or ()),
                "devices": len(self._devices.get(today) or ()),
                "visitors": len(self._visitors.get(today) or ()),
            }

    def user_counts(self) -> Dict[str, int]:
        """Return unique-user counts for today and across all retained days."""
        today_key = date.today().isoformat()
        with self._lock:
            today_n = len(self._users.get(today_key) or ())
            all_users: Set[str] = set()
            for s in self._users.values():
                all_users.update(s)
        return {"today": today_n, "total": len(all_users)}

    def reach_counts(self) -> Dict[str, int]:
        """Unique web visitors + TFTP devices seen today."""
        today_key = date.today().isoformat()
        with self._lock:
            return {
                "visitors_today": len(self._visitors.get(today_key) or ()),
                "devices_today":  len(self._devices.get(today_key)  or ()),
            }

    def flush(self) -> None:
        with self._lock:
            self._maybe_flush_locked(force=True)


# -- Module-level singleton ------------------------------------------
_INSTANCE: Optional[DailyStats] = None


def init(path: str) -> DailyStats:
    global _INSTANCE
    _INSTANCE = DailyStats(path)
    return _INSTANCE


def get() -> Optional[DailyStats]:
    return _INSTANCE


def record(*, success: bool, server_fault: bool = False,
           bytes_sent: int = 0, bytes_received: int = 0,
           filename: Optional[str] = None,
           client_mac: Optional[str] = None) -> None:
    """Safe no-op wrapper used from engine callbacks."""
    inst = _INSTANCE
    if inst is None:
        return
    try:
        inst.record(success=success, server_fault=server_fault,
                    bytes_sent=bytes_sent, bytes_received=bytes_received,
                    filename=filename, client_mac=client_mac)
    except Exception:  # noqa: BLE001
        log.exception("daily_stats: record failed")


def record_visitor(ip: Optional[str]) -> None:
    inst = _INSTANCE
    if inst is None:
        return
    try:
        inst.record_visitor(ip)
    except Exception:  # noqa: BLE001
        log.exception("daily_stats: record_visitor failed")


def _extract_user_id(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    m = _USER_ID_RE.match(filename)
    if not m:
        return None
    # Strip leading zeros for consistent lookup against the employee roster.
    return str(int(m.group(1)))
