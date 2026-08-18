"""Generic daily external-series reads from the dwh ``tcg_instruments`` schema.

The single path through which ANY daily dwh series is fetched by symbol over a
date range: VVIX (``IND_VVIX``) now, VIX1D (``IND_VIX1D``) when it lands, and
``IND_SP_500`` daily closes (from which F2.1 will COMPUTE realized vol). Adding
a new daily series later is a symbol STRING at the call site — no code change
here (that drop-in property is the whole point of this seam).

This is a RAW value-series reader: input = symbol + date range (+ optional
field selector), output = an ordered :class:`DailySeries` (date -> float). It
deliberately bakes in NO realized-vol / regime / derivation logic — that is
downstream (F2.1) work. Mirrors :mod:`tcg.data._sql.intraday_v2` conventions
(read-only ``tcg_read`` pool, schema bound per-statement, ``DataAccessError``
wrapping) but reads the DAILY star (``dim_instrument`` -> ``fact_price_eod``).

Warehouse gotchas honoured:
  * [2] Read the PARENT ``fact_price_eod`` filtered on ``trade_date`` (the
    planner prunes yearly partitions) — never a ``*_YYYY`` child, and the
    ``trade_date BETWEEN`` clamp is a constant range so pruning applies.
  * [3] The default ``close`` field is ``COALESCE(adj_close, close)``:
    ``adj_close`` is YAHOO-only / sparse, ``close`` is NOT NULL by schema.
  * Decimal -> float coercion at this boundary (:func:`to_float`); a SQL NULL
    value drops the point rather than poisoning a downstream NumPy array.
  * Symbol-only filter on the durable ``dim_instrument.symbol`` (the Mongo
    ``_id``), so one call resolves any daily instrument without a collection.

NON-OPTION CAVEAT: the symbol-only filter is safe because ``dim_instrument``
carries a PARTIAL unique index, ``uq_dim_symbol_nonoption`` — ``symbol`` is
unique only across NON-OPTION rows. This reader is for daily index / indicator
/ spot series (``IND_*``, underlying closes), where that uniqueness holds and
one symbol resolves exactly one ``instrument_id``. It must NOT be pointed at
option symbols (monthly/weekly contracts can share a symbol across strikes /
expiries / cycles), or the join can silently interleave rows from more than
one instrument. Options have their own dedicated readers.
"""

from __future__ import annotations

import logging
from datetime import date

from tcg.data._sql.connection import SCHEMA, DwhConnectionPool, to_float
from tcg.data._utils import date_to_int
from tcg.types.daily_series import DailySeries, DailySeriesPoint
from tcg.types.errors import DataAccessError

logger = logging.getLogger(__name__)

# Sentinel bounds when the caller leaves start/end open. Match the dwh
# partition span (1980..2050) exactly as :class:`SqlInstrumentReader` does, so
# an open-ended read stays inside every covered yearly partition.
_MIN_DATE = date(1980, 1, 1)
_MAX_DATE = date(2050, 12, 31)

# The DEFAULT field: the daily close, adj-close-preferring per gotcha [3].
DEFAULT_FIELD = "close"

# Whitelisted ``fact_price_eod`` value columns a caller may select. A strict
# allow-list (NOT string interpolation of arbitrary caller input) keeps the
# ``field`` selector injection-proof — an unknown field raises loudly rather
# than reaching the database. Maps the public field name -> its SQL value
# expression. ``close`` COALESCEs adj_close (gotcha [3]); the rest are raw.
_FIELD_EXPR: dict[str, str] = {
    "close": "COALESCE(f.adj_close, f.close)",
    "adj_close": "f.adj_close",
    "open": "f.open",
    "high": "f.high",
    "low": "f.low",
    "volume": "f.volume",
    "bid": "f.bid",
    "ask": "f.ask",
}

#: The fields a caller may pass (public, stable) — for callers/tests to check.
ALLOWED_FIELDS = frozenset(_FIELD_EXPR)


class DailySeriesReader:
    """Read-only generic daily value-series adapter over ``tcg_instruments``.

    One instance wraps the shared read-only :class:`DwhConnectionPool`; every
    :meth:`read_series` call is an independent, targeted, partition-pruning
    read. The same reader instance is reused across symbols (VVIX, IND_SP_500,
    VIX1D, ...) — the symbol is a per-call argument, not construction state.
    """

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    async def read_series(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        field: str = DEFAULT_FIELD,
    ) -> DailySeries:
        """Fetch one symbol's daily value series over ``[start, end]`` inclusive.

        Parameters
        ----------
        symbol : str
            The dwh ``dim_instrument.symbol`` (durable id), e.g. ``IND_VVIX``,
            ``IND_SP_500``, or ``IND_VIX1D`` once it lands. Filtered exactly (no
            prefix parsing, no collection needed). NON-OPTION ONLY: ``symbol``
            is unique per ``uq_dim_symbol_nonoption`` for non-option
            instruments only — do not pass an option symbol here.
        start, end : date | None
            Inclusive date bounds. ``None`` opens that side to the dwh partition
            span. ``trade_date BETWEEN start AND end`` — a constant range the
            planner uses to prune yearly partitions (gotcha [2]).
        field : str
            Which value column to return; one of :data:`ALLOWED_FIELDS`
            (default ``"close"`` = ``COALESCE(adj_close, close)``). An unknown
            field raises :class:`DataAccessError` (never interpolated into SQL).

        Returns
        -------
        DailySeries
            Ordered ascending by date. A symbol absent in the range yields a
            well-formed EMPTY series (``points == ()``), never ``None`` and
            never an error — downstream RV/regime code degrades cleanly.

        Notes
        -----
        Rows whose selected value is SQL ``NULL`` (or NaN) are DROPPED at the
        boundary (:func:`to_float`), so ``points`` carries only usable floats —
        a missing daily observation never becomes a ``None`` in a NumPy array.
        """
        value_expr = _FIELD_EXPR.get(field)
        if value_expr is None:
            raise DataAccessError(
                f"unknown daily-series field {field!r}; "
                f"allowed: {sorted(ALLOWED_FIELDS)}"
            )

        lo = start if start is not None else _MIN_DATE
        hi = end if end is not None else _MAX_DATE

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.trade_date, {value_expr} AS value
                            FROM {SCHEMA}.fact_price_eod f
                            JOIN {SCHEMA}.dim_instrument d
                              ON d.instrument_id = f.instrument_id
                            WHERE d.symbol = %s
                              AND f.trade_date BETWEEN %s AND %s
                            ORDER BY f.trade_date""",
                        (symbol, lo, hi),
                    )
                    rows = await cur.fetchall()
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error reading daily series for {symbol!r} "
                f"(field={field!r}): {exc}"
            ) from exc

        points: list[DailySeriesPoint] = []
        for r in rows:
            value = to_float(r["value"])
            if value is None:
                continue
            points.append(
                DailySeriesPoint(date=date_to_int(r["trade_date"]), value=value)
            )
        return DailySeries(symbol=symbol, field=field, points=tuple(points))
