"""Intraday (1-minute) reads from the dwh v2 star schema ``tcg_instruments_v2``.

Unlike :meth:`SqlInstrumentReaderV2.read_serie_facts`, every method here PRESERVES
the full ``timestamptz`` (the recon flags that the shared reader truncates ``ts``
to a date via ``_ts_to_int`` — unusable for an intraday strategy). The read-only
``tcg_read`` pool (:class:`DwhConnectionPool`) is reused; the schema is bound
per-statement. All fact queries bound ``ts`` with a constant ``>= lower AND <
upper`` range so the planner prunes / BRIN-scans (warehouse gotcha).

Star: ``object -> contract -> serie -> fact_bar / fact_bbba``. The ES option
complex (``kind='option'``, symbol ``OPT_SP_500_*``) and the ES future
(``FUT_SP_500``) carry ``freq='1m'`` series (recon §1/§2/§6).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from tcg.data._sql.connection import DwhConnectionPool, to_float
from tcg.types.errors import DataAccessError
from tcg.types.intraday import (
    ES_FUTURE_TICK_SIZE,
    ES_OPTION_TICK_SIZE,
    IntradayBar,
    WINDOW_MAX_DATE,
    WINDOW_MIN_DATE,
)

logger = logging.getLogger(__name__)

V2_SCHEMA = "tcg_instruments_v2"

_FUTURE_SYMBOL = "FUT_SP_500"
_OPTION_SYMBOL_PREFIX = "OPT_SP_500"


def _clean_two_sided(bid: float | None, ask: float | None) -> bool:
    """Usable UNCROSSED two-sided quote: both sides present, bid > 0, ask > bid.

    Rejects crossed / locked books (``ask <= bid``) so an inverted top-of-book
    never fabricates a mid (Gap 4b). A rejected quote leaves the bar's bid/ask
    ``None``, degrading it to the trade-bar close, which then fails the
    downstream ``max_spread`` / ``min_quote_size`` guards rather than being
    silently accepted at a nonsensical mid.
    """
    return bid is not None and ask is not None and bid > 0 and ask > bid


class IntradayV2Reader:
    """Read-only 1m intraday adapter over ``tcg_instruments_v2`` (full ts)."""

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ #
    # Dimension / metadata
    # ------------------------------------------------------------------ #
    async def available_window(self) -> tuple[date, date]:
        """Return the intraday-options coverage window.

        Pinned from the recon (§2/§6): the intersection of the ES future and ES
        option 1m coverage. Returned as constants rather than a ``MIN(ts)/MAX(ts)``
        over the ~19M-row option fact table (an unbounded scan the warehouse
        gotchas forbid) — the window is a fixed property of the loaded feed.
        """
        return WINDOW_MIN_DATE, WINDOW_MAX_DATE

    async def get_option_tick_size(self) -> float:
        """Minimum price increment (index points) for the ES option complex.

        Sourced from a dwh min-increment column **if one existed** — but a
        scan of ``information_schema.columns`` for ``tcg_instruments_v2``
        (contract / object / serie) found NO tick / increment / min_move
        column. So the documented CME ES-option constant is used
        (:data:`ES_OPTION_TICK_SIZE` = 0.05 index pts). See PROBLEMS.md. This
        method is the single sourcing point: if such a column is added later,
        wire the read here with zero call-site change.
        """
        return ES_OPTION_TICK_SIZE

    async def get_es_future_tick_size(self) -> float:
        """Minimum price increment (index points) for the ES FUTURE hedge leg.

        Same sourcing rationale as :meth:`get_option_tick_size` — no dwh
        min-increment column exists — so the documented CME ES-future constant
        is used (:data:`ES_FUTURE_TICK_SIZE` = 0.25 index pts). Used by the hedge
        module's ``max_spread`` 1-tick floor on the ES-future bar. Single
        sourcing point; wire a real column read here with zero call-site change.
        """
        return ES_FUTURE_TICK_SIZE

    async def resolve_future_object_id(self) -> int | None:
        """Object id of the ES future (``FUT_SP_500``), or ``None``."""
        row = await self._fetch_one(
            f"""SELECT object_id FROM {V2_SCHEMA}.object
                WHERE symbol = %s AND kind = 'future' LIMIT 1""",
            (_FUTURE_SYMBOL,),
        )
        return int(row["object_id"]) if row else None

    async def list_option_roots(self) -> list[dict]:
        """List the ES option-complex roots (``OPT_SP_500_*``)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT object_id, symbol, name
                            FROM {V2_SCHEMA}.object
                            WHERE kind = 'option' AND symbol LIKE %s
                            ORDER BY symbol""",
                        (f"{_OPTION_SYMBOL_PREFIX}%",),
                    )
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 intraday error listing option roots: {exc}") from exc

    async def list_expirations(
        self, option_object_ids: list[int], min_date: date
    ) -> list[tuple[int, date]]:
        """Return ``(object_id, expiration)`` pairs at/after *min_date*, sorted.

        The option-root -> expiration map for the chain. Reads the ``contract``
        dimension only (no fact scan); one row per listed expiration.
        """
        if not option_object_ids:
            return []
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT object_id, expiration
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = ANY(%s)
                              AND expiration IS NOT NULL
                              AND expiration >= %s
                              AND strike IS NOT NULL
                            ORDER BY expiration, object_id""",
                        (list(option_object_ids), min_date),
                    )
                    return [(int(r["object_id"]), r["expiration"]) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 intraday error listing expirations: {exc}") from exc

    async def list_strikes(self, object_id: int, expiration: date) -> list[float]:
        """Distinct call-side strikes for one root + expiration (the ATM ladder)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT strike
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s AND expiration = %s
                              AND option_type = 'call' AND strike IS NOT NULL
                            ORDER BY strike""",
                        (object_id, expiration),
                    )
                    return [to_float(r["strike"]) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 intraday error listing strikes for object {object_id}: {exc}"
            ) from exc

    async def get_option_contract_id(
        self, object_id: int, expiration: date, strike: float, option_type: str
    ) -> int | None:
        """Resolve one option contract id (root + expiry + strike + type)."""
        row = await self._fetch_one(
            f"""SELECT contract_id FROM {V2_SCHEMA}.contract
                WHERE object_id = %s AND expiration = %s
                  AND option_type = %s AND strike = %s
                LIMIT 1""",
            (object_id, expiration, option_type, strike),
        )
        return int(row["contract_id"]) if row else None

    # ------------------------------------------------------------------ #
    # Fact reads (full timestamptz preserved)
    # ------------------------------------------------------------------ #
    async def fetch_es_future_1m(
        self, start_ts: datetime, end_ts: datetime, on_or_after: date
    ) -> list[IntradayBar]:
        """Front ES-future 1m mark series over ``[start_ts, end_ts)``.

        Picks the FRONT contract (nearest expiration STRICTLY AFTER *on_or_after*
        that has bars in the window; Gap 4a — a contract expiring ON the trade
        day is a stale/settling series, not the active front, so ``>`` not
        ``>=``) and MERGES its trade bars (``fact_bar`` close) with
        its top-of-book quotes (``fact_bbba`` — the ES ``bbo-1m`` grid, mapped to
        ``serie.type='bbba'``) into one event-ordered series. Per bar the
        returned :class:`IntradayBar` carries:

        * ``price`` — the MARK: two-sided bbba mid when both sides present, else
          the trade-bar close (mirrors :meth:`fetch_option_1m`).
        * ``bid`` / ``ask`` / ``bid_size`` / ``ask_size`` — the ES top-of-book at
          a two-sided bbba event; **all ``None`` on a trade-only bar** (the
          fields the hedge ``max_spread`` / ``min_quote_size`` conditions read).

        ``ts`` constant-bounded on both reads (BRIN prune). Caveat: across a
        quarterly roll the "front" can differ between windows; for one intraday
        day-window this is the active front month.
        """
        future_id = await self.resolve_future_object_id()
        if future_id is None:
            return []
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT c.expiration, s.contract_id, f.ts, f.close
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_bar f ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'bar' AND s.freq = '1m'
                              AND c.expiration > %s
                              AND f.close > 0
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY c.expiration, f.ts""",
                        (future_id, on_or_after, start_ts, end_ts),
                    )
                    bar_rows = await cur.fetchall()
                    if not bar_rows:
                        return []
                    # Front contract = smallest expiration present in the window.
                    front_exp = bar_rows[0]["expiration"]
                    front_cids = sorted(
                        {r["contract_id"] for r in bar_rows if r["expiration"] == front_exp}
                    )
                    await cur.execute(
                        f"""SELECT f.ts, f.best_bid_value, f.best_ask_value,
                                   f.best_bid_volume, f.best_ask_volume
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.fact_bbba f ON f.serie_id = s.serie_id
                            WHERE s.contract_id = ANY(%s)
                              AND s.type = 'bbba' AND s.freq = '1m'
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts""",
                        (front_cids, start_ts, end_ts),
                    )
                    bbba_rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 intraday error fetching ES future: {exc}") from exc

        # ts -> IntradayBar. Trade-bar close seeds a quote-less mark; a two-sided
        # bbba event OVERRIDES with mid + full top-of-book (same merge as options).
        marks: dict[datetime, IntradayBar] = {}
        for r in bar_rows:
            if r["expiration"] != front_exp:
                continue
            close = to_float(r["close"])
            if close is not None and close > 0:
                marks[r["ts"]] = IntradayBar(ts=r["ts"], price=close)
        for r in bbba_rows:
            bid = to_float(r["best_bid_value"])
            ask = to_float(r["best_ask_value"])
            if _clean_two_sided(bid, ask):
                marks[r["ts"]] = IntradayBar(
                    ts=r["ts"],
                    price=(bid + ask) / 2.0,
                    bid=bid,
                    ask=ask,
                    bid_size=to_float(r["best_bid_volume"]),
                    ask_size=to_float(r["best_ask_volume"]),
                )
        return [marks[ts] for ts in sorted(marks)]

    async def fetch_option_1m(
        self, contract_id: int, start_ts: datetime, end_ts: datetime
    ) -> list[IntradayBar]:
        """One contract's 1m marks over ``[start_ts, end_ts)``.

        Merges the ``fact_bbba`` (top-of-book) and ``fact_bar`` (trade) 1m events
        into a single event-ordered mark series. Per event, the returned
        :class:`IntradayBar` carries:

        * ``price`` — the MARK: two-sided bbba mid ``(best_bid+best_ask)/2`` when
          both sides present, else the bar close (recon §3).
        * ``bid`` / ``ask`` / ``bid_size`` / ``ask_size`` — the top-of-book at a
          two-sided bbba event; **all ``None`` on a last-trade-only bar** (no
          two-sided quote — the fields the ``max_spread`` / ``min_quote_size``
          conditions require). ``ts`` constant-bounded on both reads.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.ts, f.best_bid_value, f.best_ask_value,
                                   f.best_bid_volume, f.best_ask_volume
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.fact_bbba f ON f.serie_id = s.serie_id
                            WHERE s.contract_id = %s
                              AND s.type = 'bbba' AND s.freq = '1m'
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts""",
                        (contract_id, start_ts, end_ts),
                    )
                    bbba_rows = await cur.fetchall()
                    await cur.execute(
                        f"""SELECT f.ts, f.close
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.fact_bar f ON f.serie_id = s.serie_id
                            WHERE s.contract_id = %s
                              AND s.type = 'bar' AND s.freq = '1m'
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts""",
                        (contract_id, start_ts, end_ts),
                    )
                    bar_rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 intraday error fetching option contract {contract_id}: {exc}"
            ) from exc

        # ts -> IntradayBar. bar close seeds a last-trade-only mark (quote fields
        # None); a two-sided bbba event OVERRIDES with mid + full top-of-book.
        marks: dict[datetime, IntradayBar] = {}
        for r in bar_rows:
            close = to_float(r["close"])
            if close is not None and close > 0:
                marks[r["ts"]] = IntradayBar(ts=r["ts"], price=close)
        for r in bbba_rows:
            bid = to_float(r["best_bid_value"])
            ask = to_float(r["best_ask_value"])
            if _clean_two_sided(bid, ask):
                marks[r["ts"]] = IntradayBar(
                    ts=r["ts"],
                    price=(bid + ask) / 2.0,
                    bid=bid,
                    ask=ask,
                    bid_size=to_float(r["best_bid_volume"]),
                    ask_size=to_float(r["best_ask_volume"]),
                )
        return [marks[ts] for ts in sorted(marks)]

    async def fetch_future_settlement(
        self, start: date, end: date
    ) -> dict[date, float]:
        """Front-quarterly ES-future SETTLEMENT per settlement date (Gap 1).

        The 0DTE ES options cash-settle against the front ES future's
        settlement level ``|F_settle - K|``. This returns ``{settlement_date:
        F_settle}`` over ``[start, end]`` reading the settlement grid
        (``serie.type='value'`` -> ``fact_value.value`` on the ``FUT_SP_500``
        object, resolved via :meth:`resolve_future_object_id` — NOT hardcoded).

        Fold: among ``value > 0`` rows on a settlement date ``d``, the FRONT
        quarterly is the smallest contract expiration STRICTLY AFTER ``d`` (Gap
        4a — a contract expiring ON ``d`` is a stale/settling series); if none is
        after ``d`` (all expired), the smallest expiration is used. This is a
        settlement LEVEL, not a fill: NO spread is applied. ``ts`` is
        constant-bounded (BRIN prune); the range runs from ``start`` 00:00Z to
        ``end`` + 2 days to capture the last day's settlement stamp.
        """
        future_id = await self.resolve_future_object_id()
        if future_id is None:
            return {}
        lower = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        upper = (
            datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
            + timedelta(days=2)
        )
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT c.expiration, f.ts, f.value
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_value f ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'value'
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY c.expiration, f.ts""",
                        (future_id, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 intraday error fetching future settlement: {exc}"
            ) from exc

        by_date: dict[date, list[tuple[date, float]]] = {}
        for r in rows:
            value = to_float(r["value"])
            if value is None or value <= 0:
                continue
            exp = r["expiration"]
            settle_date = r["ts"].astimezone(timezone.utc).date()
            by_date.setdefault(settle_date, []).append((exp, value))

        settlement: dict[date, float] = {}
        for settle_date, lst in by_date.items():
            front = [(e, v) for e, v in lst if e is not None and e > settle_date]
            if front:
                settlement[settle_date] = min(front, key=lambda x: x[0])[1]
            else:
                settlement[settle_date] = min(
                    lst, key=lambda x: x[0] or date.max
                )[1]
        return settlement

    # ------------------------------------------------------------------ #
    async def _fetch_one(self, sql: str, params: tuple) -> dict | None:
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 intraday query error: {exc}") from exc
