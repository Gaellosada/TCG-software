"""Shared JSON-serialization helpers for API routers.

Keeps NaN-handling identical across endpoints: JSON ``NaN`` isn't valid
per RFC 8259, so NaN floats must map to ``null`` before serialization.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt


def nan_safe_floats(
    arr: npt.NDArray[np.floating] | None,
) -> list[float | None]:
    """Convert a float array to a JSON-safe list with NaN → ``None``."""
    if arr is None:
        return []
    return [None if (v != v) else float(v) for v in arr.tolist()]
