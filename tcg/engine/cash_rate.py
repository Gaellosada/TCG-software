"""Cash / financing accrual leg — the pure accrual math (DB-free).

A *cash-rate leg* is a first-class portfolio leg that earns a short-term
interest rate on cash collateral.  It has near-zero volatility and an
all-positive drift when the rate is non-negative — mirroring the legacy
``USD_1M_rate(P)`` leg (SPEC §5.7): its equity accrues the US short rate day
by day.

This module owns ONLY the numerics (``tcg.engine`` may depend on
``tcg.types`` but never on ``tcg.data``): given a per-bar annual rate it
produces a base-100 equity curve that other legs' curves combine with in the
weighted portfolio.  The rate SOURCE — a fetched dwh short-rate series — is
resolved by the wiring layer (``tcg.core``) and handed here as a plain array;
the reindex helper below is the only series-shaping logic and is pure.

Unit / convention (documented, verified in tests)
--------------------------------------------------
* The input rate is an **annualized rate expressed as a FRACTION** (0.01 =
  1 %/yr), NOT a percent.  The wiring layer divides a percent source by 100.
* **Accrual is COMPOUND on a 252 trading-day year** (the default):

      daily_return_i = (1 + rate_i) ** (1 / 252) - 1

  Compounded is the more correct convention (SPEC §5.7 quotes an annualized
  ``ann_ret``; compounding one trading-day at a time recovers ``1 + rate`` over
  ~252 bars).  A ``compound=False`` simple-interest mode (``rate / 252``) is
  offered for callers that prefer it; both are exercised by the unit oracle.
* We accrue **once per bar the portfolio trades** (a trading-day grid).  Weekend
  / holiday interest is folded into the trading-day factor by the 252-day
  annualization — consistent with every other %-return leg in the engine, which
  also compounds per trading bar.
* ``equity[0] == base`` (no accrual booked on the first bar; the leg is funded
  at t0).  The return booked on bar *i* (i>=1), moving equity from bar ``i-1``
  to bar ``i``, uses ``rate[i]`` (the rate observed on that bar).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

__all__ = [
    "DEFAULT_DAY_COUNT",
    "accrue_cash_equity",
    "daily_accrual_factor",
    "reindex_rate_series",
]

DEFAULT_DAY_COUNT = 252


def daily_accrual_factor(
    rate_annual: npt.NDArray[np.float64],
    *,
    day_count: int = DEFAULT_DAY_COUNT,
    compound: bool = True,
) -> npt.NDArray[np.float64]:
    """Per-bar growth factor ``1 + daily_return`` for an annual-rate array.

    ``rate_annual`` is a FRACTION array (0.01 == 1 %/yr).  Compound mode returns
    ``(1 + rate) ** (1/day_count)``; simple mode returns ``1 + rate/day_count``.
    A rate of exactly 0 yields a factor of exactly 1.0 (flat equity).
    """
    if day_count <= 0:
        raise ValueError(f"day_count must be positive, got {day_count}")
    r = np.asarray(rate_annual, dtype=np.float64)
    if compound:
        # (1 + r) ** (1/dc). Guard 1 + r <= 0 (a rate <= -100 %/yr is nonsensical
        # for a cash leg) so a fractional power never yields NaN silently.
        base = 1.0 + r
        if np.any(base <= 0.0):
            raise ValueError(
                "cash-rate leg requires rate_annual > -1 (i.e. > -100 %/yr); "
                "got a value <= -1"
            )
        return np.power(base, 1.0 / day_count)
    return 1.0 + r / day_count


def accrue_cash_equity(
    rate_annual: float | npt.NDArray[np.float64],
    n: int | None = None,
    *,
    day_count: int = DEFAULT_DAY_COUNT,
    base: float = 100.0,
    compound: bool = True,
) -> npt.NDArray[np.float64]:
    """Base-``base`` equity curve accruing ``rate_annual`` per trading bar.

    Parameters
    ----------
    rate_annual : float or 1-D array (FRACTION units, 0.01 == 1 %/yr)
        A scalar constant rate, or a per-bar rate array of length ``n``.
    n : int, optional
        Number of bars.  Required when ``rate_annual`` is a scalar; ignored (but
        cross-checked) when it is an array.
    day_count : int
        Trading days per year (252 by convention).
    base : float
        Starting equity level (100.0, matching every synthetic leg curve).
    compound : bool
        Compound (default) vs simple interest — see module docstring.

    Returns
    -------
    np.ndarray of shape (n,) : ``equity[0] == base``; monotonically
    non-decreasing when every rate >= 0.
    """
    if np.isscalar(rate_annual):
        if n is None:
            raise ValueError("n is required when rate_annual is a scalar")
        rate = np.full(int(n), float(rate_annual), dtype=np.float64)
    else:
        rate = np.asarray(rate_annual, dtype=np.float64)
        if rate.ndim != 1:
            raise ValueError("rate_annual array must be 1-D")
        if n is not None and int(n) != rate.shape[0]:
            raise ValueError(
                f"n ({n}) does not match rate_annual length ({rate.shape[0]})"
            )

    length = rate.shape[0]
    if length == 0:
        return np.empty(0, dtype=np.float64)

    factors = daily_accrual_factor(rate, day_count=day_count, compound=compound)
    # Bar 0 is the funding bar (no accrual): force its factor to 1.0 so
    # equity[0] == base regardless of rate[0].
    factors[0] = 1.0
    equity = base * np.cumprod(factors)
    return equity


def reindex_rate_series(
    src_dates: npt.NDArray[np.int64],
    src_rates: npt.NDArray[np.float64],
    target_dates: npt.NDArray[np.int64],
    *,
    fallback: float = 0.0,
) -> npt.NDArray[np.float64]:
    """Step-fill a (dates, rates) series onto ``target_dates`` (both YYYYMMDD).

    A short rate is a piecewise-constant quote: it holds until the next observed
    value.  For each target bar we take the LAST source rate on or before that
    bar (``searchsorted`` right).  Target bars BEFORE the first source date get
    ``fallback`` (the flat default rate the wiring passes, so a series that
    starts mid-history still accrues sensibly at the edges rather than NaN).

    ``src_dates`` must be sorted ascending (the dwh reader returns trade_date
    ordered).  Returns a FRACTION array aligned to ``target_dates``.
    """
    src_dates = np.asarray(src_dates, dtype=np.int64)
    src_rates = np.asarray(src_rates, dtype=np.float64)
    target_dates = np.asarray(target_dates, dtype=np.int64)
    if src_dates.shape[0] != src_rates.shape[0]:
        raise ValueError("src_dates and src_rates must be the same length")
    out = np.full(target_dates.shape[0], float(fallback), dtype=np.float64)
    if src_dates.shape[0] == 0:
        return out
    # idx-1 = index of the last src date <= each target date (-1 => before start)
    idx = np.searchsorted(src_dates, target_dates, side="right") - 1
    covered = idx >= 0
    out[covered] = src_rates[idx[covered]]
    return out
