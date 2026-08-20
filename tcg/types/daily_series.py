"""Frozen result type for a generic daily external market series.

Dependency-free (only stdlib + this layer) so both ``tcg.engine`` and
``tcg.core`` may import it without violating the module-boundary contract
(types <- data/engine <- core). The data layer's :class:`DailySeriesReader`
emits this dataclass; downstream code (e.g. the F2.1 realized-vol computation)
consumes it.

This is the ONE typed shape a daily dwh series (VVIX now, VIX1D when it lands,
and ``IND_SP_500`` daily closes from which realized vol is derived) flows
through. It carries a RAW ordered value series only — NO realized-vol, regime,
or any other derived logic lives here. Deriving RV / regime from these points
is a separate (F2.1) concern.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DailySeriesPoint:
    """One daily observation: a ``YYYYMMDD`` date int and a float value.

    ``date`` is an ``int`` (e.g. ``20250131``) — the same JSON-friendly date
    representation the rest of the daily market types use (``EquityPoint``,
    ``PriceSeries``) — so the point round-trips through JSON with no custom
    encoder. ``value`` is the series value at that date (already Decimal->float
    coerced at the SQL boundary).
    """

    date: int
    value: float


@dataclass(frozen=True)
class DailySeries:
    """An ordered daily value series for one dwh symbol over a date range.

    The generic seam every daily external series is read through: ``symbol`` is
    the dwh ``dim_instrument.symbol`` (e.g. ``IND_VVIX``, ``IND_SP_500``,
    ``IND_VIX1D`` when it lands), ``field`` records which fact column the value
    came from (default the close), and ``points`` is the value series ordered by
    ascending date (may be EMPTY when the symbol has no rows in the range — an
    empty series is a valid, well-formed result, never an error).

    Frozen + all-primitive fields => hashable, JSON-friendly, safe to cache and
    to feed a deterministic cache key.
    """

    symbol: str
    field: str
    points: tuple[DailySeriesPoint, ...] = ()

    @property
    def dates(self) -> list[int]:
        """The ``YYYYMMDD`` date ints, ascending (parallel to :attr:`values`)."""
        return [p.date for p in self.points]

    @property
    def values(self) -> list[float]:
        """The float values, ascending by date (parallel to :attr:`dates`)."""
        return [p.value for p in self.points]

    def __len__(self) -> int:
        return len(self.points)
