# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the TCG backend one-file sidecar.

Build from the REPO ROOT so ``tcg`` is importable:
    pyinstaller --noconfirm --clean desktop/sidecar/tcg-backend.spec

Bundling strategy
-----------------
The backend pulls in heavy native packages (numpy, scipy via py_vollib,
psycopg's libpq binary) plus packages that import submodules dynamically
(uvicorn picks its event loop / HTTP / websocket implementation by string at
runtime; pandas_market_calendars ships calendar data files). For each of these
we use ``collect_all`` (data + dynamic libs + every submodule) rather than
hand-listing modules, then add the handful of uvicorn runtime hidden imports
that even collect_all can miss because they are referenced only as strings.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# Collect every tcg submodule so all routers/services wired in create_app()
# (and anything they import lazily) are present in the frozen binary.
hiddenimports += collect_submodules("tcg")

# Packages collected wholesale: native extensions + dynamic-import packages.
# scipy is pulled transitively by py_vollib (Black-Scholes / implied vol).
for _pkg in (
    # numpy / scipy / pandas are intentionally NOT collected wholesale:
    # PyInstaller's built-in hooks bundle their binaries + data correctly,
    # whereas collect_all drags in thousands of *.tests.* modules (massive,
    # slow, useless at runtime). Normal import analysis + the hooks cover what
    # py_vollib / pandas_market_calendars actually use.
    "psycopg",
    "psycopg_binary",
    "psycopg_pool",
    "py_vollib",
    "py_lets_be_rational",
    "uvicorn",
    "pandas_market_calendars",
):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# uvicorn selects these by string at runtime ("auto" -> concrete impl). They
# are listed explicitly so the bootloader includes them even if collect_all on
# uvicorn misses a lazily-referenced one.
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    # Only the impls that are actually installed: wsproto is absent, and the app
    # exposes no WS endpoints, but "auto" still imports websockets_impl.
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # The whole tcg package is imported by the entry script; collect every
    # submodule so the routers/services wired in create_app() are present.
    "tcg",
    # psutil powers the sidecar's parent-death watchdog (tcg_backend.py) so a
    # killed bootloader never orphans this uvicorn child holding the port. It
    # ships a compiled extension, so name it explicitly to guarantee bundling.
    "psutil",
]


a = Analysis(
    [os.path.join(SPECPATH, "tcg_backend.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tcg-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console window: on Windows a console=True sidecar pops a black terminal
    # alongside the Tauri window. lib.rs forwards the sidecar's stdout/stderr to
    # the app log (CommandEvent::Stdout/Stderr), so diagnostics are preserved.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
