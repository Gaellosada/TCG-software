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
from datetime import date, datetime

from tcg.data._sql.connection import DwhConnectionPool, to_float
from tcg.types.errors import DataAccessError
from tcg.types.intraday import IntradayBar, WINDOW_MAX_DATE, WINDOW_MIN_DATE

logger = logging.getLogger(__name__)

V2_SCHEMA = "tcg_instruments_v2"

_FUTURE_SYMBOL = "FUT_SP_500"
_OPTION_SYMBOL_PREFIX = "OPT_SP_500"


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
        """Front ES-future 1m close series over ``[start_ts, end_ts)``.

        Picks the FRONT contract (nearest expiration >= *on_or_after* that has
        bars in the window) and returns its (ts, close) series — one clean price
        path for ATM selection and delta hedging. ``ts`` constant-bounded.
        """
        future_id = await self.resolve_future_object_id()
        if future_id is None:
            return []
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT c.expiration, f.ts, f.close
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_bar f ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'bar' AND s.freq = '1m'
                              AND c.expiration >= %s
                              AND f.close > 0
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY c.expiration, f.ts""",
                        (future_id, on_or_after, start_ts, end_ts),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 intraday error fetching ES future: {exc}") from exc

        if not rows:
            return []
        # Front contract = the SMALLEST expiration that has bars in the window.
        # Caveat: across a quarterly roll boundary the "front" can differ between
        # windows; for a single intraday day-window this is the active front month.
        front_exp = rows[0]["expiration"]  # smallest expiration present in window
        return [
            IntradayBar(ts=r["ts"], price=to_float(r["close"]))
            for r in rows
            if r["expiration"] == front_exp and to_float(r["close"]) is not None
        ]

    async def fetch_option_1m(
        self, contract_id: int, start_ts: datetime, end_ts: datetime
    ) -> list[IntradayBar]:
        """One contract's 1m marks over ``[start_ts, end_ts)``: bbba mid, else close.

        Merges the ``fact_bbba`` (top-of-book) and ``fact_bar`` (trade) 1m events
        into a single event-ordered mark series. Per event: mid =
        ``(best_bid+best_ask)/2`` when both present, else fall back to the bar
        close (recon §3). ``ts`` constant-bounded on both reads.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.ts, f.best_bid_value, f.best_ask_value
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

        # ts -> mark. bbba mid takes precedence; bar close fills only where no
        # two-sided quote exists at that ts.
        marks: dict[datetime, float] = {}
        for r in bar_rows:
            close = to_float(r["close"])
            if close is not None and close > 0:
                marks[r["ts"]] = close
        for r in bbba_rows:
            bid = to_float(r["best_bid_value"])
            ask = to_float(r["best_ask_value"])
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                marks[r["ts"]] = (bid + ask) / 2.0
        return [IntradayBar(ts=ts, price=marks[ts]) for ts in sorted(marks)]

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
