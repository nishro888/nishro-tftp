# Changelog

All notable changes to Nishro TFTP are tracked here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/).

## [2.4.0] - 2026-04-19

### Added
- **Dashboard hero row.** Five big stat cards at the top of the
  dashboard using the shared `.stat-block` / `.stat-card-row`
  primitives from the Statistics tab: TFTP engine, Web users today,
  TFTP devices today (by MAC), Bytes sent, Bytes received. Replaces
  the old engine-status beacon that used to live in the nav footer.
- **First-run admin credential setup.** Fresh installs no longer ship
  with seeded credentials. When `require_auth` is first flipped on,
  the next Admin-mode switch shows a "Set initial admin credentials"
  prompt (new `POST /api/auth/setup` endpoint) instead of an empty
  login form. The setup dialog refuses if credentials already exist.
- **Configurable Statistics throughput windows.** The 1-minute and
  12-hour windows on the Statistics page are now admin-settable via
  Admin Config → Web UI (`stats_recent_window_sec`,
  `stats_long_window_hours`). The ring buffers in `state.js` truncate
  or grow in place on change — pushed live to every browser.
- **`config.yaml.example`.** Seed template committed to the repo;
  real `config.yaml` is now gitignored so FTP credentials and
  adapter GUIDs never end up on GitHub.

### Changed
- **Sessions tab blksize / windowsize column now reports for failed
  sessions.** The C engine was emitting `session_start` before the
  request's negotiated options were copied into the session record,
  so early-exit failures (file not found, size-limit rejection,
  writes-disabled, write-denied) showed `0` / `0`. `start_rrq()` and
  `start_wrq()` in `c_core/session.c` now seed `s->blksize`,
  `s->windowsize`, and `s->timeout_sec` up front, overwritten later
  by `negotiate()` on the happy path.
- **Rail unified on the left column at every breakpoint.** Previously
  the "System status" / "Reach today" rail lived on the right margin at
  wide viewports and only shifted left on mid-viewports. It now sits
  directly under the side nav on the left at every size — wide, mid,
  and narrow — so the live pulse chips are always adjacent to the
  navigation column rather than drifting to the opposite side or the
  bottom of the page.
- **Responsive Sessions tables.** Both the Sessions tab and the
  dashboard's Active-sessions table collapse into a card grid below
  1100 px instead of forcing a horizontal scrollbar. Each row becomes
  a bordered card with data-label pills driven by `::before` pseudo
  elements, File and Progress cells span full width, and active rows
  get an accent left border so they stand out in the history stream.
- **File browser typography.** Row content bumped from 11 px → 13 px,
  icons 14 → 16, breadcrumbs 11 → 13. Readable without zooming.
- **Single auth-toggle path.** `require_auth` is now owned exclusively
  by the main Config form (`POST /api/config`). The duplicate
  checkbox in the Web-UI sub-panel and its `POST /api/ui_settings`
  acceptance were removed, eliminating the save-race that
  occasionally surfaced as "error while saving" when flipping auth
  on.

### Fixed
- **"Sent today" / "Received today" not updating.** The dashboard
  hero was reading `dailyData.today.bytes_sent/received` which only
  refreshes on session completion. Now reads `state.stats.bytes_sent`
  / `bytes_received` (live WebSocket-tick counters). Card labels
  tightened to "Bytes sent" / "Bytes received" to match their
  since-boot semantics.
- **Two Cancel buttons in setup / change-password dialogs.** The
  first-run setup section now has its own Cancel beside "Create
  admin"; the top-row `loginCancel` is hidden in setup and
  change-password modes so only one Cancel is visible at a time.

### Removed
- Seeded default `nishro` / `1a1b1c1d` admin credentials in
  `core/auth.py`. Fresh installs start with no credentials; setup
  happens interactively on first auth enablement.
- Engine-status beacon in the nav footer (`#engineBeacon`, the
  `.engine-beacon` / `.engine-halo` / `.engine-orb` CSS and pulse
  keyframes). Engine identity now lives in the hero row's first
  card instead.

## [2.3.0] - 2026-04-19

### Added
- **Vertical side-nav with sliding accent marker.** Tabs moved from a
  horizontal top strip to a sticky left sidebar with icon + label
  buttons, an admin section divider, and a gradient indicator that
  slides between the active tab. Stays pinned on every tab switch.
