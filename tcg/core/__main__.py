"""Backend entry point: ``python -m tcg.core``.

Launches the FastAPI app under uvicorn. On Linux/WSL the default uvicorn
event loop is a ``SelectorEventLoop``, which psycopg's async driver
requires — so no custom loop factory is needed. Starts ONLY the backend;
the Vite frontend is a separate ``npm run dev`` process (see ``start.sh``).
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
    )


if __name__ == "__main__":
    main()
