"""Project-wide constants.

Every magic number and default value lives here so there is exactly one
place to tune behaviour. Individual subsystems import from this module
rather than repeating literals inline. Anything reasonably likely to
change (buffer sizes, defaults, limits) goes here.

Conventions
-----------
* ``*_DEFAULT``  - default value used when config omits a field.
* ``*_MIN`` / ``*_MAX`` - hard guardrails enforced by the negotiator.
* Names are ALL_CAPS. Grouped by subsystem.
"""
from __future__ import annotations

# ---------------------------------------------------------------- network
#: Backlog for the asyncio.Queue that bridges scapy's sniffer thread
#: into the event loop. Must be larger than any conceivable burst.
SNIFFER_QUEUE_MAX: int = 20_000

#: IPv4 Time-To-Live used on every outbound frame we craft.
IP_DEFAULT_TTL: int = 64

#: Ethernet / 802.1Q tag bytes.
ETHERTYPE_IPV4: int = 0x0800
ETHERTYPE_DOT1Q: int = 0x8100

#: Don't-Fragment flag + fragment offset combined field (RFC 791).
IP_FLAGS_DF: int = 0x4000

# ---------------------------------------------------------------- TFTP
TFTP_DEFAULT_LISTEN_PORT: int = 69

#: Classic block size per RFC 1350 (before blksize negotiation).
TFTP_CLASSIC_BLKSIZE: int = 512

#: Defaults when the config omits ``tftp.defaults.*``.
TFTP_BLKSIZE_DEFAULT: int = 1468        # MTU 1500 - 14 eth - 20 ip - 8 udp + 10 tftp
TFTP_WINDOWSIZE_DEFAULT: int = 4
TFTP_TIMEOUT_DEFAULT: int = 3           # seconds
TFTP_MAX_RETRIES_DEFAULT: int = 5

#: Hard limits enforced when the client requests out-of-range options.
TFTP_BLKSIZE_MIN: int = 8
TFTP_BLKSIZE_MAX: int = 65_464          # RFC 2348
TFTP_WINDOWSIZE_MIN: int = 1
TFTP_WINDOWSIZE_MAX: int = 64
TFTP_TIMEOUT_MIN: int = 1
TFTP_TIMEOUT_MAX: int = 60

#: TID (ephemeral server port) pool bounds. We avoid 0-9999 to stay
#: clear of well-known services and keep us below 65500 for tooling.
TFTP_TID_MIN: int = 10_000
TFTP_TID_MAX: int = 65_500

#: Per-session inbound packet queue size.
TFTP_SESSION_QUEUE_MAX: int = 256

# ---------------------------------------------------------------- sessions
SESSIONS_MAX_CONCURRENT_DEFAULT: int = 100
SESSIONS_QUEUE_TIMEOUT_DEFAULT: float = 10.0   # seconds
SESSIONS_OVERFLOW_POLL_INTERVAL: float = 0.05  # seconds

# ---------------------------------------------------------------- files
#: Default location for RRQ reads.
LOCAL_ROOT_DEFAULT: str = "./tftp_root"

#: Default location for WRQ uploads.
WRITE_ROOT_DEFAULT: str = "./tftp_uploads"

#: 0 disables the cap.
MAX_FILE_SIZE_DEFAULT: int = 50 * 1024 * 1024   # 50 MiB

# ---------------------------------------------------------------- cache
CACHE_TOTAL_BYTES_DEFAULT: int = 512 * 1024 * 1024     # 512 MiB
CACHE_PER_FILE_BYTES_DEFAULT: int = 100 * 1024 * 1024  # 100 MiB

# ---------------------------------------------------------------- FTP
FTP_DEFAULT_PORT: int = 21
FTP_CONNECT_TIMEOUT: float = 8.0     # seconds
FTP_SOCKET_TIMEOUT: float = 30.0     # seconds
FTP_DOWNLOAD_CHUNK: int = 64 * 1024  # bytes per stream chunk
FTP_DEFAULT_USER: str = "share"
FTP_DEFAULT_PASSWORD: str = "shared"
FTP_DEFAULT_ROOT: str = "/"

#: Prefix routing defaults (``f::NNN/file`` -> ``BDCOMnnnn/file``).
FTP_PREFIX_TRIGGER: str = "f::"
FTP_PREFIX_FOLDER: str = "BDCOM"
FTP_PREFIX_DIGIT_PAD: int = 4

# ---------------------------------------------------------------- logging
LOG_FORMAT_DEFAULT: str = "%(asctime)s %(levelname)-7s %(name)-20s %(message)s"
LOG_FILE_DEFAULT: str = "./logs/nishro_tftp.log"
LOG_ROTATE_BYTES: int = 10 * 1024 * 1024  # 10 MiB
LOG_ROTATE_BACKUPS: int = 5
LOG_MEMORY_BUFFER: int = 2_000             # ring-buffer lines kept in RAM

# ---------------------------------------------------------------- web UI
WEB_HOST_DEFAULT: str = "127.0.0.1"
WEB_PORT_DEFAULT: int = 8011
WEB_STATS_INTERVAL_DEFAULT: float = 1.0    # seconds
WEB_WS_LOG_QUEUE: int = 500
WEB_WS_INIT_LOG_COUNT: int = 200
WEB_WS_LOGS_PER_TICK: int = 200

#: How much of the config reload delay we tolerate between save and
#: rebinding the listen socket. Must be long enough for uvicorn to
#: finish writing the POST response.
RESTART_GRACE_SECONDS: float = 0.3
