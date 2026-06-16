"""SQL read adapters for non-option market data (prices, instruments, continuous futures).

Replaces tcg.data._mongo.instruments.MongoInstrumentReader.
All queries are read-only, parameterized (%s binding), and push filtering to the
database. Every gotcha from the recon synthesis is handled here.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from psycopg import AsyncConnection

from tcg.data._sql.connection import DwhConnectionPool
from tcg.types.errors import DataAccessError, DataNotFoundError
from tcg.types.market import ContractPriceData, InstrumentId, PriceSeries
from tcg.data._utils import date_to_int, int_to_date

import numpy as np

logger = logging.getLogger(__name__)

SCHEMA = "tcg_instruments"


class SqlInstrumentReader:
    """Read-only SQL adapter for market data (prices, instruments, futures contracts).

    Used exclusively by the SQL-backed MarketDataService. All queries go to
    PostgreSQL dwh schema tcg_instruments with read-only enforcement.
    """

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    async def list_instruments(
        self,
        collection: str,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[InstrumentId], int]:
        """List instruments in *collection* (by source_collection) with pagination.

        Returns (instruments, total_count).
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Count total (no pagination)
                    cur.execute(
                        f"SELECT count(*) FROM {SCHEMA}.dim_instrument WHERE source_collection = %s",
                        (collection,),
                    )
                    total = cur.fetchone()[0]

                    # Fetch page (exclude heavy columns)
                    cur.execute(
                        f"""SELECT symbol, asset_class, source_collection
                           FROM {SCHEMA}.dim_instrument
                           WHERE source_collection = %s
                           ORDER BY symbol
                           OFFSET %s LIMIT %s""",
                        (collection, skip, limit),
                    )
                    instruments: list[InstrumentId] = []
                    for row in cur.fetchall():
                        symbol, asset_class_str, source_coll = row
                        # Map asset_class string back to enum
                        if asset_class_str == "future":
                            asset_class = "future"
                        elif asset_class_str == "index":
                            asset_class = "index"
                        else:
                            asset_class = "equity"  # etf, fund, forex
                        instruments.append(
                            InstrumentId(
                                symbol=symbol,
                                asset_class=asset_class,
                                collection=source_coll,
                            )
                        )
                    return instruments, total
        except Exception as exc:
            raise DataAccessError(
                f"SQL error listing instruments in '{collection}': {exc}"
            ) from exc

    async def read_prices(
        self,
        collection: str,
        instrument_id: str,
        *,
        provider: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> PriceSeries | None:
        """Fetch OHLCV data for a single instrument (by symbol).

        [Gotcha 2] Reads from parent fact_price_eod, filtered by trade_date
        (planner prunes yearly partitions). [Gotcha 3] close is NOT NULL by schema;
        adj_close may be NULL → COALESCE(adj_close, close).
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Bind dates as ISO strings for the WHERE clause
                    start_int = date_to_int(start) if start else 0
                    end_int = date_to_int(end) if end else 99999999

                    cur.execute(
                        f"""SELECT f.trade_date,
                                  COALESCE(f.adj_close, f.close) AS close_val,
                                  f.open, f.high, f.low, f.volume
                           FROM {SCHEMA}.fact_price_eod f
                           JOIN {SCHEMA}.dim_instrument d ON d.instrument_id = f.instrument_id
                           WHERE d.source_collection = %s
                             AND d.symbol = %s
                             AND f.trade_date BETWEEN %s AND %s
                           ORDER BY f.trade_date""",
                        (
                            collection,
                            instrument_id,
                            start if start else date(1900, 1, 1),
                            end if end else date(2100, 12, 31),
                        ),
                    )

                    rows = cur.fetchall()
                    if not rows:
                        return None

                    # Build arrays (all trades within the date range)
                    dates_list = []
                    opens = []
                    highs = []
                    lows = []
                    closes = []
                    volumes = []

                    for (
                        trade_date,
                        close_val,
                        open_val,
                        high_val,
                        low_val,
                        volume_val,
                    ) in rows:
                        dates_list.append(date_to_int(trade_date))
                        closes.append(
                            float(close_val) if close_val is not None else 0.0
                        )
                        opens.append(float(open_val) if open_val is not None else 0.0)
                        highs.append(float(high_val) if high_val is not None else 0.0)
                        lows.append(float(low_val) if low_val is not None else 0.0)
                        volumes.append(
                            float(volume_val) if volume_val is not None else 0.0
                        )

                    return PriceSeries(
                        dates=np.array(dates_list, dtype=np.int64),
                        open=np.array(opens, dtype=np.float64),
                        high=np.array(highs, dtype=np.float64),
                        low=np.array(lows, dtype=np.float64),
                        close=np.array(closes, dtype=np.float64),
                        volume=np.array(volumes, dtype=np.float64),
                    )
        except Exception as exc:
            raise DataAccessError(
                f"SQL error reading prices for '{instrument_id}' in '{collection}': {exc}"
            ) from exc

    async def fetch_futures_contracts(
        self,
        collection: str,
        *,
        cycle: str | None = None,
    ) -> list[ContractPriceData]:
        """Fetch all futures contracts in a collection, ordered by expiration.

        [Gotcha 1] Pulls all contracts for this root (source_collection),
        ordered by expiration. Each contract = one ContractPriceData with
        prices aggregated per contract symbol. [Gotcha 2] Reads parent
        fact_price_eod. [Gotcha 3] COALESCE(adj_close, close).
        [Gotcha special] Strip zero-close rows (handled by the roller).
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Build WHERE clause for cycle filter if provided
                    where_cycle = ""
                    params: list[Any] = [collection]
                    if cycle is not None:
                        where_cycle = " AND d.expiration_cycle = %s"
                        params.append(cycle)

                    # Fetch all contracts for this collection, ordered by expiration
                    # (this ordering is CRITICAL for the roller)
                    cur.execute(
                        f"""SELECT d.symbol, d.expiration, f.trade_date,
                                  COALESCE(f.adj_close, f.close) AS close_val,
                                  f.open, f.high, f.low, f.volume
                           FROM {SCHEMA}.fact_price_eod f
                           JOIN {SCHEMA}.dim_instrument d ON d.instrument_id = f.instrument_id
                           WHERE d.source_collection = %s {where_cycle}
                           ORDER BY d.expiration, f.trade_date""",
                        params,
                    )

                    # Group rows by contract symbol (expiration)
                    contracts_dict: dict[
                        str,
                        tuple[int, list[tuple[int, float, float, float, float, float]]],
                    ] = {}

                    for (
                        symbol,
                        expiration,
                        trade_date,
                        close_val,
                        open_val,
                        high_val,
                        low_val,
                        volume_val,
                    ) in cur.fetchall():
                        if symbol not in contracts_dict:
                            exp_int = date_to_int(expiration)
                            contracts_dict[symbol] = (exp_int, [])

                        exp_int, bars = contracts_dict[symbol]
                        bars.append(
                            (
                                date_to_int(trade_date),
                                float(close_val) if close_val is not None else 0.0,
                                float(open_val) if open_val is not None else 0.0,
                                float(high_val) if high_val is not None else 0.0,
                                float(low_val) if low_val is not None else 0.0,
                                float(volume_val) if volume_val is not None else 0.0,
                            )
                        )

                    # Build ContractPriceData for each contract (ordered by expiration)
                    # [Important] The roller REQUIRES contracts sorted by expiration
                    contracts: list[ContractPriceData] = []
                    for symbol in sorted(
                        contracts_dict.keys(), key=lambda s: contracts_dict[s][0]
                    ):
                        exp_int, bars = contracts_dict[symbol]
                        if not bars:
                            continue

                        # Sort bars by trade_date (should already be sorted from SQL)
                        bars.sort(key=lambda b: b[0])

                        dates_arr = np.array([b[0] for b in bars], dtype=np.int64)
                        closes = np.array([b[1] for b in bars], dtype=np.float64)
                        opens = np.array([b[2] for b in bars], dtype=np.float64)
                        highs = np.array([b[3] for b in bars], dtype=np.float64)
                        lows = np.array([b[4] for b in bars], dtype=np.float64)
                        volumes = np.array([b[5] for b in bars], dtype=np.float64)

                        contracts.append(
                            ContractPriceData(
                                contract_id=symbol,
                                expiration=exp_int,
                                prices=PriceSeries(
                                    dates=dates_arr,
                                    open=opens,
                                    high=highs,
                                    low=lows,
                                    close=closes,
                                    volume=volumes,
                                ),
                            )
                        )

                    return contracts
        except Exception as exc:
            raise DataAccessError(
                f"SQL error fetching futures contracts from '{collection}': {exc}"
            ) from exc

    async def find_contract_by_expiration(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the symbol of the single contract in *collection* whose
        expiration equals *expiration_int* (YYYYMMDD integer).

        Used by the VIX greeks resolver (find_futures_contract_by_expiration).
        Returns None when no contract matches.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Convert YYYYMMDD int to a date for comparison
                    exp_date = int_to_date(expiration_int)
                    cur.execute(
                        f"""SELECT symbol FROM {SCHEMA}.dim_instrument
                           WHERE source_collection = %s AND expiration = %s
                           LIMIT 1""",
                        (collection, exp_date),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
        except Exception as exc:
            raise DataAccessError(
                f"SQL error finding contract by expiration in '{collection}' "
                f"(expiration={expiration_int}): {exc}"
            ) from exc

    async def fetch_available_cycles(
        self,
        collection: str,
    ) -> list[str]:
        """Return distinct expiration_cycle values for a futures collection."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    cur.execute(
                        f"""SELECT DISTINCT expiration_cycle FROM {SCHEMA}.dim_instrument
                           WHERE source_collection = %s
                           ORDER BY expiration_cycle""",
                        (collection,),
                    )
                    return [row[0] for row in cur.fetchall() if row[0]]
        except Exception as exc:
            raise DataAccessError(
                f"SQL error fetching cycles from '{collection}': {exc}"
            ) from exc
