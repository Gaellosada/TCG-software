"""Shared JSON-serialization helpers for API routers.

Keeps NaN-handling identical across endpoints: JSON ``NaN`` isn't valid
per RFC 8259, so NaN floats must map to ``null`` before serialization.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt


def nan_safe_floats(
    arr: npt.NDArray[np.floating] | None,
) -> list[float | None]:
    """Convert a float array to a JSON-safe list with NaN → ``None``.

    Note: on its own this helper maps ``NaN`` to ``None`` and lets ``inf``
    pass through (historical price-array behaviour). But any payload that
    is then routed through :func:`sanitize_json_floats` — as the portfolio
    compute response now is, in full — has its ``inf`` nulled at that
    boundary too, so the "inf passes through" contract only holds for
    callers that do NOT apply the terminal sanitizer. For blocks that must
    be strictly RFC-8259 finite on their own, use
    :func:`sanitize_json_floats`, which nulls both ``NaN`` and ``inf``.
    """
    if arr is None:
        return []
    return [None if (v != v) else float(v) for v in arr.tolist()]


def sanitize_json_floats(value: Any) -> Any:
    """Recursively map every non-finite float to ``None`` for JSON output.

    The project's invariant (see module docstring) is that JSON ``NaN`` /
    ``Infinity`` are invalid per RFC 8259 and must serialize as ``null``.
    FastAPI's default encoder emits bare ``NaN`` / ``Infinity`` tokens,
    which the browser's strict ``Response.json()`` rejects — so aggregate
    blocks (``metrics`` / ``leg_metrics`` / ``monthly_returns`` /
    ``yearly_returns``) must be passed through this sanitizer before they
    go into the response.

    Walks ``dict`` and ``list`` / ``tuple`` containers recursively. Any
    float (Python or NumPy) that is NaN, ``+inf`` or ``-inf`` becomes
    ``None``; finite floats are returned as plain ``float``. ``bool``
    (an ``int`` subclass) and other scalars pass through unchanged. The
    input is never mutated — containers are rebuilt.
    """
    # ``bool`` is a subclass of ``int`` — keep it as-is, don't treat as float.
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {k: sanitize_json_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json_floats(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    return value
