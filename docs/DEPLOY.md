# Deployment guide

## Target requirements

- Windows 10 / 11 / Server 2019+ (x64).
- [Npcap](https://npcap.com/) installed with **WinPcap API-compatible
  Mode** enabled. Install before first run.
- An interface with a physical or virtual NIC that Npcap recognises.
- Administrator rights (the exe is UAC-marked; raw L2 I/O needs it).

## Install

1. Copy the `dist\nishro_tftp\` folder from the build machine to a
   persistent path on the target, e.g.
   `C:\Program Files\Nishro TFTP\`.
2. Right-click `nishro_tftp.exe` -> **Run as administrator**. A console
   window opens.
3. Browse to <http://127.0.0.1:8011>. The first run seeds
   `config.yaml` and `auth.json` next to the exe. Auth is **off** by
   default (`web.require_auth: false`), so you can walk straight into
   Admin Config without a login. After setting a password in
   **Admin Config -> Auth**, tick **Require admin login** to lock the
   UI for everyone; there is no per-browser override for this switch.
4. In **Admin Config**:
   - Pick a **Network adapter** from the dropdown.
   - Set the **Virtual IP** to an address NOT currently assigned to
     the NIC in Windows. This is the address clients will TFTP to.
   - Leave **Virtual MAC** blank (auto-uses the adapter's MAC).
   - Save. The engine restarts in-place.

## Running as a service (recommended)

The exe does not install itself as a service. Use NSSM or the
built-in `sc create` once:

```bat
sc create NishroTFTP binPath= "\"C:\Program Files\Nishro TFTP\nishro_tftp.exe\"" start= auto
sc description NishroTFTP "Nishro TFTP (VLAN-aware)"
sc start NishroTFTP
```

Because the exe needs elevation, the service must run as
LocalSystem. Logs are in `logs\` next to the exe.

## Engine selection

`Admin Config -> TFTP -> Engine` toggles between:

- **Python** -- Scapy + L2 send, easy to debug, good up to ~1 MiB/s.
- **C** -- native `nishro_core.exe` using `pcap_sendqueue_transmit`,
  designed for sustained multi-MiB/s throughput.

Switching engines triggers a clean in-process restart. The dashboard's
cumulative counters (bytes, sessions, RRQ/WRQ, errors, ACL denials,
ARP/ICMP, cache hits) are preserved across the swap by an in-memory
carryover; a full process restart is still a fresh session.

## Data at rest

| File                     | Purpose                                         |
| ------------------------ | ----------------------------------------------- |
| `config.yaml`            | All runtime configuration.                      |
| `auth.json`              | PBKDF2 hash of the admin password.              |
| `users.json`             | Employee-ID -> display name mapping.            |
| `daily_sessions.json`    | Per-day session + unique-user aggregates.       |
| `tftp_root\`             | RRQ-served files (read-only path).              |
| `tftp_uploads\`          | WRQ landing area.                               |
| `logs\`                  | Rotating text logs.                             |

`auth.json` and `users.json` are gitignored -- they are secrets.
`daily_sessions.json` is gitignored as runtime state; delete it to
reset the dashboard's historical chart (active sessions are
unaffected).

## Upgrading

Stop the service (or exit the console), overwrite the `dist` folder's
contents with the new build, restart. `config.yaml`, `auth.json`,
`users.json`, and both data folders are preserved -- the bundle only
overwrites binaries and static UI assets.
