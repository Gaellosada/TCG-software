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
    import os

    from dotenv import load_dotenv

    load_dotenv(os.environ.get("TCG_ENV_FILE") or os.path.join(os.getcwd(), ".env"))

    import uvicorn

    # Import the app object directly (not the "module:attr" string form) so the
    # frozen binary resolves it without a runtime re-import. Matches the web
    # path's single-process, no-reload uvicorn.run().
    from tcg.core.app import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