- **Engine status beacon** pinned at the side-nav footer. Pulsing halo
  around an orb shows engine flavour (Python / C native) and live
  online/offline state on every tab -- replaces the dashboard hero.
- **Three rail-groups in the live-stats column.** The right rail is
  now segmented into *Server pulse* (Uptime, Active sessions,
  Successful sessions, Sent today, Received today), *Reach today*
  (Web visitors today, TFTP devices today by MAC) and *System health*
  (Cache hit, CPU proc, CPU host). All three are sticky and visible
  on every tab.
- **Per-day reach tracking.** `daily_stats` now records a unique set
  of TFTP client MAC addresses and a unique set of web-UI visitor IPs
  per day, exposed via `/api/sessions/daily.reach_counts`. A FastAPI
  middleware records the visitor IP on every non-static HTTP / WS hit.
- **Page-frame layout.** New 3-column grid (`nav | main | rail`) with
  both rails sticky on wide viewports and graceful stacking on
  mid/narrow viewports. Content column is noticeably wider than
  before on desktops since the rails no longer steal width from
  `<main>`.

### Changed
- **Statistics → Traffic** trimmed to five focused cards: Total bytes
  sent, Total bytes received, Total sessions, Active, Completed.
- **Failed-session semantics.** The `tftp_sessions_failed` counter and
  the daily-chart "failed" bar now only advance on genuine server
  faults (disk-write failures, FTP upload failures, internal engine
  crashes). Client-side dropouts -- timeouts, aborts, file-not-found,
  access-denied, disk-full, policy rejection -- end the session but do
  not count as server failures. Both engines carry a new
  `server_fault` boolean on session-end events and `daily_stats`.
- **Session history state column** now shows the actual ended reason
  ("file not found", "client timeout", "disk write failed", ...) with
  colour coding (amber for client drops, red for server faults, green
  for done) instead of a generic "failed" chip.
- **Auth off by default.** Fresh installs ship `web.require_auth:
  false` so the first admin walks straight into Admin Config without
  a seeded password juggle. The toggle is server-side only (central in
  `config.yaml`); flipping it to `true` forces every browser to log in
  with the shared admin credential -- there is no per-browser override.

### Removed
- Dashboard hero panel (`#dashHero`, `.dash-hero`, `.hero-*`). Engine
  identity moved to the side-nav beacon; the four hero metric chips
  collapsed into the *Server pulse* rail-group.
- Dashboard-scoped `.dash-layout` / `.dash-rail-left` /
  `.dash-rail-right` structure. Rails are now page-level.
- "Users today / Users total" rail chips, replaced by the new
  *Reach today* group.
<!--  -->- The old "error sent" framing in the daily-sessions summary. Errors
  are still counted in the statistics tab but no longer drive the
  daily "failed" bar or the per-day failure totals.

## [2.2.0] - 2026-04-18

### Added
- **Redesigned dashboard.** Wide-viewport layout escapes the main column
  and pins twelve metric chips into left/right rails (Engine, Uptime,
  Sessions, Cache hit, CPU proc, CPU host on the left; Sent, Received,
  Users today, Users total, Errors, ACL denied on the right). Hero panel
  surfaces Active sessions, Users today, Sent, Received with a soft
  animated halo. Narrow viewports reflow to hero → daily → active →
  rails so the most important numbers stay above the fold.
- **Combined daily-session tiles.** "Successful / Errors" and
  "Sent / Received" now render side-by-side in a single split tile with
  a vertical divider and ratio bar instead of two stacked cards.
- **Unique-user counters.** A per-day set of unique employee IDs is
  derived from FTP-prefixed filenames (`f::NNN/...`) and persisted into
  `daily_sessions.json`. The dashboard surfaces "Users today" and
  "Users total" (across all retained days). Backwards compatible with
  pre-1.2 daily-stats files.
- **Counter persistence across engine swaps.** A new
  `core/carryover.py` module folds the outgoing engine's cumulative
  counters (`bytes_sent`, `bytes_received`, session totals, RRQ/WRQ,
  errors, ACL denials, ARP/ICMP, cache hits/misses) into a process-
  local accumulator before the engine is torn down. The web layer adds
  this carryover on top of the next engine's live counts, so swapping
  Python ↔ C no longer makes the dashboard appear to reset.

### Changed
- Light theme repainted as a warm-paper palette
  (`--bg: #f5f4ef`, `--panel-alt: #eef0ec`, `--border: #d5d9d0`) with
  ambient layered radial gradients fixed to the viewport. Reduces eye
  strain in bright environments while staying clearly readable.
