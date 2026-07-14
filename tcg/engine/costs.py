"""Uniform proportional (bps) slippage & fees for the simulation engines.

This is the SINGLE home for the transaction-cost math shared by the signal
engine (:mod:`tcg.engine.signal_exec`), the portfolio engine
(:mod:`tcg.engine.metrics`) and the option-leg roll path
(:mod:`tcg.engine.hold_pnl`).  Pure NumPy -- no coupling to block/signal or
portfolio machinery, and no dependency outside ``tcg.types`` conventions.

Cost model (FIXED -- do not redesign):

* Uniform proportional model with two INDEPENDENT rates, ``slippage_bps`` and
  ``fees_bps`` (``rate = bps / 10_000``).  They are always tracked and reported
  SEPARATELY -- never merged into one number.
* Per-bar turnover ``T[t] = Σ_i |w_i_target − w_i_drifted|`` where
  ``w_i_drifted = pos_i·(1+r_i)/(1+R)`` and ``R = Σ_i pos_i·r_i``.  At the first
  bar (initial entry from cash) turnover is ``Σ_i |pos_i[0]|``.  A round-trip
  (rolls) charges two sides of the traded notional.
* Per bar: ``slippage_drag = slippage_rate·T[t]`` and
  ``fees_drag = fees_rate·T[t]``; BOTH are subtracted from that bar's portfolio
  return BEFORE compounding, so equity/Sharpe/etc. reflect them automatically.
* Reported totals are the cumulative cost divided by the initial capital
  (normalised to 1.0), as a PERCENT.  They MAY exceed 100% for high-turnover
  strategies -- that is correct.

OFF by default: a :class:`CostConfig` with both rates ``0`` makes
:meth:`CostConfig.is_zero` true; every caller MUST early-skip all cost math in
that case so output is byte-identical to the pre-feature behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

_BPS_PER_UNIT = 10_000.0


@dataclass(frozen=True)
class CostConfig:
    """Two independent basis-point rates. ``bps → rate`` conversion single-sited."""

    slippage_bps: float = 0.0
    fees_bps: float = 0.0

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / _BPS_PER_UNIT

    @property
    def fees_rate(self) -> float:
        return self.fees_bps / _BPS_PER_UNIT

    def is_zero(self) -> bool:
        """True iff BOTH rates are zero (feature off → early-skip, byte-identical)."""
        return self.slippage_bps == 0.0 and self.fees_bps == 0.0


@dataclass(frozen=True)
class CostTotals:
    """Cumulative cost as PERCENT of initial capital, tracked separately."""

    total_slippage_paid_pct: float = 0.0
    total_fees_paid_pct: float = 0.0


def establish_turnover(
    positions: npt.NDArray[np.float64],
    returns: npt.NDArray[np.float64],
    net_step: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Per-step turnover at each position's *establish* point.

    Parameters
    ----------
    positions:
        ``(T, K)`` target weight fraction of equity in each of ``K`` legs at each
        of ``T`` bars.  ``positions[s]`` is the position held over step
        ``s -> s+1``.
    returns:
        ``(T, K)`` simple per-leg returns; ``returns[t, k]`` is leg ``k``'s return
        realised over step ``t-1 -> t`` (``returns[0]`` is ignored).  Non-finite
        entries (gap / different listing history) are held flat (0 return).
    net_step:
        ``(T-1,)`` netted per-bar portfolio return, ``net_step[s] = Σ_k
        positions[s, k]·returns[s+1, k]`` (the pre-cost return).

    Returns
    -------
    ``(T-1,)`` turnover aligned 1:1 to ``net_step``: ``turnover[s]`` is the
    turnover of the trade that ESTABLISHES ``positions[s]`` (held over step
    ``s -> s+1``), so charging its drag on ``net_step[s]`` charges the cost on the
    position actually held.  ``turnover[0] = Σ_k |positions[0, k]|`` is the
    initial entry from cash; ``turnover[s>=1]`` rebalances the drifted
    ``positions[s-1]`` to ``positions[s]``.  A rebalance at the very last bar is
    never held into a return step and so is not charged.
    """
    positions = np.asarray(positions, dtype=np.float64)
    returns = np.asarray(returns, dtype=np.float64)
    if positions.ndim == 1:
        positions = positions[:, None]
    if returns.ndim == 1:
        returns = returns[:, None]

    T = positions.shape[0]
    m = max(T - 1, 0)
    turnover = np.zeros(m, dtype=np.float64)
    if T == 0:
        return turnover

    # Initial entry from cash.
    turnover[0] = float(np.sum(np.abs(positions[0])))

    if T >= 3:
        prev = positions[:-2]  # positions[s-1], s = 1 .. T-2
        cur = positions[1:-1]  # positions[s]
        r = returns[1:-1]  # returns[s]  (step s-1 -> s)
        denom = 1.0 + net_step[: m - 1]  # 1 + net_step[s-1]
        ok = np.isfinite(denom) & (denom != 0.0)
        safe_denom = np.where(ok, denom, 1.0)
        r_safe = np.where(np.isfinite(r), r, 0.0)
        drift = prev * (1.0 + r_safe) / safe_denom[:, None]
        # A wiped / non-finite compounding base leaves no meaningful drifted
        # weight -- charge nothing there (positions are typically already flat).
        drift = np.where(ok[:, None], drift, 0.0)
        turnover[1:] = np.sum(np.abs(cur - drift), axis=1)

    return turnover


def roll_turnover_from_flags(
    is_roll: npt.NDArray[np.bool_],
    nav_times: float,
    n_steps: int,
) -> npt.NDArray[np.float64]:
    """Per-step turnover from a held leg's rolls (round-trip = 2 sides).

    A held option/continuous leg trades its ``nav_times`` notional fraction at
    each roll.  The initial open (the FIRST roll flag) is a single side; every
    subsequent roll is a round-trip (2 sides).  The turnover of a roll at bar
    ``s`` is charged on the step ``s -> s+1`` it opens; a roll on the very last
    bar is never held into a step and is dropped.
    """
    turnover = np.zeros(max(n_steps, 0), dtype=np.float64)
    roll_idx = np.flatnonzero(np.asarray(is_roll, dtype=bool))
    mag = abs(float(nav_times))
    for i, s in enumerate(roll_idx):
        if s >= n_steps:
            continue
        turnover[s] += (1.0 if i == 0 else 2.0) * mag
    return turnover


def split_drag(
    turnover: npt.NDArray[np.float64],
    cfg: CostConfig,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Per-step ``(slippage_drag, fees_drag)`` return reductions from turnover."""
    turnover = np.asarray(turnover, dtype=np.float64)
    return cfg.slippage_rate * turnover, cfg.fees_rate * turnover


def cumulative_cost_pct(
    drag: npt.NDArray[np.float64],
    equity_ratio_start: npt.NDArray[np.float64],
) -> float:
    """Cumulative cost as PERCENT of initial capital.

    ``drag[s]`` is a fraction-of-equity return reduction on step ``s``; the
    capital deployed over that step is ``equity_ratio_start[s]`` (the running
    equity at the START of the step, normalised so the initial capital is 1.0).
    The dollar cost of step ``s`` is therefore ``drag[s]·equity_ratio_start[s]``
    (in units of initial capital); summed and rendered as a percent.
    """
    drag = np.asarray(drag, dtype=np.float64)
    er = np.asarray(equity_ratio_start, dtype=np.float64)
    return 100.0 * float(np.sum(drag * er))
