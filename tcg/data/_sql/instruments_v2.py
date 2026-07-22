"""SQL read adapter for the dwh v2 star schema ``tcg_instruments_v2``.

The v2 warehouse is a star: ``object -> contract -> serie -> fact_*``. A serie's
``type`` (``value`` / ``bar`` / ``greeks`` / ``bbba``) selects exactly one fact
table — nothing in the DB enforces the mapping, so every read here reads
``serie.type`` first and dispatches. All queries are read-only, parameterized,
and reuse the EXISTING ``tcg_read`` read pool (:class:`DwhConnectionPool`); the
schema is bound per-query (``V2_SCHEMA``) rather than by a second pool.

Partition/BRIN gotcha (honoured): every multi-row fact query bounds ``ts`` with a
constant ``>= lower AND < upper`` range so the planner can prune / BRIN-scan
rather than reading the whole fact table.

Decimal → float coercion happens at this boundary (NumPy/engine want floats).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

from tcg.data._sql.connection import DwhConnectionPool, to_float, to_float_or
from tcg.data._utils import date_to_int
from tcg.types.errors import DataAccessError
from tcg.types.market import ContractPriceData, PriceSeries

logger = logging.getLogger(__name__)

# Schema for every v2 query. Bound per-statement (never a second pool) so the
# same read-only ``tcg_read`` pool serves both v1 and v2.
V2_SCHEMA = "tcg_instruments_v2"

# Sentinel ``ts`` bounds when the caller leaves start/end open. Kept inside a
# generous span so the constant range still lets the planner prune.
_MIN_DATE = date(1900, 1, 1)
_MAX_DATE = date(2100, 12, 31)

# Which fact table + value columns each serie.type dispatches to. Read
# ``serie.type`` first, then look this up — the DB does not enforce the mapping.
FACT_DISPATCH: dict[str, tuple[str, tuple[str, ...]]] = {
    "bar": ("fact_bar", ("open", "high", "low", "close", "volume", "open_interest")),
    "value": ("fact_value", ("value",)),
    "greeks": (
        "fact_greeks",
        ("delta", "gamma", "theta", "vega", "rho", "implied_vol"),
    ),
    "bbba": (
        "fact_bbba",
        ("best_bid_value", "best_bid_volume", "best_ask_value", "best_ask_volume"),
    ),
}


def _ts_to_int(ts: datetime) -> int:
    """timestamptz → YYYYMMDD int (in UTC — the dwh stores daily bars at 00:00Z)."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return date_to_int(ts.date())


def _bounds(start: date | None, end: date | None) -> tuple[date, date]:
    """Return an inclusive [lower, upper_exclusive) pair of date bounds.

    ``upper`` is the day AFTER *end* so the SQL uses ``ts < upper`` and captures
    an inclusive end date regardless of intraday ts (all v2 ts are 00:00Z today,
    but this stays correct if intraday facts are ever loaded).
    """
    lower = start if start is not None else _MIN_DATE
    end_incl = end if end is not None else _MAX_DATE
    return lower, end_incl + timedelta(days=1)


