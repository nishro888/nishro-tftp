"""Admin authentication - PBKDF2-SHA256 password hashing + token sessions.

Stores credentials in ``auth.json`` next to ``config.yaml``. The file
contains a JSON object with ``username``, ``salt`` (hex), ``hash`` (hex),
and ``iterations``.

First-run state: no ``auth.json`` exists, so ``has_password()`` returns
False. When ``web.require_auth`` is flipped on, the next attempt to
enter Admin mode triggers the browser's setup prompt; the user picks
a fresh username + password and the file is created via
``setup_initial``. No seeded default credentials ship anymore.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from pathlib import Path

log = logging.getLogger("nishro.auth")

ITERATIONS = 260_000
HASH_ALGO = "sha256"
SALT_BYTES = 32
TOKEN_BYTES = 32
# Token validity in seconds (10 minutes idle timeout).
TOKEN_TTL = 10 * 60

def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(HASH_ALGO, password.encode("utf-8"), salt, ITERATIONS)


class AuthStore:
    """On-disk credential store + in-memory token set."""

    def __init__(self, base_dir: str):
        self.path = Path(base_dir) / "auth.json"
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}  # token -> expiry (epoch)
        # Count of live WebSockets per token. When a browser closes its
        # tab the WS disconnects; when the count drops to zero we revoke
        # the token so another admin can log in immediately, rather than
        # waiting out the TTL.
        self._ws_refs: dict[str, int] = {}
        self._ensure_file()

    # -- bootstrap --------------------------------------------------------
    def _ensure_file(self) -> None:
        if self.path.exists():
            log.info("auth store loaded from %s", self.path)
            return
        log.info("no auth.json found - first-run (admin must set credentials via setup prompt)")

    def has_password(self) -> bool:
        """True when a credential record exists on disk."""
        if not self.path.exists():
            return False
        try:
            creds = self._load()
        except Exception:  # noqa: BLE001
            return False
        return bool(creds.get("username")) and bool(creds.get("hash")) and bool(creds.get("salt"))

    def setup_initial(self, username: str, password: str) -> bool:
        """Create the first credential record. Refuses if one already exists."""
        if self.has_password():
            return False
        if not username or not password:
            return False
        self._save_creds(username, password)
        log.info("initial admin credentials created (user=%s)", username)
        return True

    def _save_creds(self, username: str, password: str) -> None:
        salt = secrets.token_bytes(SALT_BYTES)
        hashed = _hash_password(password, salt)
        data = {
            "username": username,
            "salt": salt.hex(),
            "hash": hashed.hex(),
            "iterations": ITERATIONS,
        }
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)

    def _load(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # -- verify -----------------------------------------------------------
    def verify(self, username: str, password: str) -> bool:
        """Return True if username + password match the stored credentials."""
        if not self.has_password():
            return False
        try:
            creds = self._load()
        except Exception:
            log.exception("failed to read auth.json")
            return False
        if username != creds.get("username", ""):
            return False
        salt = bytes.fromhex(creds.get("salt", ""))
        expected = bytes.fromhex(creds.get("hash", ""))
        actual = _hash_password(password, salt)
        return secrets.compare_digest(actual, expected)

    # -- tokens -----------------------------------------------------------
    def create_token(self) -> str:
        """Issue a new session token."""
        import time
        token = secrets.token_hex(TOKEN_BYTES)
        with self._lock:
            self._tokens[token] = time.time() + TOKEN_TTL
        return token

    def check_token(self, token: str | None) -> bool:
        """Return True if the token is valid and not expired.
        Each valid check refreshes the expiry (sliding window)."""
        if not token:
            return False
        import time
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if time.time() > expiry:
                del self._tokens[token]
                return False
            # Slide the expiry forward on each successful check.
            self._tokens[token] = time.time() + TOKEN_TTL
            return True

    def revoke_token(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)
            self._ws_refs.pop(token, None)

    def ws_attach(self, token: str | None) -> bool:
        """Bind a WebSocket to ``token``. Returns True if the token is
        known + valid (and the refcount was incremented)."""
        if not token:
            return False
        import time
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None or time.time() > expiry:
                self._tokens.pop(token, None)
                self._ws_refs.pop(token, None)
                return False
            self._ws_refs[token] = self._ws_refs.get(token, 0) + 1
            return True

    def ws_detach(self, token: str | None) -> None:
        """Release a WebSocket's hold on ``token``. When the last hold
        drops, revoke the token immediately so a new admin can log in."""
        if not token:
            return
        with self._lock:
            n = self._ws_refs.get(token, 0) - 1
            if n <= 0:
                self._ws_refs.pop(token, None)
                # Only revoke if we were actually tracking it; a
                # detach for an unknown token is a no-op.
                self._tokens.pop(token, None)
            else:
                self._ws_refs[token] = n

    def cleanup_expired(self) -> None:
        """Remove all expired tokens."""
        import time
        now = time.time()
        with self._lock:
            expired = [t for t, exp in self._tokens.items() if now > exp]
            for t in expired:
                del self._tokens[t]

    def has_active_session(self) -> bool:
        """True iff at least one non-expired admin token is outstanding.

        Purges expired tokens as a side effect, so a stale session that's
        past its TTL doesn't block a new login.
        """
        import time
        now = time.time()
        with self._lock:
            expired = [t for t, exp in self._tokens.items() if now > exp]
            for t in expired:
                del self._tokens[t]
            return bool(self._tokens)

    # -- change password --------------------------------------------------
    def change_password(self, old_password: str, new_username: str,
                        new_password: str) -> bool:
        """Change credentials. Requires the current password for verification."""
        creds = self._load()
        current_user = creds.get("username", "")
        if not self.verify(current_user, old_password):
            return False
        self._save_creds(new_username or current_user, new_password)
        # Invalidate all existing tokens.
        with self._lock:
            self._tokens.clear()
        log.info("admin credentials changed (user=%s)", new_username or current_user)
        return True
