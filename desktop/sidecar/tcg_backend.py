"""PyInstaller entry point for the TCG backend "sidecar".

This is the frozen-binary counterpart of ``python -m tcg.core`` (see
``tcg/core/__main__.py``). The Tauri desktop wrapper spawns the bundled
one-file binary built from this script and waits on ``/health``.

Why a dedicated entry instead of freezing ``tcg.core.__main__``?
---------------------------------------------------------------
PyInstaller freezes a *script*, not a ``python -m pkg`` invocation. We also
pass the **app object** to ``uvicorn.run`` rather than the import string
``"tcg.core.app:app"``: in a frozen build the string form forces uvicorn to
re-import the module by name at runtime, which is fragile inside the
PyInstaller bootloader. Importing ``app`` here means the bundler sees the
dependency statically and uvicorn gets a ready object. ``reload`` is off in
both paths, so single-process ``run(app, ...)`` is behaviour-identical to
the dev path.

Secrets: the FastAPI app loads its DWH_*/APP_DB_* config via python-dotenv
from the process working directory (the repo checkout's gitignored ``.env``),
exactly as the web path does. Nothing is bundled into the binary — launch the
sidecar from a directory where ``.env`` is resolvable.
"""

from __future__ import annotations

import argparse


def main() -> None:
    import os
    import sys

    # PyInstaller's WINDOWED build (console=False, which the Tauri sidecar uses)
    # leaves ``sys.stdout`` / ``sys.stderr`` set to ``None`` on Windows. uvicorn
    # configures logging at import/startup and calls ``sys.stdout.isatty()``,
    # which then raises ``AttributeError: 'NoneType' object has no attribute
    # 'isatty'`` -> "Unable to configure formatter 'default'" -> the sidecar
    # crashes before it ever binds the port (surfacing as "Backend unreachable").
    # Reattach the std streams to the inherited OS pipes (fd 1/2 — Tauri captures
    # these into backend.log), falling back to devnull, BEFORE importing or
    # running uvicorn. This runs before any uvicorn import below. On Linux/macOS
    # the streams are not ``None`` (console builds keep real streams), so the
    # guard is a no-op there.
    for _fd, _name in ((1, "stdout"), (2, "stderr")):
        if getattr(sys, _name, None) is None:
            try:
                setattr(sys, _name, os.fdopen(_fd, "w", buffering=1))
            except OSError:
                setattr(sys, _name, open(os.devnull, "w"))

    parser = argparse.ArgumentParser(prog="tcg-backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    # In a frozen (PyInstaller) build, tcg's config loaders derive their
    # ``.env`` path from ``__file__`` -> which points INSIDE the unpacked
    # bundle, not the repo, so ``dotenv_values(_ENV_PATH)`` finds nothing.
    # Those loaders consult ``os.environ`` FIRST (see
    # tcg/data/_sql/connection.py ``_get``), so load the real ``.env`` into the
    # environment here, before importing the app. Path is overridable via
    # ``TCG_ENV_FILE`` (Tauri sets this); otherwise the ``.env`` in the working
    # directory (the repo checkout). ``override=False`` keeps any creds Tauri
    # already injected into the spawned process environment.
    from dotenv import load_dotenv

    load_dotenv(os.environ.get("TCG_ENV_FILE") or os.path.join(os.getcwd(), ".env"))

    import asyncio

    import uvicorn

    # Import the app object directly (not the "module:attr" string form) so the
    # frozen binary resolves it without a runtime re-import.
    from tcg.core.app import app

    # Run uvicorn on an explicit SelectorEventLoop. uvicorn 0.44's loop_factory
    # returns Windows' ProactorEventLoop for a single-process server, which
    # psycopg's async driver refuses ("Psycopg cannot use the
    # 'ProactorEventLoop'") -> the dwh/app pools fail and the backend exits.
    # uvicorn passes that factory straight to asyncio, so setting the global
    # event-loop POLICY does NOT override it; instead bypass uvicorn.run()'s loop
    # management and drive Server.serve() on a loop we create. SelectorEventLoop
    # is cross-platform (Linux/macOS already default to it) -> one code path
    # everywhere. reload/workers are off, so single-process serve() is
    # behaviour-identical to the previous uvicorn.run().
    config = uvicorn.Config(
        app, host=args.host, port=args.port, log_level=args.log_level
    )
    server = uvicorn.Server(config)
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


if __name__ == "__main__":
    main()
