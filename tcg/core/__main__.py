"""Backend entry point: ``python -m tcg.core``.

Launches the FastAPI app under uvicorn, forcing a SelectorEventLoop via a custom
``loop`` factory. This is required on Windows: psycopg's async driver cannot run
on the default ProactorEventLoop, and uvicorn >= 0.36 picks the loop through a
``loop_factory`` that overrides asyncio's event-loop policy — so the loop must be
chosen via ``Config.loop`` (see ``tcg.core._eventloop``), not a policy call.

On Windows the backend MUST be started this way (the launcher does), not via a
bare ``uvicorn tcg.core.app:app`` (which would use the default Proactor loop).
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tcg.core")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "tcg.core.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        # Force a SelectorEventLoop (psycopg async is incompatible with Windows'
        # default ProactorEventLoop). uvicorn imports this factory for any
        # non-builtin loop value.
        loop="tcg.core._eventloop:loop_factory",
    )


if __name__ == "__main__":
    main()
