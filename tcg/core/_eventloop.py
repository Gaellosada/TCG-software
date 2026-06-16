"""Custom uvicorn event-loop factory: forces a SelectorEventLoop.

psycopg's async driver cannot run on Windows' default ProactorEventLoop
("Psycopg cannot use the 'ProactorEventLoop' to run in async mode").

uvicorn >= 0.36 selects the loop through a ``loop_factory`` (``Server.run`` →
``asyncio_run(serve(), loop_factory=config.get_loop_factory())``), and on
Windows the built-in ``asyncio`` factory returns a ``ProactorEventLoop``. A
``loop_factory`` passed to ``asyncio.run`` OVERRIDES the asyncio event-loop
*policy* — so ``set_event_loop_policy(WindowsSelectorEventLoopPolicy())`` has no
effect under uvicorn. The reliable way to get a SelectorEventLoop is to hand
uvicorn this factory via ``Config.loop`` (``loop="tcg.core._eventloop:loop_factory"``);
``get_loop_factory`` imports it for any non-builtin loop value.

SelectorEventLoop is the default on Linux/macOS and is compatible with the rest
of the stack: the SSM tunnel uses ``subprocess.Popen`` + threads (not asyncio
subprocesses), and Motor and uvicorn both run on it.
"""

from __future__ import annotations

import asyncio


def loop_factory() -> asyncio.AbstractEventLoop:
    """Return a SelectorEventLoop (psycopg-async compatible on every platform)."""
    loop = asyncio.SelectorEventLoop()
    # Printed before logging is configured, so use a plain flushed print — this
    # is the startup proof of which loop is actually running.
    print(
        f"[tcg.core] event loop: {type(loop).__name__} (psycopg-compatible)", flush=True
    )
    return loop
