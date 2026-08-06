"""v2 star-schema reads for the index and ES futures, shaped as v1 DTOs.

Read-only, parameterized, on the existing ``tcg_read`` pool. The schema is bound
per-query rather than by a second pool.

Three invariants this module exists to hold — each one was a live-measured trap:

1. **``serie.freq = 'daily'`` on every bar query.** ``FUT_SP_500`` carries both
   ``freq='1m'`` (968,538 rows at REAL intraday timestamps) and ``freq='daily'``
   (17,121 rows at 00:00Z). Without the filter you get 57x the rows at
   non-midnight ts and the YYYYMMDD conversion silently collapses them onto
   duplicate dates.

2. **Futures ``close`` is the SETTLEMENT** (``fact_value.value``), not
   ``fact_bar.close``. Live-paired over 239 contract-days: settlement matched v1
   ``close`` exactly on 238/239, while ``fact_bar.close`` matched 4/239 and was
   off by a median of 9.25 index points. ``fact_bar`` is Databento's last
   *traded* price — a different quantity.

3. **Pivot through ``serie.contract_id``, never ``serie_id``.** One contract has
   a DIFFERENT ``serie_id`` per ``serie.type``, so joining
   ``fact_value.serie_id = fact_bar.serie_id`` returns zero rows.

Consequence of (2): the futures grid is the SETTLEMENT grid, left-joined to
daily bars. Only ~40% of settlement contract-days have a matching daily bar, so
``open``/``high``/``low``/``volume`` are ``NaN`` on the majority of futures
bars. ``NaN`` (not ``0.0``) because a missing ROW is not a NULL column: 0.0 is a
plausible-looking price that would quietly corrupt any high/low comparison,
whereas NaN propagates loudly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from tcg.data._sql.connection import DwhConnectionPool, to_float, to_float_or
from tcg.data._utils import date_to_int, int_to_date
from tcg.data._v2_compat._mapping import (
    V2_FUTURES_OBJECT_ID,
    V2_INDEX_OBJECT_ID,
    date_int_bounds,
    futures_symbol_from_expiration,
    ts_to_date_int,
)
from tcg.types.errors import DataAccessError
from tcg.types.market import ContractPriceData, FuturesContractMeta, PriceSeries

V2_SCHEMA = "tcg_instruments_v2"

# v1 stores the empty string for every FUT_SP_500 contract's expiration_cycle.
# Mirrored verbatim (NOT the v2 object-level 'quarterly', NOT None) because the
# END_OF_MONTH collapse compares this against 'M'.
V1_FUTURES_CYCLE = ""


async def read_index_prices(
    pool: DwhConnectionPool,
    *,
    start: date | None = None,
    end: date | None = None,
) -> PriceSeries | None:
    """Read the S&P 500 index daily bars (object 5) → ``PriceSeries``.

    The index is the one v2 series with a full OHLCV bar and no contract, so
    all five fields come straight from ``fact_bar``. Returns ``None`` when the
    window holds no rows (matching ``DefaultMarketDataService.get_prices``).
    """
    lo, hi = date_int_bounds(start, end)
    sql = f"""
        SELECT f.ts, f.open, f.high, f.low, f.close, f.volume
        FROM {V2_SCHEMA}.serie s
        JOIN {V2_SCHEMA}.fact_bar f ON f.serie_id = s.serie_id
        WHERE s.object_id = %s
          AND s.type = 'bar'
          AND s.freq = 'daily'
          AND f.ts >= %s AND f.ts < %s
        ORDER BY f.ts
    """
    rows = await _fetch(pool, sql, (V2_INDEX_OBJECT_ID, lo, hi), "index prices")
    if not rows:
        return None
    return PriceSeries(
        dates=np.array([ts_to_date_int(r["ts"]) for r in rows], dtype=np.int64),
        open=_col(rows, "open"),
        high=_col(rows, "high"),
        low=_col(rows, "low"),
        close=_col(rows, "close"),
        volume=_col(rows, "volume"),
    )


async def read_futures_contract_rows(
    pool: DwhConnectionPool,
    *,
    expiration_int: int | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """Read ES futures bars on the SETTLEMENT grid, left-joined to daily bars.

    One row per (contract, settlement date). ``close`` is the settlement;
    ``open``/``high``/``low``/``volume`` are ``None`` where that contract-day
    has no daily bar. Pass *expiration_int* to scope to a single contract.
    """
    lo, hi = date_int_bounds(start, end)
    params: list[Any] = [V2_FUTURES_OBJECT_ID, lo, hi, V2_FUTURES_OBJECT_ID, lo, hi]
    contract_clause = ""
    if expiration_int is not None:
        contract_clause = " AND c.expiration = %s"
        params.append(int_to_date(expiration_int))

    # settle/bars pivot on contract_id (Sign 7). The bars CTE carries the
    # freq='daily' filter that keeps the 1m series out.
    sql = f"""
        WITH settle AS (
            SELECT s.contract_id, f.ts, f.value
            FROM {V2_SCHEMA}.serie s
            JOIN {V2_SCHEMA}.fact_value f ON f.serie_id = s.serie_id
            WHERE s.object_id = %s
              AND s.type = 'value'
              AND f.ts >= %s AND f.ts < %s
        ),
        bars AS (
            SELECT s.contract_id, f.ts,
                   f.open, f.high, f.low, f.volume
            FROM {V2_SCHEMA}.serie s
            JOIN {V2_SCHEMA}.fact_bar f ON f.serie_id = s.serie_id
            WHERE s.object_id = %s
              AND s.type = 'bar'
              AND s.freq = 'daily'
              AND f.ts >= %s AND f.ts < %s
        )
        SELECT c.expiration, v.ts, v.value AS close,
               b.open, b.high, b.low, b.volume
        FROM settle v
        JOIN {V2_SCHEMA}.contract c ON c.contract_id = v.contract_id
        LEFT JOIN bars b
               ON b.contract_id = v.contract_id AND b.ts = v.ts
        WHERE c.expiration IS NOT NULL{contract_clause}
        ORDER BY c.expiration, v.ts
    """
    return await _fetch(pool, sql, tuple(params), "futures bars")


async def read_futures_prices(
    pool: DwhConnectionPool,
    expiration_int: int,
    *,
    start: date | None = None,
    end: date | None = None,
) -> PriceSeries | None:
    """Read one ES futures contract → ``PriceSeries`` (settlement grid)."""
    rows = await read_futures_contract_rows(
        pool, expiration_int=expiration_int, start=start, end=end
    )
    if not rows:
        return None
    return _rows_to_series(rows)


async def read_futures_contracts(
    pool: DwhConnectionPool,
) -> list[ContractPriceData]:
    """Every ES contract's settlement series → roller input, expiration-sorted.

    Feeds the UNCHANGED ``ContinuousSeriesBuilder``. The whole history is pulled
    (the roller trims) but ``ts`` is still constant-bounded so BRIN applies.
    ``expiration_cycle`` is stamped ``''`` to mirror v1 exactly.
    """
    rows = await read_futures_contract_rows(pool)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(date_to_int(r["expiration"]), []).append(r)

    contracts = [
        ContractPriceData(
            contract_id=futures_symbol_from_expiration(exp_int),
            expiration=exp_int,
            expiration_cycle=V1_FUTURES_CYCLE,
            prices=_rows_to_series(bucket),
        )
        for exp_int, bucket in grouped.items()
        if bucket
    ]
    # The roller REQUIRES ascending expiration order.
    contracts.sort(key=lambda c: c.expiration)
    return contracts


async def list_futures_contract_meta(
    pool: DwhConnectionPool,
) -> list[FuturesContractMeta]:
    """Contract dimension only (no fact join) — symbol/expiration/multiplier.

    v2 states ``multiplier`` live for 100% of contracts (uniformly 50.0), where
    v1 is NULL for 93 of 104 and falls back to the signed-off config table. Both
    resolve to 50.0, so sizing is identical; only the provenance string differs.
    """
    sql = f"""
        SELECT expiration, multiplier
        FROM {V2_SCHEMA}.contract
        WHERE object_id = %s AND expiration IS NOT NULL
        ORDER BY expiration ASC, contract_id ASC
    """
    rows = await _fetch(pool, sql, (V2_FUTURES_OBJECT_ID,), "futures contract meta")
    return [
        FuturesContractMeta(
            symbol=futures_symbol_from_expiration(date_to_int(r["expiration"])),
            expiration=r["expiration"],
            contract_size=to_float(r["multiplier"]),
            expiration_cycle=V1_FUTURES_CYCLE,
        )
        for r in rows
    ]


async def list_futures_expirations(pool: DwhConnectionPool) -> list[date]:
    """Ascending distinct ES contract expirations (dimension read only)."""
    sql = f"""
        SELECT DISTINCT expiration
        FROM {V2_SCHEMA}.contract
        WHERE object_id = %s AND expiration IS NOT NULL
        ORDER BY expiration
    """
    rows = await _fetch(pool, sql, (V2_FUTURES_OBJECT_ID,), "futures expirations")
    return [r["expiration"] for r in rows]


# --- internals ------------------------------------------------------------- #


async def _fetch(
    pool: DwhConnectionPool,
    sql: str,
    params: tuple[Any, ...],
    what: str,
) -> list[dict[str, Any]]:
    """Run one read-only query, wrapping driver failures as DataAccessError."""
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return [dict(r) for r in await cur.fetchall()]
    except DataAccessError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DataAccessError(f"v2 SQL error reading {what}: {exc}") from exc


def _col(rows: list[dict[str, Any]], field: str) -> np.ndarray:
    """Column → float64 array, NULL → 0.0 (v1's convention for a present row)."""
    return np.array([to_float_or(r[field], 0.0) for r in rows], dtype=np.float64)


def _col_nan(rows: list[dict[str, Any]], field: str) -> np.ndarray:
    """Column → float64 array, absent row → NaN.

    Used for futures O/H/L/volume, where a NULL means "this contract-day has no
    daily bar at all" rather than "one column of an existing bar was NULL".
    """
    return np.array(
        [to_float_or(r[field], float("nan")) for r in rows], dtype=np.float64
    )


def _rows_to_series(rows: list[dict[str, Any]]) -> PriceSeries:
    """Settlement-grid rows → ``PriceSeries`` (close settled, O/H/L/V NaN-able)."""
    return PriceSeries(
        dates=np.array([ts_to_date_int(r["ts"]) for r in rows], dtype=np.int64),
        open=_col_nan(rows, "open"),
        high=_col_nan(rows, "high"),
        low=_col_nan(rows, "low"),
        close=_col(rows, "close"),
        volume=_col_nan(rows, "volume"),
    )
