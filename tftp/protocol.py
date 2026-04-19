"""TFTP wire-format helpers - opcode constants, pack / unpack."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5
OP_OACK = 6

# RFC 1350 error codes
ERR_UNDEFINED = 0
ERR_NOT_FOUND = 1
ERR_ACCESS = 2
ERR_DISK_FULL = 3
ERR_ILLEGAL_OP = 4
ERR_UNKNOWN_TID = 5
ERR_FILE_EXISTS = 6
ERR_NO_USER = 7
ERR_OPTION = 8  # RFC 2347


class TFTPError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"TFTP error {code}: {message}")
        self.code = code
        self.message = message


@dataclass
class Request:
    opcode: int  # RRQ or WRQ
    filename: str
    mode: str
    options: dict[str, str]


@dataclass
class Data:
    block: int
    payload: bytes


@dataclass
class Ack:
    block: int


@dataclass
class Error:
    code: int
    message: str


@dataclass
class OAck:
    options: dict[str, str]


# -- Unpack --------------------------------------------------------------

def peek_opcode(pkt: bytes) -> int:
    if len(pkt) < 2:
        raise TFTPError(ERR_ILLEGAL_OP, "short packet")
    return struct.unpack("!H", pkt[:2])[0]


def _split_cstrings(buf: bytes) -> list[str]:
    """Split a buffer of NUL-terminated strings, tolerating a missing
    trailing NUL (some cheap TFTP clients omit it on the last option
    value)."""
    parts: list[str] = []
    cur = bytearray()
    for b in buf:
        if b == 0:
            parts.append(cur.decode("ascii", errors="replace"))
            cur = bytearray()
        else:
            cur.append(b)
    if cur:
        parts.append(cur.decode("ascii", errors="replace"))
    return parts


def parse_request(pkt: bytes) -> Request:
    op = peek_opcode(pkt)
    if op not in (OP_RRQ, OP_WRQ):
        raise TFTPError(ERR_ILLEGAL_OP, "expected RRQ/WRQ")
    parts = _split_cstrings(pkt[2:])
    if len(parts) < 2:
        raise TFTPError(ERR_ILLEGAL_OP, "malformed RRQ/WRQ")
    filename = parts[0]
    mode = parts[1].lower()
    # Options are key/value pairs appended after filename+mode.
    options: dict[str, str] = {}
    it = iter(parts[2:])
    for key in it:
        try:
            value = next(it)
        except StopIteration:
            break
        if key:
            options[key.lower()] = value
    return Request(op, filename, mode, options)


def parse_data(pkt: bytes) -> Data:
    if len(pkt) < 4:
        raise TFTPError(ERR_ILLEGAL_OP, "short DATA")
    block = struct.unpack("!H", pkt[2:4])[0]
    return Data(block, pkt[4:])


def parse_ack(pkt: bytes) -> Ack:
    if len(pkt) < 4:
        raise TFTPError(ERR_ILLEGAL_OP, "short ACK")
    return Ack(struct.unpack("!H", pkt[2:4])[0])


def parse_error(pkt: bytes) -> Error:
    if len(pkt) < 4:
        raise TFTPError(ERR_ILLEGAL_OP, "short ERROR")
    code = struct.unpack("!H", pkt[2:4])[0]
    msg = pkt[4:].split(b"\x00", 1)[0].decode("ascii", errors="replace")
    return Error(code, msg)


# -- Pack ----------------------------------------------------------------

def pack_data(block: int, payload: bytes) -> bytes:
    return struct.pack("!HH", OP_DATA, block & 0xFFFF) + payload


def pack_ack(block: int) -> bytes:
    return struct.pack("!HH", OP_ACK, block & 0xFFFF)


def pack_error(code: int, message: str) -> bytes:
    return struct.pack("!HH", OP_ERROR, code) + message.encode("ascii", errors="replace") + b"\x00"


def pack_oack(options: dict[str, str]) -> bytes:
    out = bytearray(struct.pack("!H", OP_OACK))
    for k, v in options.items():
        out += k.encode("ascii") + b"\x00" + str(v).encode("ascii") + b"\x00"
    return bytes(out)