- `_proc_stats()` (`web/app.py`) now accepts an optional child PID and
  samples the C engine subprocess with `psutil.Process(child_pid)`. CPU
  and RSS for the child are added on top of the Python parent so the
  "CPU proc" / process RSS values reflect actual packet-path work in
  C-engine mode (previously stuck at 0% because the parent is idle).
- WebSocket payloads now omit the `sessions` array on ticks where the
  active-session list is unchanged and empty, cutting per-tick payload
  size for idle dashboards. Client-side rendering moved to a
  `requestAnimationFrame`-batched publisher and the in-memory log ring
  is trimmed in place to avoid per-tick allocations.

### Fixed
- "CPU proc" indicator showing 0% whenever the C engine was running.
  Now sums Python parent CPU + C subprocess CPU.
- Dashboard counters (bytes, sessions, RRQ/WRQ, ARP/ICMP, ACL denied)
  no longer reset to zero when the user toggles `tftp.engine` between
  `python` and `c`.

## [2.1.0] - 2026-04-18

### Added
- **Light / dark theme toggle.** Per-browser preference (localStorage
  `nishro.theme`), initialized from `prefers-color-scheme` on first visit.
  Header hosts a realistic moon/sun orb switch: stars twinkle on dark, sun
  rays radiate on light, the orb glides between positions with a spring
  ease. An inline head script applies the theme before the stylesheet
  paints so there is no flash of wrong theme. Non-central by design --
  one user's choice does not affect other viewers.
- **Daily session counter + chart.** Aggregate totals (completed /
  failed / bytes) per day are persisted to `daily_sessions.json` next
  to the config. The dashboard renders a stacked bar chart with a
  selectable window (7–90 days) and a today/window summary. Six months
  of history retained.
- **Central view-mode tab visibility.** Moved the "View-mode tab
  visibility" setting from per-browser `localStorage` to
  `config.yaml → web.visible_tabs`. Toggling a tab in Admin Config
  pushes the change live to every connected browser via WebSocket.
- New public endpoint `GET /api/ui_settings` and admin endpoint
  `POST /api/ui_settings` to drive central UI controls.

### Changed
- `POST /api/config` now refetches the server's current config before
  writing so out-of-band saves (ACL editor, UI-settings editor) are
  never clobbered by a stale form.
- `require_auth: false` short-circuits the login overlay; viewers are
  granted admin mode automatically. Documented as "trusted LAN" mode.

### Fixed
- ARP request counter on the C engine now only counts requests
  targeting the configured Virtual IP (was incrementing for every ARP
  on the wire in promiscuous mode). Same fix applied to ICMP echo
  requests.
- `process_cpu` / `system_cpu` dashboard values always showed 0 until
  the psutil sampler's second call. The sampler is now primed on first
  use so real values appear from tick 2 onward.

## [2.0.0] - 2026-04-14

First public release.

### Added
- Native C TFTP engine (`c_core/`) as an admin-toggleable alternative to
  the Python/Scapy engine. Uses Npcap's `pcap_sendqueue_transmit` to
  batch an entire window in one kernel transition; typical sustained
  throughput exceeds 1 MiB/s on a single session.
- **Prebuilt-next-window pipeline** in the C engine: while waiting for
  the current window's ACK, the next window's packets are read from
  disk, built, and staged in a second `pcap_sendqueue`. When the ACK
  arrives, the fast path is a single `pcap_sendqueue_transmit` with no
  disk I/O or packet construction in between.
- Windowed TFTP (RFC 7440) with windowsize up to 64.
- Web UI engine indicator on the dashboard.
- File browser **Upload** button on the RRQ-root panel (multi-file).
- Dashboard **degraded-start banner** when the configured NIC fails to
  open -- server + web UI still come up so the admin can pick another
  adapter.
- Adapter dropdown shows Windows-Settings friendly names, link state,
  and link speed, sourced from the authoritative Npcap device list.
- Session table columns: Duration, client MAC, client port, VLAN ID,
  blksize, windowsize, live progress + speed.

### Changed
- Stdout IPC on the C engine is fully-buffered; progress events are
  rate-limited to 200 ms per session to keep the send path hot.
