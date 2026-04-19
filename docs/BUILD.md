# Build guide

Nishro TFTP is Windows-only and has two artifacts:

1. `c_core/bin/nishro_core.exe` -- the native TFTP engine.
2. `dist/nishro_tftp.exe` -- the PyInstaller one-folder bundle that
   wraps the web UI, the Python engine, and the C engine binary.

`build_exe.bat` at the repo root builds both in the right order.

## Prerequisites

- **Python 3.11 or 3.13** on `PATH` (or at `C:\Program Files\Python313`).
- **Npcap SDK** -- headers + import libraries.
  Download: <https://npcap.com/#download>. Default install path is
  `C:\Npcap-SDK`; override with `NPCAP_SDK=...` when invoking make.
- **MinGW-w64 / MSYS2** for `gcc` + `mingw32-make`. Any recent toolchain
  (gcc 13+) works. Tested on MSYS2 gcc 15.2.0.
- **Npcap runtime** on every machine that will *run* the server
  (kernel driver, cannot be bundled). Download the installer from
  npcap.com and install with "WinPcap API-compatible Mode" checked.

Python deps:

```bat
python -m pip install -r requirements.txt
python -m pip install pyinstaller
```

## One-shot build

```bat
build_exe.bat
```

Produces:
- `c_core\bin\nishro_core.exe`
- `dist\nishro_tftp\nishro_tftp.exe` (+ support files)

## Step-by-step (developer loop)

Build just the C engine (fast iteration):

```bat
cd c_core
mingw32-make
```

Run from source (no PyInstaller) -- useful for UI / Python work:

```bat
python main.py --config config.yaml
```

The UI is at <http://127.0.0.1:8011> by default. Admin must Run as
administrator because raw L2 sockets and pcap need elevation.

Rebuild the bundled exe only:

```bat
python -m PyInstaller nishro_tftp.spec --clean --noconfirm
```

## Smoke test after build

1. Run `dist\nishro_tftp\nishro_tftp.exe` as administrator.
2. Browse to <http://127.0.0.1:8011>. Auth is off by default on a
   fresh install, so you go straight into Admin Config.
3. Admin Config -> Network adapter: confirm the dropdown lists your
   real adapters with their Windows friendly names.
4. Start a TFTP client and request a file from `tftp_root/`. The
   Sessions tab should show the transfer live; the Dashboard cards
   should tick up.

If the dashboard shows the red banner
"TFTP engine not running", follow the link to Admin Config and pick a
valid adapter. Clear `Virtual MAC` (leave blank) so the engine
auto-uses the adapter's real MAC.