class SqlInstrumentReaderV2:
    """Read-only SQL adapter for the ``tcg_instruments_v2`` star schema."""

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ #
    # Dimension reads
    # ------------------------------------------------------------------ #
    async def list_objects(self) -> list[dict[str, Any]]:
        """List every object (all kinds) with its root metadata."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT object_id, kind, symbol, name, cycle,
                                   underlying_object_id
                            FROM {V2_SCHEMA}.object
                            ORDER BY kind, symbol"""
                    )
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"v2 SQL error listing objects: {exc}") from exc

    async def get_object(self, object_id: int) -> dict[str, Any] | None:
        """Return one object row (or ``None``)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT object_id, kind, symbol, name, cycle,
                                   underlying_object_id
                            FROM {V2_SCHEMA}.object
                            WHERE object_id = %s""",
                        (object_id,),
                    )
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading object {object_id}: {exc}"
            ) from exc

    async def list_contracts(self, object_id: int) -> list[dict[str, Any]]:
        """List an object's contracts, ordered by expiration then strike."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT contract_id, contract_code, expiration, strike,
                                   option_type, multiplier
                            FROM {V2_SCHEMA}.contract
                            WHERE object_id = %s
                            ORDER BY expiration, strike NULLS FIRST, contract_id""",
                        (object_id,),
                    )
                    out: list[dict[str, Any]] = []
                    for r in await cur.fetchall():
                        out.append(
                            {
                                "contract_id": r["contract_id"],
                                "contract_code": r["contract_code"],
                                "expiration": r["expiration"].isoformat()
                                if r["expiration"]
                                else None,
                                "strike": to_float(r["strike"]),
                                "option_type": r["option_type"],
                                "multiplier": to_float(r["multiplier"]),
                            }
                        )
                    return out
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error listing contracts for object {object_id}: {exc}"
            ) from exc

    async def list_series(self, object_id: int) -> list[dict[str, Any]]:
        """List an object's series (metadata only)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT serie_id, contract_id, type, freq, source
                            FROM {V2_SCHEMA}.serie
                            WHERE object_id = %s
                            ORDER BY serie_id""",
                        (object_id,),
                    )
                    return [dict(r) for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error listing series for object {object_id}: {exc}"
            ) from exc

    async def get_serie(self, serie_id: int) -> dict[str, Any] | None:
        """Return one serie row (incl. ``type`` for fact-table dispatch)."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT serie_id, object_id, contract_id, type, freq, source
                            FROM {V2_SCHEMA}.serie
                            WHERE serie_id = %s""",
                        (serie_id,),
                    )
                    row = await cur.fetchone()
                    return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading serie {serie_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Fact reads (dispatched by serie.type)
    # ------------------------------------------------------------------ #
    async def read_serie_facts(
        self,
        serie_id: int,
        serie_type: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[list[int], dict[str, list[float | None]]]:
        """Read one serie's facts from the fact table its ``type`` dispatches to.

        Returns ``(ts_ints, {field: [values...]})`` with one list per field for
        the resolved fact table. ``ts`` is bounded with a constant range so the
        planner prunes / BRIN-scans. Raises ``DataAccessError`` on an unknown
        ``serie_type`` (should never happen — the CHECK constrains it).
        """
        dispatch = FACT_DISPATCH.get(serie_type)
        if dispatch is None:
            raise DataAccessError(
                f"v2 unknown serie.type {serie_type!r} for serie {serie_id}"
            )
        table, fields = dispatch
        lower, upper = _bounds(start, end)
        col_list = ", ".join(fields)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT ts, {col_list}
                            FROM {V2_SCHEMA}.{table}
                            WHERE serie_id = %s
                              AND ts >= %s AND ts < %s
                            ORDER BY ts""",
                        (serie_id, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error reading {table} for serie {serie_id}: {exc}"
            ) from exc

        ts_ints: list[int] = []
        cols: dict[str, list[float | None]] = {f: [] for f in fields}
        for r in rows:
            ts_ints.append(_ts_to_int(r["ts"]))
            for f in fields:
                cols[f].append(to_float(r[f]))
        return ts_ints, cols

    # ------------------------------------------------------------------ #
    # Futures continuous feed (for the reused ContinuousSeriesBuilder)
    # ------------------------------------------------------------------ #
    async def fetch_future_contract_bars(
        self,
        object_id: int,
        object_cycle: str | None,
    ) -> list[ContractPriceData]:
        """Fetch every future contract's bar series → ``ContractPriceData`` list.

        One :class:`ContractPriceData` per contract, sorted ascending by
        expiration (the ``ContinuousSeriesBuilder`` requires that ordering). Only
        ``bar``-type series are joined (a future's price lives in ``fact_bar``).
        The whole per-contract history is pulled (the roller trims); ``ts`` is
        still constant-bounded to the sentinel span so the planner can BRIN-scan.
        ``expiration_cycle`` is stamped from the object's single cycle (v2 has no
        per-contract cycle) so END_OF_MONTH collapse behaves.
        """
        lower, upper = _bounds(None, None)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT c.contract_code, c.expiration,
                                   f.ts, f.open, f.high, f.low, f.close, f.volume
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_bar f
                              ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'bar'
                              AND c.expiration IS NOT NULL
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY c.expiration, c.contract_code, f.ts""",
                        (object_id, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching future bars for object {object_id}: {exc}"
            ) from exc

        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            code = r["contract_code"]
            bucket = grouped.get(code)
            if bucket is None:
                bucket = {
                    "expiration": date_to_int(r["expiration"]),
                    "dates": [],
                    "open": [],
                    "high": [],
                    "low": [],
                    "close": [],
                    "volume": [],
                }
                grouped[code] = bucket
            bucket["dates"].append(_ts_to_int(r["ts"]))
            bucket["close"].append(to_float_or(r["close"], 0.0))
            bucket["open"].append(to_float_or(r["open"], 0.0))
            bucket["high"].append(to_float_or(r["high"], 0.0))
            bucket["low"].append(to_float_or(r["low"], 0.0))
            bucket["volume"].append(to_float_or(r["volume"], 0.0))

        contracts: list[ContractPriceData] = []
        for code, b in grouped.items():
            if not b["dates"]:
                continue
            contracts.append(
                ContractPriceData(
                    contract_id=code,
                    expiration=b["expiration"],
                    expiration_cycle=object_cycle,
                    prices=PriceSeries(
                        dates=np.array(b["dates"], dtype=np.int64),
                        open=np.array(b["open"], dtype=np.float64),
                        high=np.array(b["high"], dtype=np.float64),
                        low=np.array(b["low"], dtype=np.float64),
                        close=np.array(b["close"], dtype=np.float64),
                        volume=np.array(b["volume"], dtype=np.float64),
                    ),
                )
            )
        contracts.sort(key=lambda c: c.expiration)
        return contracts

    async def fetch_future_cycles(self, object_id: int) -> list[str]:
        """Return the object's listing cycle(s).

        v2 carries a single ``cycle`` per object (not per contract), so this is
        ``[object.cycle]`` when set, else an empty list.
        """
        obj = await self.get_object(object_id)
        if obj is None or not obj.get("cycle"):
            return []
        return [obj["cycle"]]

    # ------------------------------------------------------------------ #
    # Options selection reads (settlement values)
    # ------------------------------------------------------------------ #
    async def fetch_option_settlements(
        self,
        object_id: int,
        option_type: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch per-date settlement rows for one option object + option_type.

        Returns rows ``{ts_int, contract_id, contract_code, expiration_int,
        strike, value}`` for every ``value``-serie whose contract matches
        *option_type*, over the ``[start, end]`` window. ``ts`` is constant-
        bounded (BRIN/prune). Zero/NULL settlements are NOT filtered here — the
        resolver applies the ``> 0`` guard so it can surface dropped dates.
        """
        lower, upper = _bounds(start, end)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.ts, c.contract_id, c.contract_code,
                                   c.expiration, c.strike, f.value
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_value f
                              ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'value'
                              AND c.option_type = %s
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts, c.expiration, c.strike""",
                        (object_id, option_type, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching option settlements for object "
                f"{object_id}: {exc}"
            ) from exc

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "ts_int": _ts_to_int(r["ts"]),
                    "contract_id": r["contract_id"],
                    "contract_code": r["contract_code"],
                    "expiration_int": date_to_int(r["expiration"]),
                    "strike": to_float(r["strike"]),
                    "value": to_float(r["value"]),
                }
            )
        return out

    async def fetch_future_front_closes(
        self,
        object_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every future bar row (ts, expiration, close) for spot lookup.

        Feeds the options-continuous *moneyness* spot: the resolver picks, per
        date, the front future (nearest expiration >= that date) close. Only
        ``close > 0`` rows are returned (false-zero guard). ``ts`` constant-
        bounded.
        """
        lower, upper = _bounds(start, end)
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.ts, c.expiration, f.close
                            FROM {V2_SCHEMA}.serie s
                            JOIN {V2_SCHEMA}.contract c
                              ON c.contract_id = s.contract_id
                            JOIN {V2_SCHEMA}.fact_bar f
                              ON f.serie_id = s.serie_id
                            WHERE s.object_id = %s
                              AND s.type = 'bar'
                              AND c.expiration IS NOT NULL
                              AND f.close > 0
                              AND f.ts >= %s AND f.ts < %s
                            ORDER BY f.ts, c.expiration""",
                        (object_id, lower, upper),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"v2 SQL error fetching future front closes for object "
                f"{object_id}: {exc}"
            ) from exc

        return [
            {
                "ts_int": _ts_to_int(r["ts"]),
                "expiration_int": date_to_int(r["expiration"]),
                "close": to_float_or(r["close"], 0.0),
            }
            for r in rows
        ]
