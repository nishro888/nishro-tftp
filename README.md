# Nishro TFTP

**VLAN-aware TFTP server with a dual Python/C engine — BDCOM Dhaka R&D Center**

A high-performance, raw Layer-2 TFTP server for Windows. Ships a Scapy/Npcap
Python engine for debuggability and a native C/Npcap engine using
`pcap_sendqueue_transmit` for sustained multi-MiB/s throughput. Both engines
are driven by the same FastAPI web UI and share a single `config.yaml`.

## Highlights

- **Dual engine, one UI.** Toggle between Python (Scapy) and native C
  (`c_core/nishro_core.exe`) from Admin Config. The C engine batches a full
  TFTP window into a single kernel transition via Npcap's send-queue API.
- **Raw Layer-2 I/O.** Both engines bypass the Windows TCP/IP stack so
  802.1Q VLAN tags survive the round trip unchanged.
- **RFC-complete TFTP.** RFC 1350 + RFC 2347/2348/2349 options (`blksize`,
  `windowsize`, `timeout`, `tsize`) + RFC 7440 windowed transfers, with
  per-option negotiation policies.
- **Degraded-start web UI.** If the configured NIC is missing, unreachable,
  or wears a stale Virtual MAC, the server still boots — the dashboard
  surfaces a red banner linking straight to Admin Config.
- **Accurate adapter dropdown.** Adapters are enumerated from Npcap's own
  list and enriched with psutil friendly names, so the dropdown matches
  Windows Settings rather than showing 40+ virtual interfaces.
- **ACL engine.** VLAN and IP allow/deny lists with per-service granularity
  (ARP, ICMP, TFTP).
- **Live web dashboard.** Session list, throughput sparklines, log viewer,
  multi-file upload into the RRQ root, FTP prefix routing (`f::NNN/file`),
<<<<<<< HEAD
  schema-driven config editor. A two-column layout pins an icon-driven
  vertical side nav on the left with two live-pulse rail groups tucked
  directly beneath it — *System status* (uptime, CPU proc, CPU host,
  cache hit) and *Reach today* (active + successful sessions) — while
  `<main>` owns the full right column. A five-card hero on top of the
  dashboard (TFTP engine, Web users today, TFTP devices today, Bytes
  sent, Bytes received) stays visible across every tab switch. The
  Sessions tables collapse into a card grid below 1100 px so nothing
  ever slides out of view; at <800 px everything stacks in a single
  scrollable column with nav → rail → main ordering.
=======
  schema-driven config editor. A three-column layout pins an icon-driven
  vertical side nav on the left, a five-card hero on top of the dashboard
  (TFTP engine, Web users today, TFTP devices today, Bytes sent, Bytes
  received), and two rail groups on the right — *System status* (uptime,
  CPU proc, CPU host, cache hit) and *Reach today* (active + successful
  sessions). All of it stays visible across every tab switch. On narrow
  viewports the right rail tucks under the nav on the left instead of
  falling to the bottom of the page.
>>>>>>> 28e888cdc8c82e068c0d6cbf0b677dcbd921c6ac
- **Daily sessions chart.** Per-day totals (completed / failed / bytes
  / unique users) persisted to `daily_sessions.json` and rendered as a
  stacked bar chart on the dashboard, with side-by-side
  outcomes/transferred tiles and a today-vs-window summary. Six months
  of history retained. Unique users are derived from FTP-prefixed
  filenames (`f::NNN/...`).
- **Engine-swap counter persistence.** Toggling between the Python
  and C engines preserves the dashboard's cumulative counters
  (bytes sent/received, session totals, RRQ/WRQ, errors, ACL denials,
  ARP/ICMP, cache hits) so the numbers don't appear to reset.
- **Centralized view-mode controls.** Non-admin browsers see exactly the
  tabs the admin has enabled. Toggling a tab in Admin Config pushes the
  change live to every connected browser via WebSocket — no refresh
  required, no per-browser localStorage.
- **Admin auth (optional, off by default).** PBKDF2-SHA256 password
  hashing, sliding-window session tokens, single-admin enforcement.
  Fresh installs ship with no seeded credentials at all: `require_auth`
  is off, and the first time an admin flips it on the next Admin-mode
  switch prompts for an initial username + password. The toggle lives
  in the main Config form only (one save path — no race), and turning
  it on forces every browser to log in with the shared admin credential.
  Employee-ID → name mapping for friendly session display.

## Architecture

