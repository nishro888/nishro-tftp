# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Nishro TFTP.

Build:  pyinstaller nishro_tftp.spec --clean --noconfirm

Produces a single-file ``dist/nishro_tftp.exe``. Copy it to the target
machine; on first run it will seed config.yaml next to itself. Npcap
must be installed separately on the target machine -- it is a kernel
driver and cannot be bundled.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

# Scapy has heavy dynamic imports across layers -- collect everything
# so packet dissection/building works at runtime.
scapy_datas, scapy_binaries, scapy_hidden = collect_all("scapy")

# Uvicorn / FastAPI / aioftp / websockets pull submodules dynamically too.
hidden = []
hidden += scapy_hidden
hidden += collect_submodules("uvicorn")
hidden += collect_submodules("uvicorn.loops")
hidden += collect_submodules("uvicorn.protocols")
hidden += collect_submodules("uvicorn.lifespan")
hidden += collect_submodules("websockets")
hidden += collect_submodules("aioftp")
hidden += collect_submodules("aiofiles")
hidden += [
    "email.mime.multipart",
    "email.mime.text",
]

datas = []
datas += scapy_datas
# Web UI static assets -- FastAPI mounts these at /static.
datas += [("web/static", "web/static")]
# Ship a starter config.yaml inside the bundle so first-run users have
# a template to copy. At runtime we look for config.yaml next to the
# exe first; the bundled copy is only used as a seed.
datas += [("config.yaml", ".")]
# Bundle the C TFTP engine so the "c" engine toggle works out of the box.
import os as _os
if _os.path.exists("c_core/bin/nishro_core.exe"):
    datas += [("c_core/bin/nishro_core.exe", ".")]

# Package metadata -- several deps read their own version via
# importlib.metadata.version(...) at import time. Without the metadata
# bundled we get PackageNotFoundError inside the frozen app.
for _pkg in (
    "aioftp",
    "aiofiles",
    "fastapi",
    "starlette",
    "uvicorn",
    "pyyaml",
    "scapy",
    "psutil",
    "websockets",
    "anyio",
    "sniffio",
    "click",
    "h11",
):
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=scapy_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "notebook",
        "IPython",
        "pytest",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="nishro_tftp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,  # require elevation -- raw L2 sockets need it
    icon="web/static/img/favicon.ico" if __import__("os").path.exists("web/static/img/favicon.ico") else None,
)
