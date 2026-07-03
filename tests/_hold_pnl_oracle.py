"""Shared oracle + fixture + synthetic fetcher for the option HOLD-mode $-P&L tests.

Extracted from (and imported by) the three hold-P&L suites so the Java-faithful
dollar-NAV oracle, the APR→MAY roll fixture, and the synthetic hold-fetcher live in
ONE place instead of being re-declared per module:

  * ``tests/unit/test_api_portfolio_option_hold_pnl.py``          (portfolio HTTP path)
  * ``tests/engine/options/test_signal_exec_option_hold_pnl.py``  (signal_exec path)
  * ``tests/engine/options/test_stream_hold_signal_e2e.py``       (full resolver e2e)

Importable by BARE name (``from _hold_pnl_oracle import ...``) because the root
``tests/conftest.py`` puts ``tests/`` on ``sys.path`` (``tests`` is not usable as a
package name — a site-packages ``tests`` shadows it).
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import numpy as np

from tcg.types.signal import InstrumentOptionStream, InstrumentSpot


# ── The APR→MAY hold fixture (shape shared by the resolver tests) ───────────
#   APR K4400 held mids: 30,28,26,24(roll-day OLD mid); MAY K4450: 18(open),20,19
#   values[t] = owner-of-step mid LEVEL (OLD on roll day) = [30,28,26,24,20,19]
#   is_roll = [1,0,0,1,0,0]; roll_premium = [30,·,·,18,·,·]
DATES_INT = np.array(
    [20240327, 20240328, 20240329, 20240401, 20240402, 20240403], dtype=np.int64
)
HELD_PREMIUM = np.array([30.0, 28.0, 26.0, 24.0, 20.0, 19.0])
IS_ROLL = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
ROLL_PREMIUM = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
# Owner arrays for the oracle (same contract per step; OLD into the roll):
#   t1 APR 30->28, t2 28->26, t3 26->24 (OLD into roll), t4 MAY 18->20, t5 20->19
OWNER_PREV = np.array([np.nan, 30.0, 28.0, 26.0, 18.0, 20.0])
OWNER_CUR = np.array([np.nan, 28.0, 26.0, 24.0, 20.0, 19.0])


def oracle_ratio(
    owner_prev: np.ndarray,
    owner_cur: np.ndarray,
    is_roll: np.ndarray,
    roll_premium: np.ndarray,
    *,
    nav_times: float,
    weight: float,
    base_nav: float = 1_000_000.0,
) -> np.ndarray:
    """Java-faithful fixed-contract dollar-P&L NAV → base-1 ratio.

    ``owner_prev[t]`` / ``owner_cur[t]`` are the step-owner contract's mids on days
    t-1 / t (same contract per step; the OLD contract for the step ending on a roll
    day).  ``roll_premium`` at each ``is_roll`` date is the NEW segment's roll-day
    open mid.  Size once per roll off the compounding NAV and the roll premium, hold
    fixed, book ``sign(weight)·qty·(cur-prev)`` daily (a short short-put gains on a
    falling premium), realise+resize at each roll, normalise NAV to a base-1 ratio.

    The ENGINE weight sign maps to the oracle's direction: a LONG (weight>0) gains on
    rising premium → +qty·(cur-prev); a SHORT (weight<0) gains on falling premium →
    both are ``sign(weight)·qty·(cur-prev)``.  ``weight`` is consulted only for its
    sign — its magnitude does NOT scale the notional (that is ``nav_times``).
    """
    T = len(owner_cur)
    nav = np.empty(T, dtype=np.float64)
    nav[0] = base_nav
    sign = 1.0 if weight > 0 else -1.0
    qty = nav_times * base_nav / roll_premium[0]
    for t in range(1, T):
        dprem = owner_cur[t] - owner_prev[t]
        if not np.isfinite(dprem):
            dprem = 0.0
        nav[t] = nav[t - 1] + sign * qty * dprem
        if bool(is_roll[t]):
            qty = nav_times * nav[t] / roll_premium[t]
    return nav / nav[0]


def make_hold_fetch(
    *,
    held_premium: np.ndarray = HELD_PREMIUM,
    is_roll: np.ndarray = IS_ROLL,
    roll_premium: np.ndarray = ROLL_PREMIUM,
    dates_int: np.ndarray = DATES_INT,
    spx: np.ndarray | None = None,
    require_hold: bool = False,
    diagnostics: Sequence[str | None] | None = None,
) -> Callable[..., Any]:
    """Build a synthetic ``PriceFetcher`` over a held-premium + roll-info fixture.

    dwh-free and deterministic; matches the production fetcher shape — a
    ``fetch(instrument, field)`` coroutine carrying a ``fetch_hold_roll_info``
    attribute (and, when ``diagnostics`` is given, a ``fetch_hold_diagnostics``
    attribute mirroring the real fetcher's optional side-channel).

    Dispatch:
      * ``InstrumentSpot``         → ``(dates_int, spx)``  (spx defaults to flat 100)
      * ``InstrumentOptionStream`` → ``(dates_int, held_premium)``
      * anything else              → ``KeyError``

    ``require_hold`` asserts the option instrument is in hold mode (used by the
    portfolio-path test, which must pass ``hold=True`` through to the fetcher).
    """
    spx_series = (
        np.full(len(dates_int), 100.0, dtype=np.float64) if spx is None else spx
    )

    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            return dates_int, spx_series
        if isinstance(instrument, InstrumentOptionStream):
            if require_hold:
                assert instrument.hold_between_rolls is True
            return dates_int, np.asarray(held_premium, dtype=np.float64).copy()
        raise KeyError(f"no data for {instrument!r} ({field})")

    async def fetch_hold_roll_info(instrument):
        assert isinstance(instrument, InstrumentOptionStream)
        return (
            dates_int,
            np.asarray(is_roll, dtype=np.float64).copy(),
            np.asarray(roll_premium, dtype=np.float64).copy(),
        )

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]

    if diagnostics is not None:

        async def fetch_hold_diagnostics(instrument):
            return list(diagnostics)

        fetch.fetch_hold_diagnostics = fetch_hold_diagnostics  # type: ignore[attr-defined]

    return fetch
