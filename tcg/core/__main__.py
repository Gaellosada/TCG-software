"""Backend entry point: ``python -m tcg.core``.

Launches the FastAPI app under uvicorn. On Windows it first installs the
SelectorEventLoop policy, because psycopg's async driver cannot run on the
default Windows ProactorEventLoop ("Psycopg cannot use the 'ProactorEventLoop'
to run in async mode"). This MUST happen before uvicorn creates the event
loop — setting it inside ``app.py`` is too late, because uvicorn imports the
app only after the loop is already running.

SelectorEventLoop is compatible with the rest of the stack: the SSM tunnel uses
``subprocess.Popen`` + threads (not asyncio subprocesses), and Motor and uvicorn
both run on it.

On Windows the backend MUST be started this way (the launcher does), not via a
bare ``uvicorn tcg.core.app:app``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tcg.core")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    # psycopg (async) requires a SelectorEventLoop; Windows defaults to
    # ProactorEventLoop, which psycopg refuses. Set the policy before uvicorn
    # creates the loop. No-op (and unnecessary) on Linux/macOS, whose default
    # loop is already selector-based.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import uvicorn

    uvicorn.run(
        "tcg.core.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
