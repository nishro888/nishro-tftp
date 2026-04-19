# Architecture

Nishro TFTP is a two-engine, one-UI system. A single `nishro_tftp.exe`
hosts the admin web UI and, depending on `tftp.engine` in
`config.yaml`, either runs the packet path itself (Python/Scapy) or
spawns `nishro_core.exe` as a child process (C/Npcap).

```
 +------------------+        admin browser (websocket + REST)
 |  Web UI (FastAPI)| <-----------------------------+
 +---------+--------+                               |
           |                                        |
           | in-process                 +-----------+-----------+
           v                            | Python engine         |
    +-------------+                     |  - network/sniffer    |
    | AppState    |                     |  - tftp/server        |
    | (cfg,cache) |------ engine=py --> |  - arp/icmp responder |
    +------+------+                     +-----------------------+
           | engine=c
           v
    +-----------------+   stdin (config JSON lines)
    | CoreEngine (Py) |  -----------------------------> +------------+
    |  subprocess mgr |   stdout (events: session_*,   | c_core/    |
    +-----------------+   stat, log, hello, bye)  <--  | Npcap raw  |
                                                       | L2 engine  |
                                                       +------------+
```

## Repository layout

| Path                    | What's in it                                         |
| ----------------------- | ---------------------------------------------------- |
| `main.py`               | Top-level orchestrator; builds `Nishro`, runs web    |
| `core/config.py`        | YAML config loader with hot-reload subscribers       |
| `core/stats.py`         | Global counters + snapshot for the web UI            |
| `core/carryover.py`     | In-process accumulator that preserves counters       |
|                         | across Python ↔ C engine swaps                       |
| `core/daily_stats.py`   | Per-day session + unique-user persistence            |
| `core/engine.py`        | `CoreEngine` subprocess manager (drives `c_core`)    |
| `core/acl.py`           | VLAN + IP allow/deny rule evaluator                  |
| `core/auth.py`          | PBKDF2 admin auth + session tokens                   |
| `core/users.py`         | Employee-ID → display-name roster                    |
| `core/logger.py`        | Logging setup + in-memory ring for the Logs tab      |
| `core/file_lock.py`     | Concurrent-access serialisation on file reads        |
| `network/`              | Python engine: L2 bridge + ARP/ICMP responders       |
| `tftp/`                 | Python engine: TFTP server + session machine         |
| `files/`                | Source backends (local FS, FTP), cache, locking      |
| `web/app.py`            | FastAPI routes + WebSocket tick builder              |
| `web/static/`           | Single-page UI (HTML / CSS / vanilla JS)             |
| `c_core/`               | Native C engine (Npcap, raw L2, send-queue batching) |
| `tools/`                | One-off developer scripts (logo resizer, etc.)       |
| `config.yaml`           | Shipped default config (also the seed template)      |
| `nishro_tftp.spec`      | PyInstaller spec -- produces `dist/nishro_tftp.exe`  |
| `build_exe.bat`         | One-shot Windows build (C engine + Python exe)       |

## IPC contract (Python <-> C engine)

Both sides speak newline-delimited JSON. No length prefix; each line is
one message.

Python -> C (stdin):
- `{"op":"config","data":<full config dict>}` - full config replacement;
  critical changes cause the parent to spawn a fresh child.
- `{"op":"ping"}` - liveness check.
- `{"op":"stop"}` - graceful shutdown request.

C -> Python (stdout):
- `{"ev":"hello","version":"c-1.0"}` - emitted once at startup.
- `{"ev":"ready","device":"...","virtual_ip":"..."}` - after pcap open.
- `{"ev":"session_start", id, kind, filename, client_mac, client_ip,
   client_port, vlan_id, blksize, windowsize, bytes_transferred,
   total_bytes, state, started_at}`
