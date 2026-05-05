"""Vectorised indicator primitives for the mongoDB-backtester.

Pure NumPy. No I/O, no MongoDB, no closed strategy taxonomy. Strategies
import whichever primitives they need and compose them as they like — the
lib is helpers, not gatekeepers.

NaN warm-up convention:
- ``sma``: first ``window - 1`` values are NaN
- ``ema``: first finite close seeds the recursion; preceding NaN gaps stay NaN
- ``rsi``: first ``window`` indices are NaN (Wilder's smoothing)
- ``breakout``: first ``lookback`` indices are 0 (no comparison window yet)
- ``rolling_vol``: first ``window`` indices are NaN
"""
from __future__ import annotations

from typing import Literal

import numpy as np
from numpy.typing import NDArray


def sma(close: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """Simple moving average over ``window`` bars; first ``window - 1`` NaN."""
    if window <= 0:
        raise ValueError("window must be > 0")
    x = np.asarray(close, dtype=np.float64)
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window:
        return out
    csum = np.cumsum(np.where(np.isnan(x), 0.0, x))
    nan_count = np.cumsum(np.isnan(x).astype(np.int64))
    window_sum = csum[window - 1:].copy()
    window_sum[1:] = window_sum[1:] - csum[:-window]
    window_nans = nan_count[window - 1:].copy()
    window_nans[1:] = window_nans[1:] - nan_count[:-window]
    avg = window_sum / float(window)
    avg = np.where(window_nans > 0, np.nan, avg)
    out[window - 1:] = avg
    return out


def ema(close: NDArray[np.float64], span: int) -> NDArray[np.float64]:
    """Exponential moving average with smoothing factor ``2/(span+1)``.

    Seeded at the first finite element; trailing NaNs after seeding propagate
    the previous EMA forward (no decay through gaps).
    """
    if span <= 0:
        raise ValueError("span must be > 0")
    x = np.asarray(close, dtype=np.float64)
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return out
    alpha = 2.0 / (float(span) + 1.0)
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
    """Wilder's RSI; values bounded in ``[0, 100]``. First ``window`` indices NaN."""
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
    avg_gain = float(np.mean(gains[:window]))
    avg_loss = float(np.mean(losses[:window]))
    if avg_loss == 0.0:
        out[window] = 100.0 if avg_gain > 0.0 else 50.0
    else:
        rs = avg_gain / avg_loss
        out[window] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(window + 1, n):
        g = float(gains[i - 1])
        loss = float(losses[i - 1])
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + loss) / window
        if avg_loss == 0.0:
            out[i] = 100.0 if avg_gain > 0.0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def breakout(
    high: NDArray[np.float64],
    low: NDArray[np.float64],
    close: NDArray[np.float64],
    lookback: int,
) -> NDArray[np.float64]:
    """Donchian-style breakout signal.

    Returns ``+1`` when ``close[i] > max(high[i-lookback:i])`` (strict break of
    the prior N-bar high), ``-1`` when ``close[i] < min(low[i-lookback:i])``,
    ``0`` otherwise. The first ``lookback`` indices are 0 (no prior window).
    """
    if lookback <= 0:
        raise ValueError(f"lookback must be > 0, got {lookback!r}")
    h = np.asarray(high, dtype=np.float64)
    low_arr = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    length = c.shape[0]
    if not (h.shape[0] == low_arr.shape[0] == length):
        raise ValueError("high/low/close length mismatch")
    out = np.zeros(length, dtype=np.float64)
    if length <= lookback:
        return out
    for i in range(lookback, length):
        prior_high = float(np.max(h[i - lookback:i]))
        prior_low = float(np.min(low_arr[i - lookback:i]))
        ci = float(c[i])
        if ci > prior_high:
            out[i] = 1.0
        elif ci < prior_low:
            out[i] = -1.0
    return out


def rolling_vol(
    close: NDArray[np.float64],
    window: int,
    *,
    annualise_by: int | None = None,
) -> NDArray[np.float64]:
    """Annualised rolling standard deviation of bar-over-bar returns.

    Returns a NaN-warm-up array shaped like ``close``: the first ``window``
    values are NaN (matches the *return*-based warm-up: ``window`` returns
    require ``window+1`` prices). ``annualise_by`` defaults to
    ``lib.constants.TRADING_DAYS_PER_YEAR``.
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
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(x) / np.where(x[:-1] == 0, np.nan, x[:-1])
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
    entry signal. A constant ``signal=1.0`` therefore opens exactly one
    position over the whole run, even when paired with ``DaysToHold(n=1)``. To
    re-enter on every bar, the signal must change every bar — the simplest
    pattern is alternating signs. The PnL of an option leg with
    ``DaysToHold(n=1)`` is invariant to the sign of the entry-trigger signal
    because the leg side comes from ``OptionLegSpec.side``.
    """
    if n_bars < 0:
        raise ValueError(f"n_bars must be >= 0, got {n_bars!r}")
    out = np.ones(int(n_bars), dtype=np.float64)
    out[1::2] = -1.0
    return out


__all__ = [
    "sma",
    "ema",
    "rsi",
    "breakout",
    "rolling_vol",
    "apply_direction",
    "daily_pulse",
]