- Adapter enumeration now iterates the Npcap list first (one row = one
  openable device) and uses psutil only to annotate friendly names.
  Fixes a regression where adapters with colliding MACs could map to
  the wrong NPF path.

### Fixed
- Stale `virtual_mac` after NIC swap -- the UI now warns loudly when
  the configured Virtual MAC doesn't belong to the selected adapter,
  which would otherwise make every reply be dropped by the switch.
- FILETIME epoch conversion in `c_core/util.c` (was returning ms since
  1601 instead of 1970, breaking all speed/duration calculations).
- C-engine config dispatch: the `{op:"config","data":...}` envelope is
  drilled into before parsing, so `network`/`tftp` keys resolve.
- UDP checksum set to 0 on IPv4 (RFC 768) -- saves a payload scan on
  every DATA send.

## [1.0.0] - initial foundation

The pre-release work that the project grew out of. No tagged build --
captured here so the history starts somewhere.

### Added
- **Single-process Python architecture.** `main.py` orchestrates the
  `Nishro` runner, which wires together the L2 sniffer, ARP / ICMP /
  TFTP responders, file source, cache, and FastAPI web UI on a single
  asyncio event loop. A restart loop in `_amain()` rebuilds the runner
  cleanly when the config changes require it.
- **Raw Layer-2 Python engine.** `network/sniffer.py` opens the NIC
  via Npcap in promiscuous mode, parses 802.1Q VLAN tags in
  `network/packet_utils.py`, and hands frames to ARP / ICMP / TFTP
  responders. All replies are emitted on the raw L2 path so VLAN tags
  survive unchanged -- no reliance on the Windows TCP/IP stack.
- **TFTP server + session state machine** (`tftp/server.py`,
  `tftp/session.py`, `tftp/protocol.py`). RFC 1350 base protocol plus
  RFC 2347/2348/2349 option negotiation (`blksize`, `timeout`,
  `tsize`) and RFC 7440 windowed transfers (`windowsize`). Per-option
  negotiation policies live in `config.yaml`. Concurrent-session cap
  with reject / queue overflow policies.
- **Dual file sources** (`files/source.py`): a local filesystem root
  for RRQ and an optional FTP backend, with a read-through
  `FileCache` to keep repeated reads off the disk. `FileLockManager`
  serialises concurrent access to the same file. FTP prefix routing
  (`f::NNN/path`) strips a per-user prefix and pulls from the FTP
  backend on demand.
- **YAML config with hot reload** (`core/config.py`). Subscriber
  callbacks fire on save so subsystems can refresh without a process
  restart. Schema-driven config editor is built into the web UI;
  critical changes (engine swap, NIC swap) promote to a clean runner
  rebuild via the restart loop.
- **ACL engine** (`core/acl.py`). VLAN-list and IP-list allow/deny
  rules evaluated per-service (ARP / ICMP / TFTP). First-match wins;
  both lists have independent enable toggles.
- **FastAPI web UI** (`web/app.py` + `web/static/`). Single-page app
  with a REST API (`/api/stats`, `/api/config`, `/api/sessions`,
  `/api/files`, `/api/logs`) and a WebSocket (`/ws`) that streams
  stats, session progress, and log lines. Frontend tabs: Dashboard,
  Sessions, Files, Logs, Admin Config. Multi-file upload into the
  RRQ root from the file browser panel.
- **Admin authentication** (`core/auth.py`). PBKDF2-SHA256 password
  hashing, sliding-window session tokens in httpOnly cookies,
  single-admin enforcement. Can be disabled (`web.require_auth:
  false`) for trusted LANs.
- **Employee-ID roster** (`core/users.py`). Maps the numeric employee
  ID that clients embed in `f::NNN/` filenames to a display name so
  the sessions tab reads as people, not integers.
- **In-memory log ring** (`core/logger.py`). Configurable rotating
  file handler, plus a bounded in-memory handler that the WebSocket
  tails for the live Logs tab.
- **Stats collector** (`core/stats.py`). Thread-safe global counters
  (ARP / ICMP / TFTP / bytes / sessions / cache / ACL denials) with
  uptime and a one-shot snapshot method.
- **Windows-first packaging** (`build_exe.bat`, `nishro_tftp.spec`).
  PyInstaller one-folder bundle with a UAC manifest so Administrator
  elevation is requested on launch (required for raw L2 I/O).
- **Developer tooling** (`tools/resize_logo.py`) to regenerate the
  shipped logo and favicon from a source image.
