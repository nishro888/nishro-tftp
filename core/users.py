"""Employee ID/Name mapping store.

Maintains a simple JSON file mapping short numeric employee IDs (2-3
digits) to display names.  The TFTP filename prefix ``f::NNN/...``
carries the employee ID; the UI resolves it to a name via this store.

Example mapping::

    {"45": "Roni", "12": "Alice"}

The full device identifier is ``BDCOM<zero-padded-to-4>`` (e.g.
``BDCOM0045``), but the canonical key in this store is the raw short
numeric string (``"45"``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Optional

log = logging.getLogger("nishro.users")

# Matches the ``f::NNN/`` prefix in TFTP filenames.
_FTP_PREFIX_RE = re.compile(r"^f::(\d{2,3})/")


class UserStore:
    """Thread-safe employee lookup backed by a JSON file."""

    def __init__(self, path: str = "users.json") -> None:
        self._path = path
        self._lock = threading.Lock()
        self._users: dict[str, str] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not os.path.isfile(self._path):
            log.info("users file %s does not exist yet - starting empty", self._path)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._users = {str(k): str(v) for k, v in data.items()}
                log.info("loaded %d user(s) from %s", len(self._users), self._path)
            else:
                log.warning("users file has unexpected format - ignoring")
        except Exception:
            log.exception("failed to load users from %s", self._path)

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._users, fh, indent=2, ensure_ascii=False)
        except Exception:
            log.exception("failed to save users to %s", self._path)

    # -- public API --------------------------------------------------------
    def all(self) -> dict[str, str]:
        """Return a copy of the full {id: name} mapping."""
        with self._lock:
            return dict(self._users)

    def get(self, employee_id: str) -> Optional[str]:
        with self._lock:
            return self._users.get(str(employee_id))

    def set(self, employee_id: str, name: str) -> None:
        with self._lock:
            self._users[str(employee_id)] = name
            self._save()

    def delete(self, employee_id: str) -> bool:
        with self._lock:
            if str(employee_id) in self._users:
                del self._users[str(employee_id)]
                self._save()
                return True
            return False

    def replace_all(self, mapping: dict[str, str]) -> None:
        """Overwrite the entire mapping (used by the admin UI bulk-save)."""
        with self._lock:
            self._users = {str(k): str(v) for k, v in mapping.items()}
            self._save()

    # -- helper for session display ----------------------------------------
    def resolve_filename(self, filename: str) -> Optional[str]:
        """Extract the employee ID from an ``f::NNN/...`` filename and
        return the mapped name, or ``None`` if no match / no mapping."""
        m = _FTP_PREFIX_RE.match(filename or "")
        if not m:
            return None
        eid = m.group(1).lstrip("0") or "0"
        with self._lock:
            return self._users.get(eid)
