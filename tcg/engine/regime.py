"""Pure vol-regime SIGNAL computation for the intraday long-gamma page (F2.1).

This module computes REALIZED-VOLATILITY signals (RV H20/H30/H100) from a daily
close series and NOTHING ELSE — it deliberately carries NO side-decision, NO
threshold, and NO regime->action logic (that is the SEPARATE later F2.2 task).
It is a PURE, dependency-free part of :mod:`tcg.engine`: it imports only stdlib +
NumPy and NEVER touches :mod:`tcg.data` (the module-boundary contract is
``types <- data/engine <- core`` with data/engine independent). The FETCH of the
daily closes — via the P0.3 :class:`tcg.data._sql.daily_series.DailySeriesReader`
seam — and the join with VVIX/VIX1D passthrough happen one layer up, in
:mod:`tcg.core.api.intraday_backtest`, which then feeds these signals to the sim.

Realized-volatility convention (documented exactly)
---------------------------------------------------
Given a close series ``C[0..n-1]`` ordered ASCENDING by trade date:

1. Daily LOG returns ``r[k] = ln(C[k+1] / C[k])`` (``n-1`` of them; ``r[k]`` is
   the return realized AT price index ``k+1``).
2. Rolling realized vol at price index ``i`` for a ``window`` of ``w`` trading
   days is the SAMPLE standard deviation (``ddof=1``, i.e. divide by ``w-1``) of
   the trailing ``w`` log returns ending at ``i`` — the returns ``r[i-w .. i-1]``
   — ANNUALIZED by ``sqrt(annualization)`` (``annualization=252`` trading days).
3. NO-LOOK-AHEAD: RV at date ``i`` uses only closes up to and including ``i``.
4. INSUFFICIENT HISTORY: the first ``w`` price points (indices ``0..w-1``) have
   fewer than ``w`` trailing returns, so their RV is ``None`` — a null, NEVER a
   fabricated value.
5. A CONSTANT series has all-zero log returns => RV ``0.0`` (not null) once there
   is enough history.

Both functions are fully deterministic and unit-testable WITHOUT a database.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def rolling_realized_vol(
    values: Sequence[float],
    window: int,
    *,
    annualization: int = 252,
) -> list[float | None]:
    """Trailing-``window`` annualized realized vol of a daily close series.

    Parameters
    ----------
    values : Sequence[float]
        Daily closes ordered ASCENDING by date (the caller — the P0.3 reader —
        already returns ascending, NULL-dropped floats). Must be strictly
        positive (a log return is taken); a non-positive close is a data fault
        the reader would not emit and is not silently masked here.
    window : int
        Number of trailing trading-day RETURNS in the estimate (>= 2; a sample
        std of one return is undefined, so ``window < 2`` raises loudly rather
        than returning a silent NaN).
    annualization : int
        Trading days per year for the ``sqrt`` annualization (default 252).

    Returns
    -------
    list[float | None]
        One entry PER input close, aligned to ``values`` (same length/order).
        Indices ``0..window-1`` are ``None`` (insufficient history); index ``i
        >= window`` is the annualized sample std of ``r[i-window .. i-1]``.
    """
    if window < 2:
        raise ValueError(f"window must be >= 2 (realized-vol needs >=2 returns); got {window}")

    n = len(values)
    out: list[float | None] = [None] * n
    if n < window + 1:
        # Fewer than ``window`` returns anywhere in the series -> all None.
        return out

    arr = np.asarray(values, dtype=float)
    if not np.all(arr > 0.0):
        raise ValueError("realized vol requires strictly positive closes (log return)")

    log_ret = np.diff(np.log(arr))  # length n-1; log_ret[k] realized at index k+1
    ann = math.sqrt(float(annualization))

    # RV at price index i (i >= window) = sample-std of the window returns
    # log_ret[i-window .. i-1] (w values), annualized.
    for i in range(window, n):
        window_returns = log_ret[i - window : i]
        rv = float(np.std(window_returns, ddof=1)) * ann
        out[i] = rv
    return out


def realized_vol_by_date(
    dates: Sequence[int],
    values: Sequence[float],
    windows: Sequence[int],
    *,
    annualization: int = 252,
) -> dict[int, dict[str, float | None]]:
    """Per-date realized-vol signal map ``{date_int: {"h<w>": rv | None}}``.

    Pure join of :func:`rolling_realized_vol` over each requested ``window`` onto
    the parallel ``dates``. The signal KEY for a window ``w`` is ``f"h{w}"`` (so
    ``[20, 30, 100]`` -> keys ``h20``/``h30``/``h100``) — the exact per-day field
    names the intraday response surfaces. A date with insufficient history for a
    given window carries ``None`` for that key (never a fabricated number).

    ``dates`` and ``values`` MUST be equal length and ascending-by-date (the P0.3
    reader guarantees this); an empty series yields an empty map.
    """
    if len(dates) != len(values):
        raise ValueError(
            f"dates/values length mismatch: {len(dates)} != {len(values)}"
        )
    result: dict[int, dict[str, float | None]] = {int(d): {} for d in dates}
    for w in windows:
        rv = rolling_realized_vol(values, w, annualization=annualization)
        key = f"h{w}"
        for d, v in zip(dates, rv):
            result[int(d)][key] = v
    return result
