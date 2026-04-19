"""Option negotiation for RFC 2347 / 2348 / 2349 / 7440.

Each of blksize / windowsize / timeout has its own policy taken from
``tftp.negotiation`` in the config:

    client  - honour the client's requested value, clamped to limits
    server  - use the configured default (ignores the client value)
    min     - use the configured minimum from limits (ignores client)
    max     - use the configured maximum from limits (ignores client)

The legacy ``tftp.option_mode`` knob (``accept`` / ``force`` / ``off``)
is still recognised for backward compatibility and, if present, maps
onto the new policy:

    accept -> client
    force  -> server
    off    -> disable negotiation entirely (no OACK)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.constants import (
    TFTP_BLKSIZE_DEFAULT,
    TFTP_BLKSIZE_MAX,
    TFTP_BLKSIZE_MIN,
    TFTP_CLASSIC_BLKSIZE,
    TFTP_TIMEOUT_DEFAULT,
    TFTP_TIMEOUT_MAX,
    TFTP_TIMEOUT_MIN,
    TFTP_WINDOWSIZE_DEFAULT,
    TFTP_WINDOWSIZE_MAX,
    TFTP_WINDOWSIZE_MIN,
)


@dataclass
class Negotiated:
    blksize: int
    windowsize: int
    timeout: int
    tsize: Optional[int]
    # Options the server will echo back in the OACK. Empty dict means
    # "no OACK - send a plain ACK/DATA instead".
    reply: dict[str, str]


def _pick(policy: str, client_val: Optional[int], default: int, lo: int, hi: int) -> int:
    """Resolve one option given the policy and a client request."""
    policy = (policy or "client").lower()
    if policy == "server":
        chosen = default
    elif policy == "min":
        chosen = lo
    elif policy == "max":
        chosen = hi
    else:  # "client" or unknown
        chosen = client_val if client_val is not None else default
    return max(lo, min(hi, chosen))


def _legacy_policy(cfg_tftp: dict) -> Optional[dict]:
    """Translate the deprecated ``option_mode`` field into the new
    per-option policy map, or return ``None`` if the new key is
    already in use."""
    if "negotiation" in cfg_tftp:
        return None
    legacy = str(cfg_tftp.get("option_mode", "")).lower()
    if legacy == "accept":
        return {"blksize": "client", "windowsize": "client", "timeout": "client"}
    if legacy == "force":
        return {"blksize": "server", "windowsize": "server", "timeout": "server"}
    if legacy == "off":
        return {"_off": "true"}
    return None


def negotiate(
    client_opts: dict[str, str],
    cfg_tftp: dict,
    file_size: Optional[int],
    is_read: bool,
) -> Negotiated:
    defaults = cfg_tftp.get("defaults", {}) or {}
    limits = cfg_tftp.get("limits", {}) or {}

    legacy = _legacy_policy(cfg_tftp)
    if legacy and legacy.get("_off"):
        # Legacy "off" mode: no negotiation, no OACK.
        return Negotiated(
            blksize=TFTP_CLASSIC_BLKSIZE,
            windowsize=1,
            timeout=int(defaults.get("timeout", TFTP_TIMEOUT_DEFAULT)),
            tsize=file_size,
            reply={},
        )

    policy = legacy or cfg_tftp.get("negotiation", {}) or {}

    def_blk = int(defaults.get("blksize", TFTP_BLKSIZE_DEFAULT))
    def_win = int(defaults.get("windowsize", TFTP_WINDOWSIZE_DEFAULT))
    def_tmo = int(defaults.get("timeout", TFTP_TIMEOUT_DEFAULT))

    blk_lo = int(limits.get("blksize_min", TFTP_BLKSIZE_MIN))
    blk_hi = int(limits.get("blksize_max", TFTP_BLKSIZE_MAX))
    win_lo = int(limits.get("windowsize_min", TFTP_WINDOWSIZE_MIN))
    win_hi = int(limits.get("windowsize_max", TFTP_WINDOWSIZE_MAX))
    tmo_lo = int(limits.get("timeout_min", TFTP_TIMEOUT_MIN))
    tmo_hi = int(limits.get("timeout_max", TFTP_TIMEOUT_MAX))

    opts = {k.lower(): v for k, v in (client_opts or {}).items()}

    def _client_int(name: str) -> Optional[int]:
        if name not in opts:
            return None
        try:
            return int(opts[name])
        except ValueError:
            return None

    reply: dict[str, str] = {}

    # blksize - default to 512 (classic TFTP) when the client doesn't
    # ask and the policy is "client", matching RFC 1350 behaviour.
    blk_client = _client_int("blksize")
    blk_policy = str(policy.get("blksize", "client")).lower()
    if blk_policy == "client" and blk_client is None:
        blksize = TFTP_CLASSIC_BLKSIZE
    else:
        blksize = _pick(blk_policy, blk_client, def_blk, blk_lo, blk_hi)
        if blk_client is not None:
            reply["blksize"] = str(blksize)

    # windowsize - default window 1 when client is silent (RFC 7440)
    win_client = _client_int("windowsize")
    win_policy = str(policy.get("windowsize", "client")).lower()
    if win_policy == "client" and win_client is None:
        windowsize = 1
    else:
        windowsize = _pick(win_policy, win_client, def_win, win_lo, win_hi)
        if win_client is not None:
            reply["windowsize"] = str(windowsize)

    # timeout - only returned in OACK if the client asked
    tmo_client = _client_int("timeout")
    tmo_policy = str(policy.get("timeout", "client")).lower()
    timeout = _pick(tmo_policy, tmo_client, def_tmo, tmo_lo, tmo_hi)
    if tmo_client is not None:
        reply["timeout"] = str(timeout)

    # tsize - RRQ client sends 0 ("tell me"), we reply with the real
    # size; WRQ client sends the real size, we echo it back.
    if "tsize" in opts:
        if is_read and file_size is not None:
            reply["tsize"] = str(file_size)
        elif not is_read:
            try:
                reply["tsize"] = str(int(opts["tsize"]))
            except ValueError:
                pass

    return Negotiated(
        blksize=blksize,
        windowsize=windowsize,
        timeout=timeout,
        tsize=file_size,
        reply=reply,
    )
