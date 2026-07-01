"""Process-wide dwh-pool concurrency gate for option-stream resolution.

Why this lives in ``tcg.core``
------------------------------
The option-stream resolver (``tcg.engine.options.series.stream_resolver``) fans
out one ``query_chain_bulk`` per expiration plus per-date underlying lookups,
each acquiring a connection from the ONE shared dwh pool
(``tcg.data._sql.connection.DwhConnectionPool``, ``max_size`` =
:data:`DEFAULT_DWH_POOL_MAX_SIZE`).  The resolver's own per-call semaphore bounds
a SINGLE resolve, but two resolves running at once (e.g. the Data-page
``BasketChart`` firing the composite series + one per-leg series, or two browser
panels) each take up to that many slots → their SUM over-subscribes the pool →
``PoolTimeout`` ("couldn't get a connection after 30.00 sec").

The fix is ONE process-wide ``asyncio.Semaphore`` sized to the pool, shared by
every resolve so the *combined* fan-out stays within the pool.  It must be owned
HERE (not in the engine): the engine layer must not import ``tcg.data`` (the
``engine``⊥``data`` import-linter contract), so it cannot see the pool — it only
accepts an injected ``concurrency_gate``.  ``tcg.core`` is the one layer that may
reference both, so it builds the gate (from the dependency-free
:data:`DEFAULT_DWH_POOL_MAX_SIZE` in ``tcg.types``) and passes it down.

Sizing & lifetime
------------------
Sized ``max(1, DEFAULT_DWH_POOL_MAX_SIZE - 1)`` — reserve one slot for the
interleaved expirations / underlying-price / spot lookups that share the pool
(the same reservation the engine's per-call default makes).  A semaphore is
bound to the event loop it is created on, so we cache ONE per running loop
(keyed by ``id(loop)``): production has a single loop (the app lives there for
its whole lifetime); pytest may spin a fresh loop per test, and keying by loop
avoids "bound to a different loop" errors without leaking a stale semaphore into
the next test's loop.
"""

from __future__ import annotations

import asyncio

from tcg.types.market import DEFAULT_DWH_POOL_MAX_SIZE

# Reserve one pool slot for the interleaved single-connection lookups
# (list_expirations / underlying price / coin spot) that run alongside the
# bulk fan-out.  Floor at 1 so a (hypothetical) pool of 1 still makes progress.
_GATE_SIZE = max(1, DEFAULT_DWH_POOL_MAX_SIZE - 1)

# loop id -> the shared semaphore for that loop.  Module-global so all callers in
# the process share one gate per loop.
_GATES: dict[int, asyncio.Semaphore] = {}


def get_dwh_concurrency_gate() -> asyncio.Semaphore:
    """Return the process-wide dwh-pool concurrency gate for the running loop.

    Lazily creates (and caches) one ``asyncio.Semaphore(_GATE_SIZE)`` per event
    loop.  Must be called from within a running loop (it is — every option-stream
    resolve runs inside the request handler's loop).
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    gate = _GATES.get(key)
    if gate is None:
        gate = asyncio.Semaphore(_GATE_SIZE)
        _GATES[key] = gate
    return gate


__all__ = ["get_dwh_concurrency_gate"]
