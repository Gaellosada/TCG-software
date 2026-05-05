"""Vectorized signal primitives: SMA, EMA, RSI, direction clipping.

Pure NumPy. NaN propagates through warm-up windows per convention:
- sma: first (window-1) values are NaN
- ema: first value seeded from first finite close, no NaN warm-up
- rsi: first `window` values are NaN
"""
from __future__ import annotations

from typing import Literal

import numpy as np
from numpy.typing import NDArray


def sma(close: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """Simple moving average over `window` bars; first window-1 values are NaN."""
    if window <= 0:
        raise ValueError("window must be > 0")
    x = np.asarray(close, dtype=np.float64)
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    # Cumulative-sum trick handles NaN by treating it as a gap; for simplicity
    # we require finite inputs and let downstream callers ffill if needed.
    csum = np.cumsum(np.where(np.isnan(x), 0.0, x))
    nan_count = np.cumsum(np.isnan(x).astype(np.int64))
    # Window sum at index i (inclusive) = csum[i] - csum[i-window]
    window_sum = csum[window - 1:].copy()
    window_sum[1:] = window_sum[1:] - csum[:-window]
    window_nans = nan_count[window - 1:].copy()
    window_nans[1:] = window_nans[1:] - nan_count[:-window]
    avg = window_sum / float(window)
    avg = np.where(window_nans > 0, np.nan, avg)
    out[window - 1:] = avg
    return out


def ema(close: NDArray[np.float64], span: int) -> NDArray[np.float64]:
    """Exponential moving average with smoothing factor 2/(span+1)."""
    if span <= 0:
        raise ValueError("span must be > 0")
    x = np.asarray(close, dtype=np.float64)
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return out
    alpha = 2.0 / (float(span) + 1.0)
    # Seed at first finite value.
    seeded = False
    prev = 0.0
    for i in range(n):
        v = float(x[i])
        if np.isnan(v):
            if seeded:
                out[i] = prev
            continue
        if not seeded:
            prev = v
            seeded = True
        else:
            prev = alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rsi(close: NDArray[np.float64], window: int = 14) -> NDArray[np.float64]:
    """Wilder's RSI; values in [0, 100]; first `window` indices are NaN."""
    if window <= 0:
        raise ValueError("window must be > 0")
    x = np.asarray(close, dtype=np.float64)
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= window:
        return out
    diffs = np.diff(x)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    # Initial average over the first `window` deltas.
    avg_gain = float(np.mean(gains[:window]))
    avg_loss = float(np.mean(losses[:window]))
    # First RSI value lands at index `window` in `close`.
    if avg_loss == 0.0:
        out[window] = 100.0 if avg_gain > 0.0 else 50.0
    else:
        rs = avg_gain / avg_loss
        out[window] = 100.0 - 100.0 / (1.0 + rs)
    # Wilder smoothing for the rest.
    for i in range(window + 1, n):
        g = float(gains[i - 1])
        l = float(losses[i - 1])
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + l) / window
        if avg_loss == 0.0:
            out[i] = 100.0 if avg_gain > 0.0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def rolling_vol(
    close: NDArray[np.float64],
    window: int,
    *,
    annualise_by: int | None = None,
) -> NDArray[np.float64]:
    """Annualised rolling standard deviation of bar-over-bar returns.

    Returns a NaN-warm-up array shaped like `close`: the first `window` values
    are NaN (matches the *return*-based warm-up: window returns require
    window+1 prices, so the first `window` close-indices are NaN).
    Annualisation factor defaults to `lib.constants.TRADING_DAYS_PER_YEAR`.
    """
    if window <= 0:
        raise ValueError("window must be > 0")
    from .constants import TRADING_DAYS_PER_YEAR
    ann = TRADING_DAYS_PER_YEAR if annualise_by is None else int(annualise_by)
    x = np.asarray(close, dtype=np.float64)
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= window:
        return out
    # Bar-over-bar returns; rets[0] is undefined.
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(x) / np.where(x[:-1] == 0, np.nan, x[:-1])
    # rets has length n-1; index i in `rets` corresponds to close[i+1]'s return.
    # Rolling std over `window` returns means we need at least `window` returns,
    # so the first finite value lands at close-index `window` (using rets[1..window]).
    for i in range(window, n):
        seg = rets[i - window: i]
        if np.any(np.isnan(seg)):
            continue
        out[i] = float(np.std(seg, ddof=1)) * np.sqrt(float(ann))
    return out


def apply_direction(
    raw: NDArray[np.float64],
    direction: Literal["long_only", "short_only", "long_short"],
) -> NDArray[np.float64]:
    """Clip a raw signal to the allowed sign for the given direction policy."""
    x = np.asarray(raw, dtype=np.float64)
    if direction == "long_only":
        return np.where(x > 0, x, 0.0).astype(np.float64)
    if direction == "short_only":
        return np.where(x < 0, x, 0.0).astype(np.float64)
    if direction == "long_short":
        return x.astype(np.float64).copy()
    raise ValueError(
        f"direction must be one of long_only/short_only/long_short, got {direction!r}"
    )


def daily_pulse(n_bars: int) -> NDArray[np.float64]:
    """Alternating ``+1 / -1`` signal of length ``n_bars`` for daily-rebalance strategies.

    The engine fires entries on a 0->nonzero transition or sign change in the
    entry signal (see ``lib.engine`` § entry trigger). A constant ``signal=1.0``
    therefore opens exactly one position over the whole run, even when paired
    with ``DaysToHold(n=1)``. To re-enter on every bar, the signal must change
    every bar — the simplest pattern is alternating signs.

    This helper produces ``[1, -1, 1, -1, ...]`` of length ``n_bars``. The
    PnL of an option leg with ``DaysToHold(n=1)`` is invariant to the sign of
    the entry-trigger signal because the leg side comes from
    ``OptionLegSpec.side``, not from the trigger sign. Use it as the
    ``BacktestSpec.signal`` (or as a named entry under ``secondary_signals``)
    when you want every bar to trigger a fresh entry.

    Example:
        >>> sig = daily_pulse(n_bars=len(bars.dates))
        >>> spec = BacktestSpec(..., signal=sig, sizing=SizingConfig(method="fixed_fraction", fraction=0.0))

    See ``pipeline/03-backtest.md`` § "Daily-rebalance signal semantics".
    """
    if n_bars < 0:
        raise ValueError(f"n_bars must be >= 0, got {n_bars!r}")
    out = np.ones(int(n_bars), dtype=np.float64)
    out[1::2] = -1.0
    return out


__all__ = ["sma", "ema", "rsi", "rolling_vol", "apply_direction", "daily_pulse"]
