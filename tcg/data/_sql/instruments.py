"""SQL read adapters for non-option market data (prices, instruments, continuous futures).

Replaces ``tcg.data._mongo.instruments.MongoInstrumentReader``. All queries are
read-only, parameterized (``%s`` binding), and push filtering to PostgreSQL.

Collection mapping (verified against dwh): ``dim_instrument.source_collection``
is exactly the legacy Mongo collection name (INDEX / ETF / FUND / FOREX /
FUT_* / OPT_*), and ``dim_instrument.symbol`` is the durable Mongo ``_id``.
So a "collection" filter is ``source_collection = %s`` and an instrument id is
``symbol = %s`` — no prefix parsing.

Gotchas honoured here:
  * [2] Read the PARENT ``fact_price_eod`` filtered on ``trade_date`` (the
    planner prunes yearly partitions) — never a ``*_YYYY`` child.
  * [3] ``COALESCE(adj_close, close)``: ``adj_close`` is YAHOO-only/sparse;
    ``close`` is NOT NULL by schema.
  * Decimal→float at the boundary (NumPy/engine expect float); NULL OHLV →
    ``0.0`` (matching the Mongo adapter's ``_sanitize_non_critical``), close
    falls back defensively but is never NULL.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np

from tcg.data._sql.connection import SCHEMA, DwhConnectionPool, to_float_or
from tcg.data._utils import date_to_int, int_to_date
from tcg.types.errors import DataAccessError
from tcg.types.market import (
    AssetClass,
    ContractPriceData,
    FuturesContractMeta,
    InstrumentId,
    PriceSeries,
)

logger = logging.getLogger(__name__)

# Sentinel bounds when the caller leaves start/end open. Match the dwh
# partition span (1980..2050); a wider literal would error against the
# partitioned parent only if it fell outside every partition, so we keep it
# inside the covered range.
_MIN_DATE = date(1980, 1, 1)
_MAX_DATE = date(2050, 12, 31)


def _asset_class_for(raw: str) -> AssetClass:
    """Map a dwh ``asset_class`` string onto the Mongo-era ``AssetClass`` enum.

    The Mongo registry classified ETF / FUND / FOREX all as ``EQUITY``,
    ``INDEX`` as ``INDEX``, and ``FUT_*`` as ``FUTURE``. The dwh schema has
    finer-grained classes (``etf``/``fund``/``forex``/``index``/``future``);
    collapse them to the coarse enum the API contract expects so
    ``InstrumentId.asset_class.value`` keeps returning the historical value.
    """
    if raw == "future":
        return AssetClass.FUTURE
    if raw == "index":
        return AssetClass.INDEX
    # etf / fund / forex (and any future addition) → EQUITY, as Mongo did.
    return AssetClass.EQUITY


class SqlInstrumentReader:
    """Read-only SQL adapter for market data (prices, instruments, futures contracts).

    Used exclusively by the SQL-backed ``MarketDataService``. All queries go to
    the PostgreSQL dwh schema ``tcg_instruments`` with read-only enforcement.
    """

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    async def list_collections(
        self, asset_class: AssetClass | None = None
    ) -> list[str]:
        """List non-option ``source_collection`` names, optionally by asset class.

        Mirrors the Mongo registry's ``all_active`` semantics: options are
        excluded, and the coarse ``AssetClass`` filter maps onto the finer dwh
        classes — ``EQUITY`` → {etf, fund, forex}, ``INDEX`` → index,
        ``FUTURE`` → future. ``None`` returns every non-option collection.
        """
        # Map the coarse enum to the concrete dwh asset_class values it covers.
        if asset_class is None:
            classes = ["index", "etf", "fund", "forex", "future"]
        elif asset_class == AssetClass.INDEX:
            classes = ["index"]
        elif asset_class == AssetClass.FUTURE:
            classes = ["future"]
        else:  # AssetClass.EQUITY
            classes = ["etf", "fund", "forex"]
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT source_collection
                            FROM {SCHEMA}.dim_instrument
                            WHERE asset_class = ANY(%s)
                            ORDER BY source_collection""",
                        (classes,),
                    )
                    return [r["source_collection"] for r in await cur.fetchall()]
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(f"SQL error listing collections: {exc}") from exc

    async def collection_exists(self, collection: str) -> bool:
        """True if *collection* names a known non-option ``source_collection``.

        Preserves the Mongo path's up-front collection validation (a clean 404
        for an unknown collection) without trusting unvalidated route input to
        simply return empty.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT 1 FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s AND asset_class <> 'option'
                            LIMIT 1""",
                        (collection,),
                    )
                    return (await cur.fetchone()) is not None
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error checking collection '{collection}': {exc}"
            ) from exc

    async def list_instruments(
        self,
        collection: str,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[InstrumentId], int]:
        """List instruments in *collection* (by ``source_collection``), paginated.

        Returns ``(instruments, total_count)``. ``total`` is the full count
        ignoring pagination (the frontend needs it for the pager).
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT count(*) AS n FROM {SCHEMA}.dim_instrument "
                        f"WHERE source_collection = %s",
                        (collection,),
                    )
                    row = await cur.fetchone()
                    total = int(row["n"]) if row else 0

                    await cur.execute(
                        f"""SELECT symbol, asset_class, exchange
                            FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s
                            ORDER BY symbol
                            OFFSET %s LIMIT %s""",
                        (collection, skip, limit),
                    )
                    instruments: list[InstrumentId] = []
                    for r in await cur.fetchall():
                        instruments.append(
                            InstrumentId(
                                symbol=r["symbol"],
                                asset_class=_asset_class_for(r["asset_class"]),
                                collection=collection,
                                exchange=r["exchange"],
                            )
                        )
                    return instruments, total
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface any driver error uniformly
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
        """Fetch OHLCV for a single instrument (by ``symbol``) → ``PriceSeries``.

        ``provider`` is accepted for protocol parity but dwh stores one curated
        series per instrument (no per-provider arrays), so it does not branch
        the query — the row is whatever the backfill chose. [2] parent table +
        ``trade_date`` filter. [3] ``COALESCE(adj_close, close)``.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT f.trade_date,
                                   COALESCE(f.adj_close, f.close) AS close_val,
                                   f.open, f.high, f.low, f.volume
                            FROM {SCHEMA}.fact_price_eod f
                            JOIN {SCHEMA}.dim_instrument d
                              ON d.instrument_id = f.instrument_id
                            WHERE d.source_collection = %s
                              AND d.symbol = %s
                              AND f.trade_date BETWEEN %s AND %s
                            ORDER BY f.trade_date""",
                        (
                            collection,
                            instrument_id,
                            start if start is not None else _MIN_DATE,
                            end if end is not None else _MAX_DATE,
                        ),
                    )
                    rows = await cur.fetchall()
                    if not rows:
                        return None
                    return _rows_to_price_series(rows)
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error reading prices for '{instrument_id}' in '{collection}': {exc}"
            ) from exc

    async def fetch_futures_contracts(
        self,
        collection: str,
        *,
        cycle: str | None = None,
    ) -> list[ContractPriceData]:
        """Fetch all contracts in a futures collection, **ordered by expiration**.

        Returns one :class:`ContractPriceData` per contract symbol, each with a
        ``PriceSeries`` of its bars. The list is sorted ascending by expiration
        — the ``ContinuousSeriesBuilder`` (unchanged) requires this ordering;
        we do NOT reimplement the roll here, only feed it SQL-sourced contracts
        identical in shape to the old Mongo path.

        [2] parent ``fact_price_eod`` + ``trade_date``-free filter (the whole
        contract history is needed; the roller trims). [3] ``COALESCE``. Rows
        with NULL expiration are excluded (a continuous series is undefined for
        them). Zero-close rows are left in place — ``trim_overlaps`` strips them
        (this is how the GOLD 2023-04-07 ``close=0.0`` bar is handled, exactly
        as in the Mongo path).
        """
        try:
            params: list[Any] = [collection]
            cycle_clause = ""
            if cycle is not None:
                cycle_clause = " AND d.expiration_cycle = %s"
                params.append(cycle)

            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT d.symbol, d.expiration, f.trade_date,
                                   COALESCE(f.adj_close, f.close) AS close_val,
                                   f.open, f.high, f.low, f.volume
                            FROM {SCHEMA}.fact_price_eod f
                            JOIN {SCHEMA}.dim_instrument d
                              ON d.instrument_id = f.instrument_id
                            WHERE d.source_collection = %s
                              AND d.expiration IS NOT NULL
                              {cycle_clause}
                            ORDER BY d.expiration, d.symbol, f.trade_date""",
                        params,
                    )
                    rows = await cur.fetchall()
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error fetching futures contracts from '{collection}': {exc}"
            ) from exc

        # Group bars per contract symbol. The SQL ORDER BY already groups by
        # (expiration, symbol, trade_date); we preserve first-seen order which
        # is therefore ascending-by-expiration.
        grouped: dict[str, dict[str, Any]] = {}
        for r in rows:
            sym = r["symbol"]
            bucket = grouped.get(sym)
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
                grouped[sym] = bucket
            bucket["dates"].append(date_to_int(r["trade_date"]))
            bucket["close"].append(to_float_or(r["close_val"], 0.0))
            bucket["open"].append(to_float_or(r["open"], 0.0))
            bucket["high"].append(to_float_or(r["high"], 0.0))
            bucket["low"].append(to_float_or(r["low"], 0.0))
            bucket["volume"].append(to_float_or(r["volume"], 0.0))

        contracts: list[ContractPriceData] = []
        for sym, b in grouped.items():
            if not b["dates"]:
                continue
            contracts.append(
                ContractPriceData(
                    contract_id=sym,
                    expiration=b["expiration"],
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
        # Defensive: guarantee ascending-by-expiration even if the dict order
        # ever drifts (the roller raises if it is not sorted).
        contracts.sort(key=lambda c: c.expiration)
        return contracts

    async def find_contract_by_expiration(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the ``symbol`` of the single contract in *collection* whose
        ``expiration`` equals *expiration_int* (YYYYMMDD integer), or ``None``.

        Used by the OPT_VIX underlying resolver to map an option expiration to
        the matching FUT_VIX contract.
        """
        try:
            exp_date = int_to_date(expiration_int)
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT symbol FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s AND expiration = %s
                            ORDER BY symbol
                            LIMIT 1""",
                        (collection, exp_date),
                    )
                    row = await cur.fetchone()
                    return row["symbol"] if row else None
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error finding contract by expiration in '{collection}' "
                f"(expiration={expiration_int}): {exc}"
            ) from exc

    async def find_front_contract_on_or_after(
        self,
        collection: str,
        expiration_int: int,
    ) -> str | None:
        """Return the ``symbol`` of the FRONT contract in *collection* — the one
        with the smallest ``expiration`` that is >= *expiration_int* (YYYYMMDD
        integer) — or ``None`` when none expires on/after that date.

        Used by the option-on-future underlying resolver: index/commodity futures
        are quarterly while options also list serial months + weeklies, so a
        serial/weekly option settles against the FRONT quarterly future (nearest
        expiration >= the option's).  ``ORDER BY expiration ASC LIMIT 1`` selects
        it; ``symbol`` tie-breaks a (hypothetical) same-expiration duplicate
        deterministically.
        """
        try:
            exp_date = int_to_date(expiration_int)
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT symbol FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s AND expiration >= %s
                            ORDER BY expiration ASC, symbol ASC
                            LIMIT 1""",
                        (collection, exp_date),
                    )
                    row = await cur.fetchone()
                    return row["symbol"] if row else None
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error finding front contract on/after expiration in "
                f"'{collection}' (expiration={expiration_int}): {exc}"
            ) from exc

    async def list_futures_contract_meta(
        self,
        collection: str,
        *,
        cycle: str | None = None,
    ) -> list[FuturesContractMeta]:
        """List a futures root's contracts with expiration + contract_size.

        Cheap ``dim_instrument``-only scan (NO ``fact_price_eod`` join / no bars):
        one ``(symbol, expiration, contract_size)`` row per contract, ordered by
        expiration.  Feeds futures-notional option sizing — the caller picks the
        reference contract (nearest on/after OR nearest by |time|) and reads its
        ``contract_size`` as the LIVE ``M_fut`` (NULL → config fallback).  Rows
        with NULL expiration are excluded (they cannot be a dated reference).
        """
        try:
            params: list[Any] = [collection]
            cycle_clause = ""
            if cycle is not None:
                cycle_clause = " AND expiration_cycle = %s"
                params.append(cycle)
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT symbol, expiration, contract_size
                            FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s
                              AND expiration IS NOT NULL
                              {cycle_clause}
                            ORDER BY expiration ASC, symbol ASC""",
                        params,
                    )
                    rows = await cur.fetchall()
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error listing futures contract meta for '{collection}': {exc}"
            ) from exc
        out: list[FuturesContractMeta] = []
        for r in rows:
            cs = r["contract_size"]
            out.append(
                FuturesContractMeta(
                    symbol=r["symbol"],
                    expiration=r["expiration"],
                    contract_size=None if cs is None else float(cs),
                )
            )
        return out

    async def fetch_available_cycles(self, collection: str) -> list[str]:
        """Return distinct non-empty ``expiration_cycle`` values for a collection."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT expiration_cycle
                            FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s
                            ORDER BY expiration_cycle""",
                        (collection,),
                    )
                    return [
                        r["expiration_cycle"]
                        for r in await cur.fetchall()
                        if r["expiration_cycle"]
                    ]
        except DataAccessError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DataAccessError(
                f"SQL error fetching cycles from '{collection}': {exc}"
            ) from exc


def _rows_to_price_series(rows: list[dict[str, Any]]) -> PriceSeries:
    """Build a ``PriceSeries`` from dict rows (cols: trade_date, close_val, open,
    high, low, volume). Dates → YYYYMMDD int; NULL OHLV → 0.0; Decimal → float."""
    dates: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []
    for r in rows:
        dates.append(date_to_int(r["trade_date"]))
        closes.append(to_float_or(r["close_val"], 0.0))
        opens.append(to_float_or(r["open"], 0.0))
        highs.append(to_float_or(r["high"], 0.0))
        lows.append(to_float_or(r["low"], 0.0))
        volumes.append(to_float_or(r["volume"], 0.0))
    return PriceSeries(
        dates=np.array(dates, dtype=np.int64),
        open=np.array(opens, dtype=np.float64),
        high=np.array(highs, dtype=np.float64),
        low=np.array(lows, dtype=np.float64),
        close=np.array(closes, dtype=np.float64),
        volume=np.array(volumes, dtype=np.float64),
    )
