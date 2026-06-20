"""PyInstaller entry point for the TCG backend "sidecar".

This is the frozen-binary counterpart of ``python -m tcg.core`` (see
``tcg/core/__main__.py``). The Tauri desktop wrapper spawns the bundled
one-file binary built from this script and waits on ``/health``.

Why a dedicated entry instead of freezing ``tcg.core.__main__``?
---------------------------------------------------------------
PyInstaller freezes a *script*, not a ``python -m pkg`` invocation. We import
the FastAPI ``app`` object directly (not the ``"tcg.core.app:app"`` string) so
the bundler sees the dependency statically and uvicorn gets a ready object —
the string form would force a fragile by-name re-import inside the PyInstaller
bootloader.

Serving: we deliberately do NOT call ``uvicorn.run()``. uvicorn 0.44's loop
factory returns Windows' ProactorEventLoop for a single-process server (which
psycopg's async driver rejects), and it hands that factory straight to asyncio,
bypassing the global event-loop *policy*. So we build ``uvicorn.Config`` /
``Server`` ourselves and drive ``Server.serve()`` on an explicit
``asyncio.SelectorEventLoop`` — one cross-platform code path (Linux/macOS
already default to Selector). ``reload``/``workers`` are off, so this is
behaviour-identical to the previous single-process ``run()``.

Config / secrets: tcg's loaders derive their ``.env`` path from ``__file__``,
which in a frozen build points *inside* the unpacked bundle, so they find
nothing on their own. They consult ``os.environ`` first, so this entry loads
the real ``.env`` into the environment — path from ``TCG_ENV_FILE`` (Tauri sets
it) or ``<cwd>/.env`` — before importing the app, with ``override=False`` so
any credentials already injected into the spawned process environment win.
Nothing is baked into the binary.

Lifecycle: in a one-file build this script runs in a CHILD process forked by
the PyInstaller bootloader; the *bootloader* is the process Tauri spawns and
kills. Killing it (SIGKILL on Unix / TerminateProcess on Windows — both
uncatchable, so the bootloader can't forward them) would otherwise ORPHAN this
uvicorn child, which keeps holding port 8000 and blocks the next launch /
backend restart from binding (verified on Linux). ``_install_parent_death_watchdog``
watches the bootloader and exits this child the moment it dies, releasing the
port.
"""

from __future__ import annotations

import argparse


def _install_parent_death_watchdog() -> None:
    """Exit this process when its parent (the PyInstaller bootloader) dies.

    Best-effort and self-contained: if the parent can't be determined (e.g.
    psutil is unavailable in a non-frozen run) the watchdog simply does not arm
    — it must never terminate a healthy server spuriously. Runs a daemon thread
    that blocks on the parent and calls ``os._exit`` (hard exit from a thread)
    when it goes away, so the OS releases the listening socket.
    """
    import os
    import sys
    import threading

    try:
        import psutil
    except Exception as exc:  # pragma: no cover - only in a build missing psutil
        print(
            f"[tcg-backend] WARNING: psutil unavailable; parent-death watchdog "
            f"disabled (orphan-on-kill possible): {exc}",
            file=sys.stderr,
            flush=True,
        )
        return

    try:
        # os.getppid() is the bootloader. psutil.Process pins its identity by
        # creation time, so .wait() still resolves correctly even if the PID is
        # later reused by an unrelated process.
        parent = psutil.Process(os.getppid())
    except Exception as exc:  # pragma: no cover - parent already gone, etc.
        print(
            f"[tcg-backend] WARNING: could not resolve parent process; "
            f"watchdog disabled: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return

    parent_pid = parent.pid

    def _wait_then_exit() -> None:
        try:
            parent.wait()  # blocks until the bootloader terminates
        except Exception:
            return  # don't kill the server on a watchdog error
        print(
            f"[tcg-backend] parent {parent_pid} exited; shutting down sidecar "
            f"to release the port",
            file=sys.stderr,
            flush=True,
        )
        os._exit(0)

    threading.Thread(
        target=_wait_then_exit, name="parent-death-watchdog", daemon=True
    ).start()
    print(
        f"[tcg-backend] parent-death watchdog armed (parent pid {parent_pid})",
        file=sys.stderr,
        flush=True,
    )


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

    # Don't outlive the Tauri app: with a one-file build, the process Tauri kills
    # is the bootloader, not this uvicorn child. Arm the watchdog before serving
    # so a killed bootloader can never leave us orphaned holding the port.
    _install_parent_death_watchdog()

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

    load_dotenv(
        os.environ.get("TCG_ENV_FILE") or os.path.join(os.getcwd(), ".env"),
        override=False,
    )

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
