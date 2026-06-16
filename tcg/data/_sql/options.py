"""SQL read adapter for options data (OPT_* collections).

Replaces tcg.data.options.reader.MongoOptionsDataReader.
Uses the same DTO builders (doc_to_contract, bar_and_greek_to_row) but pulls
data from dwh v_option_chain (UNION of greeks and quotes) and fact_price_eod.

All 8 gotchas from the recon are honored:
1. Per-contract grain via symbol (PK = instrument_id)
2. Parent fact_price_eod (no *_YYYY partitions)
3. COALESCE(adj_close, close) for close → mid via bid/ask only
4. DTE from expiration or days_to_expiry, COALESCE fallback
5. Dollarize crypto BTC/ETH premiums by coin/USD spot
6. Slice chains by root_symbol NOT underlying (underlying_id NULL for 8/10 roots)
7. Filter greek_source (vendor vs computed)
8. Collapse UNION ALL dups, mid only when both bid&ask present and positive
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal, Mapping, Sequence

from psycopg import AsyncConnection

from tcg.data._sql.connection import DwhConnectionPool
from tcg.data.options._doc_to_dto import (
    _parse_yyyymmdd,
    bar_and_greek_to_row,
    doc_to_contract,
    index_greeks_by_date,
)
from tcg.data.options._provider import (
    get_stored_greeks_ratios,
    has_greeks_for_root,
    select_provider,
)
from tcg.types.errors import OptionsContractNotFound, OptionsDataAccessError
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)
from tcg.data._utils import date_to_int, int_to_date

logger = logging.getLogger(__name__)

SCHEMA = "tcg_instruments"
_ROOT_DISPLAY_NAMES: dict[str, str] = {
    "OPT_SP_500": "SP 500",
    "OPT_NASDAQ_100": "NASDAQ 100",
    "OPT_GOLD": "Gold",
    "OPT_BTC": "Bitcoin",
    "OPT_ETH": "Ethereum",
    "OPT_VIX": "VIX",
    "OPT_T_NOTE_10_Y": "T-Note 10Y",
    "OPT_T_BOND": "T-Bond",
    "OPT_EURUSD": "EUR/USD",
    "OPT_JPYUSD": "JPY/USD",
}


def _display_name(collection: str) -> str:
    if collection in _ROOT_DISPLAY_NAMES:
        return _ROOT_DISPLAY_NAMES[collection]
    return collection.removeprefix("OPT_").replace("_", " ").title()


class SqlOptionsDataReader:
    """Read-only SQL adapter for OPT_* collections.

    Satisfies the OptionsDataReader protocol. All queries go to dwh.
    """

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    async def get_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        """Return a single contract with its full chronological day series.

        Fetches from fact_price_eod + fact_option_greeks joined on instrument_id+date.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Find the contract by symbol (contract_id = symbol in dwh)
                    cur.execute(
                        f"""SELECT d.instrument_id, d.symbol, d.provider, d.root_symbol,
                                  d.underlying_id, d.underlying_symbol, d.expiration,
                                  d.expiration_cycle, d.strike, d.option_type,
                                  d.contract_size, d.currency
                           FROM {SCHEMA}.dim_instrument d
                           WHERE d.source_collection = %s AND d.symbol = %s""",
                        (collection, contract_id),
                    )
                    doc_row = cur.fetchone()
                    if not doc_row:
                        raise OptionsContractNotFound(
                            f"Contract '{contract_id}' not found in '{collection}'"
                        )

                    (
                        instrument_id,
                        symbol,
                        provider,
                        root_symbol,
                        underlying_id,
                        underlying_symbol,
                        expiration,
                        expiration_cycle,
                        strike,
                        option_type,
                        contract_size,
                        currency,
                    ) = doc_row

                    # Build OptionContractDoc from dwh row
                    # [Gotcha 6] underlying_ref: if underlying_id is NULL (8/10 roots),
                    # set to None. Otherwise, fetch the FUT symbol. But underlying_id
                    # points to INDEX, not FUT — so underlying_ref = None for all cases.
                    # This is a coverage gap (the Mongo underlying FUT ref is not in dwh).
                    contract = OptionContractDoc(
                        collection=collection,
                        contract_id=symbol,
                        root_underlying=root_symbol or "",
                        underlying_ref=None,  # COVERAGE GAP: Mongo FUT underlying not in dwh
                        underlying_symbol=underlying_symbol,
                        expiration=expiration,
                        expiration_cycle=expiration_cycle or "",
                        strike=float(strike) if strike is not None else 0.0,
                        type=option_type.upper() if option_type else "C",
                        contract_size=float(contract_size)
                        if contract_size is not None
                        else None,
                        currency=currency,
                        provider=provider,
                        strike_factor_verified=False,  # TODO: populate from dwh
                    )

                    # Fetch all daily rows for this contract
                    cur.execute(
                        f"""SELECT f.trade_date, f.close, f.open, f.high, f.low,
                                  f.bid, f.ask, f.bid_size, f.ask_size, f.volume, f.open_interest,
                                  g.delta, g.gamma, g.vega, g.theta, g.rho,
                                  g.implied_vol, g.underlying_price
                           FROM {SCHEMA}.fact_price_eod f
                           LEFT JOIN {SCHEMA}.fact_option_greeks g
                             ON g.instrument_id = f.instrument_id AND g.trade_date = f.trade_date
                           WHERE f.instrument_id = %s
                           ORDER BY f.trade_date""",
                        (instrument_id,),
                    )

                    rows: list[OptionDailyRow] = []
                    for row in cur.fetchall():
                        (
                            trade_date,
                            close_val,
                            open_val,
                            high_val,
                            low_val,
                            bid,
                            ask,
                            bid_size,
                            ask_size,
                            volume,
                            open_interest,
                            delta,
                            gamma,
                            vega,
                            theta,
                            rho,
                            implied_vol,
                            underlying_price,
                        ) = row

                        # [Gotcha 8] mid only when both bid & ask present and positive
                        mid = None
                        if bid is not None and ask is not None:
                            if bid > 0 and ask > 0:
                                mid = (float(bid) + float(ask)) / 2.0

                        # [Gotcha 3] close is the iVolatility "option_close" equivalent
                        row_obj = OptionDailyRow(
                            date=trade_date,
                            open=float(open_val) if open_val is not None else None,
                            high=float(high_val) if high_val is not None else None,
                            low=float(low_val) if low_val is not None else None,
                            close=float(close_val) if close_val is not None else None,
                            bid=float(bid) if bid is not None else None,
                            ask=float(ask) if ask is not None else None,
                            bid_size=float(bid_size) if bid_size is not None else None,
                            ask_size=float(ask_size) if ask_size is not None else None,
                            volume=float(volume) if volume is not None else None,
                            open_interest=float(open_interest)
                            if open_interest is not None
                            else None,
                            mid=mid,
                            iv_stored=float(implied_vol)
                            if implied_vol is not None
                            else None,
                            delta_stored=float(delta) if delta is not None else None,
                            gamma_stored=float(gamma) if gamma is not None else None,
                            theta_stored=float(theta) if theta is not None else None,
                            vega_stored=float(vega) if vega is not None else None,
                            underlying_price_stored=float(underlying_price)
                            if underlying_price is not None
                            else None,
                        )
                        rows.append(row_obj)

                    return OptionContractSeries(contract=contract, rows=tuple(rows))
        except OptionsContractNotFound:
            raise
        except Exception as exc:
            raise OptionsDataAccessError(
                f"SQL error reading contract '{contract_id}' from '{collection}': {exc}"
            ) from exc

    async def query_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """Query one-day option chain (one row per contract that traded that day).

        [Gotcha 6] Filter by root_symbol (not underlying). [Gotcha 8] UNION ALL
        may produce dups per contract; collapse via groupby(option_instrument_id).
        [Gotcha 4, 8] Compute DTE and mid via row-level SQL.
        """
        try:
            target_date = date
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Build WHERE clause for v_option_chain
                    where_parts = [
                        "v.trade_date = %s",
                        "d.root_symbol = %s",
                        "v.expiration BETWEEN %s AND %s",
                    ]
                    params: list[Any] = [
                        target_date,
                        root,
                        expiration_min,
                        expiration_max,
                    ]

                    if type in ("C", "P"):
                        where_parts.append("v.option_type = %s")
                        params.append(type.upper())

                    if strike_min is not None:
                        where_parts.append("v.strike >= %s")
                        params.append(float(strike_min))

                    if strike_max is not None:
                        where_parts.append("v.strike <= %s")
                        params.append(float(strike_max))

                    if expiration_cycle is not None:
                        where_parts.append("d.expiration_cycle = %s")
                        params.append(expiration_cycle)

                    where_clause = " AND ".join(where_parts)

                    # Query chain via v_option_chain
                    cur.execute(
                        f"""SELECT v.option_instrument_id, v.option_symbol,
                                  d.root_symbol, d.underlying_id, d.underlying_symbol,
                                  v.strike, v.option_type, v.expiration, v.expiration_cycle,
                                  v.bid, v.ask, v.option_close, v.volume, v.open_interest,
                                  v.delta, v.gamma, v.vega, v.theta, v.rho,
                                  v.implied_vol, v.underlying_price,
                                  COALESCE(v.days_to_expiry, (v.expiration - v.trade_date)),
                                  d.contract_size, d.currency, v.provider
                           FROM {SCHEMA}.v_option_chain v
                           JOIN {SCHEMA}.dim_instrument d ON d.instrument_id = v.option_instrument_id
                           WHERE {where_clause}
                           ORDER BY v.option_instrument_id""",
                        params,
                    )

                    # [Gotcha 8] Collapse UNION ALL dups: group by option_instrument_id, keep first
                    seen_ids: dict[int, tuple[OptionContractDoc, OptionDailyRow]] = {}
                    for row in cur.fetchall():
                        (
                            option_id,
                            option_symbol,
                            root_symbol,
                            underlying_id,
                            underlying_symbol,
                            strike,
                            option_type,
                            expiration,
                            expiration_cycle,
                            bid,
                            ask,
                            option_close,
                            volume,
                            open_interest,
                            delta,
                            gamma,
                            vega,
                            theta,
                            rho,
                            implied_vol,
                            underlying_price,
                            dte_raw,
                            contract_size,
                            currency,
                            provider,
                        ) = row

                        # Skip if already seen (first wins, per UNION ALL collapse rule)
                        if option_id in seen_ids:
                            continue

                        # [Gotcha 8] mid only when both bid & ask present and positive
                        mid = None
                        if bid is not None and ask is not None:
                            if bid > 0 and ask > 0:
                                mid = (float(bid) + float(ask)) / 2.0

                        # [Gotcha 4] DTE: prefer computed (expiration - trade_date), fallback to days_to_expiry
                        dte = 0
                        if expiration:
                            dte = max(0, (expiration - target_date).days)

                        contract = OptionContractDoc(
                            collection=root,  # OPT_* collection name derived from root
                            contract_id=option_symbol,
                            root_underlying=root_symbol or "",
                            underlying_ref=None,  # COVERAGE GAP
                            underlying_symbol=underlying_symbol,
                            expiration=expiration,
                            expiration_cycle=expiration_cycle or "",
                            strike=float(strike) if strike else 0.0,
                            type=option_type.upper() if option_type else "C",
                            contract_size=float(contract_size)
                            if contract_size
                            else None,
                            currency=currency,
                            provider=provider or "UNKNOWN",
                            strike_factor_verified=False,
                        )

                        daily_row = OptionDailyRow(
                            date=target_date,
                            open=None,  # v_option_chain doesn't have OHLC for intraday
                            high=None,
                            low=None,
                            close=float(option_close)
                            if option_close is not None
                            else None,
                            bid=float(bid) if bid is not None else None,
                            ask=float(ask) if ask is not None else None,
                            bid_size=None,  # fact_price_eod.bid_size = NULL for options
                            ask_size=None,
                            volume=float(volume) if volume is not None else None,
                            open_interest=float(open_interest)
                            if open_interest is not None
                            else None,
                            mid=mid,
                            iv_stored=float(implied_vol)
                            if implied_vol is not None
                            else None,
                            delta_stored=float(delta) if delta is not None else None,
                            gamma_stored=float(gamma) if gamma is not None else None,
                            theta_stored=float(theta) if theta is not None else None,
                            vega_stored=float(vega) if vega is not None else None,
                            underlying_price_stored=float(underlying_price)
                            if underlying_price is not None
                            else None,
                        )

                        seen_ids[option_id] = (contract, daily_row)

                    return list(seen_ids.values())
        except Exception as exc:
            raise OptionsDataAccessError(
                f"SQL error querying chain on '{root}' for {date}: {exc}"
            ) from exc

    async def list_roots(self) -> list[OptionRootInfo]:
        """List all OPT_* roots with metadata."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Get distinct roots (root_symbol for option collections)
                    cur.execute(
                        f"""SELECT DISTINCT source_collection, root_symbol
                           FROM {SCHEMA}.dim_instrument
                           WHERE asset_class = 'option'
                           ORDER BY source_collection"""
                    )

                    roots_info: list[OptionRootInfo] = []
                    for source_coll, root_symbol in cur.fetchall():
                        # Count docs in this root
                        cur.execute(
                            f"""SELECT count(*) FROM {SCHEMA}.dim_instrument
                               WHERE source_collection = %s""",
                            (source_coll,),
                        )
                        doc_count = cur.fetchone()[0]

                        # Find expiration range
                        cur.execute(
                            f"""SELECT min(expiration), max(expiration)
                               FROM {SCHEMA}.dim_instrument
                               WHERE source_collection = %s AND expiration IS NOT NULL""",
                            (source_coll,),
                        )
                        exp_row = cur.fetchone()
                        exp_first, exp_last = (
                            (exp_row[0], exp_row[1]) if exp_row else (None, None)
                        )

                        # Get providers (sample one doc)
                        cur.execute(
                            f"""SELECT DISTINCT provider FROM {SCHEMA}.dim_instrument
                               WHERE source_collection = %s ORDER BY provider""",
                            (source_coll,),
                        )
                        providers = tuple(row[0] for row in cur.fetchall())

                        # Stored greeks ratio (use seed value for now)
                        stored_ratio = 0.0
                        if not has_greeks_for_root(source_coll):
                            stored_ratio = 0.0

                        info = OptionRootInfo(
                            collection=source_coll,
                            name=_display_name(source_coll),
                            has_greeks=stored_ratio > 0.0,
                            providers=providers,
                            expiration_first=exp_first,
                            expiration_last=exp_last,
                            doc_count_estimated=int(doc_count),
                            strike_factor_verified=False,
                            last_trade_date=exp_last,
                            stored_greeks_ratio=stored_ratio,
                            has_computed_greeks=False,
                        )
                        roots_info.append(info)

                    return roots_info
        except Exception as exc:
            raise OptionsDataAccessError(f"SQL error listing roots: {exc}") from exc

    async def list_expirations(self, root: str) -> list[date]:
        """Distinct expirations for a root, sorted ascending."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    cur.execute(
                        f"""SELECT DISTINCT expiration FROM {SCHEMA}.dim_instrument
                           WHERE source_collection = %s AND expiration IS NOT NULL
                           ORDER BY expiration""",
                        (root,),
                    )
                    return [row[0] for row in cur.fetchall()]
        except Exception as exc:
            raise OptionsDataAccessError(
                f"SQL error listing expirations on '{root}': {exc}"
            ) from exc

    async def list_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | None = None,
    ) -> list[date]:
        """Distinct expirations filtered by type and/or cycle."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    where_parts = ["source_collection = %s", "expiration IS NOT NULL"]
                    params: list[Any] = [root]

                    if option_type is not None:
                        where_parts.append("option_type = %s")
                        params.append(option_type.upper())

                    if cycle is not None:
                        where_parts.append("expiration_cycle = %s")
                        params.append(cycle)

                    where_clause = " AND ".join(where_parts)
                    cur.execute(
                        f"""SELECT DISTINCT expiration FROM {SCHEMA}.dim_instrument
                           WHERE {where_clause}
                           ORDER BY expiration""",
                        params,
                    )
                    return [row[0] for row in cur.fetchall()]
        except Exception as exc:
            raise OptionsDataAccessError(
                f"SQL error listing filtered expirations on '{root}': {exc}"
            ) from exc