- `{"ev":"session_progress", id, bytes_transferred, total_bytes, state}`
- `{"ev":"session_end", id, ok, server_fault, bytes_transferred,
   total_bytes, duration_ms, state, error?}` - `state` carries a
   human-readable reason when `ok=false` ("file not found", "client
   timeout", "disk write failed", ...). `server_fault=true` only for
   real server-side failures (disk / commit errors); client dropouts
   and policy rejections set it `false` so they don't inflate the
   `sessions_failed` counter or the daily-chart "failed" bar.
- `{"ev":"stat", ...aggregate counters...}` - once per second.
- `{"ev":"pong"}` / `{"ev":"bye"}`.

The Python side doesn't need to match C counters 1:1 -- it subscribes to
`session_*` events and reconstructs the view the web UI needs.

## Startup lifecycle

1. `main.py:main()` parses CLI, sets up logging, calls `_amain()`.
2. `_amain()` loops: build a `Nishro`, call `nishro.run()`, rebuild
   on critical-config change, exit on plain shutdown.
3. `Nishro.__init__` resolves the NIC. If it fails, it sets `nic_error`
   and builds the Python side in "view only" mode so the web UI can
   still come up (admin fixes the NIC from there).
4. `Nishro.run()` starts the engine (best-effort) and the web UI, then
   `asyncio.wait`s on dispatch / web / restart tasks.

## Stats accounting

Two counter stores feed the dashboard:

- **`core/stats.py` (Python STATS)** -- ticked by the Python engine
  on every packet / session event. In C-engine mode it stays near
  zero (packet work is in a subprocess).
- **C engine's internal counters** -- emitted once per second as an
  `{"ev":"stat", ...}` message on stdout. `CoreEngine` keeps the
  latest snapshot available via `get_stats()`.

`web/app.py::_stats_payload()` composes the final tick:

1. Start from `STATS.snapshot()` (Python counters + uptime).
2. In C-engine mode, overlay the fields from `CoreEngine.get_stats()`
   over the Python ones.
3. Apply the **carryover** accumulator (`core/carryover.py`) -- a
   process-local dict that holds the sum of totals from any
   previously-stopped engine in this process. This keeps the
   user-visible numbers stable across a Python ↔ C engine swap.
4. Recompute derived fields (e.g. `cache_hit_rate`) from the merged
   counts.

When an engine is torn down (`main.py::Nishro.run()` finally clause),
`carryover.add()` folds the outgoing totals in. The Python engine
also calls `STATS.reset_counters()` so the next Python engine starts
from zero -- otherwise the STATS singleton (module-level, survives
runner rebuilds) would double-count. The C engine has no such
concern: each C engine start is a fresh subprocess.

## Daily sessions + unique users

`core/daily_stats.py` persists per-day aggregates to
`daily_sessions.json` next to the config. Every completed or failed
session (from either engine) bumps a bucket keyed by ISO date:
`total`, `completed`, `failed`, `bytes_sent`, `bytes_received`.

Unique users are tracked in a parallel `{date: set[str]}` structure.
The user ID is extracted from FTP-prefixed filenames -- pattern
`f::NNN/` -- via the regex in `_USER_ID_RE`. Sets are serialised as
sorted lists inside each day bucket so the JSON remains
human-readable. Buckets older than ~180 days are pruned on every
`record()` call.

Both engine paths feed this module: `tftp/server.py::_on_complete`
for Python, `core/engine.py::_on_event` (on `session_end`) for C.

## Process / CPU accounting

`web/app.py::_proc_stats()` samples `psutil` for the dashboard:

- `process_cpu` / `process_rss` -- the Python parent.
- `system_cpu` -- whole-machine load (0–100 across all cores).
- `cpu_cores` -- logical core count.

In C-engine mode the parent is idle (all packet work is in the
subprocess), so `_proc_stats()` additionally samples
`psutil.Process(core.proc.pid)` and adds its CPU and RSS on top.
The child psutil handle is cached between ticks and re-primed when
the PID changes (engine restart). Priming returns 0.0 on the very
first call by design -- `psutil.cpu_percent(interval=None)` needs a
prior sample to compute a delta.

## Performance notes (C engine)

- `pcap_sendqueue_transmit` batches a full TFTP window into one kernel
  call. This is the single biggest win over per-block `pcap_sendpacket`.
- 16 MiB kernel ring buffer (`pcap_set_buffer_size`) handles bursts
  without drops.
- `pcap_setmintocopy(0)` + 1 ms read timeout keeps the dispatch loop
  responsive under load.
- UDP checksum is set to 0 on IPv4 (RFC 768 permits this) -- avoids a
  full payload pass per DATA frame.
- Progress events are rate-limited to 200 ms per session; the stdout
  pipe is fully buffered.