```
 +------------------+        admin browser (REST + WebSocket)
 |  Web UI (FastAPI)| <-----------------------------+
 +---------+--------+                               |
           |                                        |
           | in-process                 +-----------+-----------+
           v                            | Python engine         |
    +-------------+                     |  scapy + Npcap L2     |
    | AppState    |------ engine=py --> |  ARP/ICMP/TFTP/WRQ    |
    | (cfg,cache) |                     +-----------------------+
    +------+------+
           | engine=c
           v
    +-----------------+   stdin: {"op":"config"|"ping"|"stop"}
    | CoreEngine (Py) |  --------------------------------> +------------+
    | subprocess mgr  |   stdout: session_* / stat / log   | c_core/    |
    +-----------------+   <-------------------------------- | Npcap raw  |
                                                            | L2 engine  |
                                                            +------------+
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full IPC contract
and startup lifecycle.

## Repository layout

| Path            | Contents                                                 |
| --------------- | -------------------------------------------------------- |
| `main.py`       | Orchestrator — builds `Nishro`, runs web + engine        |
| `core/`         | Config, auth, users, stats, ACL, engine manager,         |
|                 | daily-stats, cross-engine carryover, logger              |
| `network/`      | Python engine: L2 bridge, ARP/ICMP responders            |
| `tftp/`         | Python engine: TFTP server + session state machine       |
| `files/`        | Source backends (local, FTP), cache, file locking        |
| `web/`          | FastAPI app + static single-page UI                      |
| `c_core/`       | Native C engine (Npcap, raw L2, send-queue batching)     |
| `tools/`        | One-off developer scripts                                |
| `docs/`         | Architecture, build, deploy, troubleshooting             |
| `build_exe.bat` | One-shot Windows build (C engine + PyInstaller)          |

## Requirements

- Windows 10 / 11 / Server 2019+ (x64).
- [Npcap](https://npcap.com/) installed with **WinPcap API-compatible Mode**.
- Python 3.11 or 3.13 to run from source.
- Administrator privileges (raw L2 I/O requires elevation).

## Quick start — run from source

```bat
copy config.yaml.example config.yaml
pip install -r requirements.txt
python main.py --config config.yaml
```

Browse to the URL logged at startup (default <http://127.0.0.1:8011>).
Auth is **off** by default so you can go straight to Admin Config with no
password. When you are ready, flip **Require admin login** on in the main
Config form — the next switch to Admin mode will prompt you to create
the initial username + password (there are no default credentials).

In **Admin Config**:
1. Pick a **Network adapter** (the dropdown matches Windows Settings).
2. Set a **Virtual IP** — an address NOT currently assigned to the NIC in
   Windows. Clients TFTP to this address.
3. Leave **Virtual MAC** blank (it auto-uses the adapter's real MAC).
4. Under **TFTP → Engine**, pick `python` for debuggability or `c` for
   throughput. Switching engines triggers a clean in-place restart.

## Quick start — prebuilt Windows exe

```bat
build_exe.bat
```

Produces `dist\nishro_tftp\nishro_tftp.exe`. Copy the whole folder to the
target, install Npcap, then right-click → **Run as administrator**. The
executable's manifest requests elevation automatically.

Full build prerequisites (Npcap SDK, MinGW-w64) are documented in
[docs/BUILD.md](docs/BUILD.md). Deployment and service install
instructions are in [docs/DEPLOY.md](docs/DEPLOY.md).

## Performance

The C engine is tuned for sustained multi-MiB/s on commodity hardware:

- `pcap_sendqueue_transmit` batches an entire TFTP window (up to
  `windowsize` DATA frames) into **one** kernel transition.
- **Prebuilt next window**: while the current window waits for its ACK,
  the C engine reads and builds the following window into a staging
  send-queue. The ACK hot path is then a single kernel call — no disk
  I/O, no packet construction between ACK arrival and wire transmit.
- 16 MiB pcap kernel ring buffer absorbs bursts without drops.
- `pcap_setmintocopy(0)` + 1 ms read timeout keeps the RX loop responsive.
- UDP checksum = 0 on IPv4 (RFC 768 permits this) skips a full-payload pass
  per DATA frame.
- Progress events are rate-limited to 200 ms per session; stdout is fully
  buffered to avoid per-event console flushes.

Effective throughput depends heavily on the client's `windowsize`. A
legacy client negotiating `windowsize=1` is RTT-bound (~2 ms per block on a
typical LAN ≈ 256 KiB/s at `blksize=512`). Clients supporting RFC 7440
with `windowsize >= 16` routinely exceed 5 MiB/s.

## Runtime files

Placed in the same directory as the executable (or the repo root when
run from source):

| File                   | Purpose                                             |
| ---------------------- | --------------------------------------------------- |
| `config.yaml`          | Single source of truth; hot-reloaded on save (gitignored — seed from `config.yaml.example`) |
| `auth.json`            | PBKDF2 admin credentials; created on first-run setup (gitignored) |
| `users.json`           | Employee-ID → name mapping (gitignored)             |
| `daily_sessions.json`  | Per-day session aggregates for the dashboard chart (gitignored) |
| `logs/nishro_tftp.log` | Rotating log file                                   |
| `tftp_root/`           | RRQ source folder served to clients                 |
| `tftp_uploads/`        | WRQ destination folder for incoming uploads         |

Delete any of them to reset that subsystem; the server re-creates them
on next boot.

## Custom branding

```bat
python tools/resize_logo.py path\to\your_logo.png
```

Generates `web/static/img/logo.png` and `web/static/img/favicon.ico`.

## Troubleshooting

Common failure modes — degraded banner, TFTP timeouts, stale Virtual MAC,
"pcap open failed" — are covered in
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Project metadata

- **Version**: see [CHANGELOG.md](CHANGELOG.md).
- **License**: MIT — see [LICENSE](LICENSE).
- **Lead developer**: Md. Nishad Shahriair Roni.
- **AI collaborator**: Anthropic Claude (Opus 4.6).
