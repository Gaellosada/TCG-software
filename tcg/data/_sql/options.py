"""SQL read adapter for options data (OPT_* collections).

Replaces ``tcg.data.options.reader.MongoOptionsDataReader``. Reads from the dwh
``v_option_chain`` view (a UNION ALL of a greeks-side and a quotes-only side)
plus ``fact_price_eod`` / ``fact_option_greeks``, and produces the SAME frozen
DTOs the Mongo path did (``OptionContractDoc`` / ``OptionDailyRow`` /
``OptionContractSeries`` / ``OptionRootInfo``) so the FastAPI options routes are
unchanged.

Collection mapping: ``dim_instrument.source_collection`` == the legacy Mongo
collection name (OPT_SP_500, …); ``symbol`` == the durable Mongo ``_id`` ==
``contract_id``. Chains are sliced by ``source_collection`` (equivalently
``root_symbol``) — NOT ``underlying_id``, which is NULL for 8/10 roots [Gotcha 6].

The 8 gotchas:
  1 per-contract grain (one contract per symbol);
  2 parent ``fact_price_eod`` filtered on date (never ``*_YYYY``);
  3 ``close`` is the raw option close — NEVER used as mid;
  4 dte = ``COALESCE(days_to_expiry, expiration - trade_date)``, clipped ≥0;
  5 dollarize Deribit BTC/ETH premiums (× coin/USD spot);
  6 slice by ``source_collection``/``root_symbol`` not underlying;
  7 ``greek_source`` (vendor vs computed) — surfaced for filtering, not stored
    on the row (the Mongo DTO had no such field; parity preserved);
  8 ``v_option_chain`` is UNION ALL → collapse to one row per
    ``option_instrument_id`` taking the first non-NULL of each field; mid only
    when bid&ask both present and >0 (else None); Decimal→float at the boundary.

``underlying_ref`` (the Mongo per-contract FUT ``_id`` an option-on-future
referenced) is NOT preserved in dwh (only ``underlying_id`` → the INDEX, and
``underlying_symbol`` → a provider ticker). It is therefore ``None`` here. The
options route degrades gracefully (VIX resolves its future via
``find_contract_by_expiration``; BTC uses the row-level underlying price; other
option-on-future roots fall back to stored greeks). See PROBLEMS.md.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal, Sequence

from tcg.data._sql.connection import SCHEMA, DwhConnectionPool, to_float
from tcg.data.options._provider import _SEED_RATIOS, has_greeks_for_root
from tcg.data.options._strike_factor import STRIKE_FACTOR_VERIFIED
from tcg.types.errors import OptionsContractNotFound, OptionsDataAccessError
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)

logger = logging.getLogger(__name__)

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

# [Gotcha 5] crypto option premiums are quoted in COIN; dollarize by the
# coin/USD spot (forex asset_class, symbol BTC_USD / ETH_USD).
_COIN_USD_BY_COLLECTION: dict[str, str] = {
    "OPT_BTC": "BTC_USD",
    "OPT_ETH": "ETH_USD",
}


def _cycle_predicate(
    expiration_cycle: "str | Sequence[str] | None",
) -> tuple[str | None, Any]:
    """Build the ``expiration_cycle`` WHERE fragment + bound value.

    A SCALAR (or ``None``) preserves the historical single-equality binding
    exactly — ``("expiration_cycle = %s", "M")`` — so existing callers/tests are
    byte-identical.  A SEQUENCE (the monthly 3rd-Friday series expands to two
    tags, see :func:`tcg.types.options.expand_cycle`) binds a list via
    ``= ANY(%s)`` so all its tags match in one query.  Returns ``(None, None)``
    when no cycle filter applies.

    A str is a ``Sequence[str]`` too, so the scalar test comes first.
    """
    if expiration_cycle is None:
        return None, None
    if isinstance(expiration_cycle, str):
        return "expiration_cycle = %s", expiration_cycle
    tags = list(expiration_cycle)
    if not tags:
        return None, None
    if len(tags) == 1:
        # Collapse a 1-element sequence to the scalar form (identical SQL/bind).
        return "expiration_cycle = %s", tags[0]
    return "expiration_cycle = ANY(%s)", tags


def _display_name(collection: str) -> str:
    if collection in _ROOT_DISPLAY_NAMES:
        return _ROOT_DISPLAY_NAMES[collection]
    return collection.removeprefix("OPT_").replace("_", " ").title()


def _normalize_type(raw: Any) -> Literal["C", "P"]:
    """Upper-case the call/put marker; default to 'C' on the (impossible by
    schema) NULL so the frozen DTO's ``Literal["C","P"]`` stays satisfied.

    dwh ``option_type`` is a NOT-NULL single char for every option row, so the
    default is never hit in practice; it exists only to keep the type total.
    """
    if isinstance(raw, str) and raw.strip().upper() in ("C", "P"):
        return raw.strip().upper()  # type: ignore[return-value]
    return "C"


def _sanitize_iv(value: float | None) -> float | None:
    """IV must be strictly positive. IVolatility uses negative/zero sentinels
    for non-converged rows — surface those as missing (parity with the Mongo
    DTO's ``_sanitize_iv``)."""
    if value is None or value <= 0.0:
        return None
    return value


def _canonical_mid_inputs_ok(bid: float | None, ask: float | None) -> bool:
    """True iff *bid* and *ask* admit a valid mid [Gotcha 8].

    The single source of truth for the mid validity rule (both quotes present
    AND strictly positive), shared by :func:`_mid` and the parity harness so
    they can never silently disagree. Matches the production Mongo
    ``_doc_to_dto._compute_mid``.
    """
    return bid is not None and ask is not None and bid > 0.0 and ask > 0.0


def _mid(bid: float | None, ask: float | None) -> float | None:
    """``(bid+ask)/2`` only when both present and >0 [Gotcha 8]; else None."""
    if not _canonical_mid_inputs_ok(bid, ask):
        return None
    return (bid + ask) / 2.0


def _coalesce_first(current: Any, incoming: Any) -> Any:
    """Return *current* if it is not None, else *incoming* — the per-field
    'first non-NULL' rule used to collapse UNION-ALL duplicate rows [Gotcha 8]."""
    return current if current is not None else incoming


class SqlOptionsDataReader:
    """Read-only SQL adapter for OPT_* collections (satisfies OptionsDataReader)."""

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # get_contract
    # ------------------------------------------------------------------
    async def get_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        """Return one contract (by ``symbol``) with its full daily series.

        Joins ``fact_price_eod`` (quotes/close) with ``fact_option_greeks``
        (stored greeks) on instrument_id+trade_date. [Gotcha 5] crypto premiums
        are dollarized per-date.
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT instrument_id, symbol, provider, root_symbol,
                                   underlying_symbol, expiration, expiration_cycle,
                                   strike, option_type, contract_size, currency
                            FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s AND symbol = %s""",
                        (collection, contract_id),
                    )
                    meta = await cur.fetchone()
                    if meta is None:
                        raise OptionsContractNotFound(
                            f"Contract '{contract_id}' not found in '{collection}'"
                        )

                    contract = self._meta_to_contract(collection, meta)
                    allow_greeks = has_greeks_for_root(collection)

                    await cur.execute(
                        f"""SELECT f.trade_date,
                                   f.close, f.open, f.high, f.low,
                                   f.bid, f.ask, f.bid_size, f.ask_size,
                                   f.volume, f.open_interest,
                                   g.delta, g.gamma, g.vega, g.theta,
                                   g.implied_vol, g.underlying_price
                            FROM {SCHEMA}.fact_price_eod f
                            LEFT JOIN {SCHEMA}.fact_option_greeks g
                              ON g.instrument_id = f.instrument_id
                             AND g.trade_date = f.trade_date
                            WHERE f.instrument_id = %s
                            ORDER BY f.trade_date""",
                        (meta["instrument_id"],),
                    )
                    raw = await cur.fetchall()

                    # [Gotcha 5] per-date coin/USD spot for crypto dollarization.
                    spot_by_date = await self._coin_spot_map(
                        conn, collection, [r["trade_date"] for r in raw]
                    )

                    rows: list[OptionDailyRow] = []
                    for r in raw:
                        rows.append(
                            self._row_from_fact(
                                r,
                                allow_greeks=allow_greeks,
                                coin_spot=spot_by_date.get(r["trade_date"]),
                            )
                        )
                    return OptionContractSeries(contract=contract, rows=tuple(rows))
        except OptionsContractNotFound:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error reading contract '{contract_id}' from '{collection}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # query_chain
    # ------------------------------------------------------------------
    async def query_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """One-day chain (one row per contract active that day).

        PUSHDOWN: instead of filtering ``v_option_chain`` by ``trade_date``
        alone (which seq-scans the whole yearly greeks+price partition because
        the only btree is the composite PK ``(instrument_id, trade_date)``), we
        first resolve the root's matching option ``instrument_id``s via the
        indexed ``source_collection`` dim lookup (all contract metadata, cheap),
        then LEFT JOIN ``fact_price_eod`` and ``fact_option_greeks`` on
        ``instrument_id + trade_date`` — which DOES use the PK for index scans
        and prunes to the single year. Measured 10.5s → 0.37s on OPT_SP_500.

        This also makes the UNION-ALL collapse unnecessary: joining both facts
        per contract yields exactly ONE row per contract (greeks OR quotes OR
        both), so there are no duplicate rows to merge — but the same gotcha-8
        semantics hold (a contract with only greeks, or only quotes, surfaces
        with the other side NULL). Slices by ``source_collection`` [Gotcha 6];
        ``option_type``/strike/expiration-window/cycle pushed to the dim CTE.
        ``ON p.trade_date=%s`` keeps partition pruning intact.
        """
        target_date = date
        try:
            dim_where = [
                "source_collection = %s",
                "asset_class = 'option'",
                "expiration BETWEEN %s AND %s",
            ]
            params: list[Any] = [root, expiration_min, expiration_max]
            if type in ("C", "P"):
                dim_where.append("option_type = %s")
                params.append(type.upper())
            if strike_min is not None:
                dim_where.append("strike >= %s")
                params.append(float(strike_min))
            if strike_max is not None:
                dim_where.append("strike <= %s")
                params.append(float(strike_max))
            _cycle_frag, _cycle_val = _cycle_predicate(expiration_cycle)
            if _cycle_frag is not None:
                dim_where.append(_cycle_frag)
                params.append(_cycle_val)

            # Three positional %s for the date appear AFTER the dim filters:
            # one in each fact join's ON, then the trade_date for partition
            # pruning is the same value — bind once per join.
            sql = f"""
                WITH ids AS (
                    SELECT instrument_id, symbol AS option_symbol, root_symbol,
                           underlying_symbol, expiration, expiration_cycle,
                           strike, option_type, contract_size, currency, provider
                    FROM {SCHEMA}.dim_instrument
                    WHERE {" AND ".join(dim_where)}
                )
                SELECT i.instrument_id AS option_instrument_id, i.option_symbol,
                       i.root_symbol, i.underlying_symbol,
                       i.strike, i.option_type, i.expiration, i.expiration_cycle,
                       p.bid, p.ask, p.close AS option_close, p.volume, p.open_interest,
                       g.delta, g.gamma, g.vega, g.theta,
                       g.implied_vol, g.underlying_price,
                       i.contract_size, i.currency, i.provider
                FROM ids i
                LEFT JOIN {SCHEMA}.fact_price_eod p
                       ON p.instrument_id = i.instrument_id AND p.trade_date = %s
                LEFT JOIN {SCHEMA}.fact_option_greeks g
                       ON g.instrument_id = i.instrument_id AND g.trade_date = %s
                WHERE p.instrument_id IS NOT NULL OR g.instrument_id IS NOT NULL
                ORDER BY i.instrument_id
            """
            params.extend([target_date, target_date])

            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    raw = await cur.fetchall()

                # [Gotcha 5] dollarize crypto: per-date coin/USD spot.
                spot_map = await self._coin_spot_map(conn, root, [target_date])
                coin_spot = spot_map.get(target_date)

            out: list[tuple[OptionContractDoc, OptionDailyRow]] = []
            for m in raw:
                contract = self._chain_meta_to_contract(root, m)
                row = self._row_from_chain(
                    m, target_date=target_date, coin_spot=coin_spot
                )
                out.append((contract, row))
            return out
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error querying chain on '{root}' for {target_date}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # query_chain_bulk
    # ------------------------------------------------------------------
    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Multi-date chain in ONE query (drop-in for the roll resolver).

        The options *rolling* path (``stream_resolver._fetch_exp``) needs the
        same chain across many trade dates.  Querying ``query_chain`` per date
        is the N+1 anti-pattern; this method fetches every date in a single
        index-driven round-trip instead.

        PUSHDOWN (mirrors :meth:`query_chain`): resolve the root's matching
        option ``instrument_id``s via the INDEXED ``source_collection`` dim
        lookup with ALL filters pushed (``option_type`` / strike /
        expiration-range / cycle).  Then build a key-set of
        ``(instrument_id, trade_date)`` from BOTH facts restricted to the
        requested ``dates`` (``trade_date = ANY(%s)`` — the date list bound
        ONCE as a ``date[]``), and LEFT JOIN ``fact_price_eod`` +
        ``fact_option_greeks`` back on the composite PK ``(instrument_id,
        trade_date)``.  The key-set UNION generalises ``query_chain``'s
        ``WHERE p IS NOT NULL OR g IS NOT NULL`` (a contract surfaces on a
        date if it has a price row OR a greeks row), so the same Gotcha-8
        semantics hold across all dates.  EXPLAIN ANALYZE: ids via
        ``ix_dim_expiration`` + indexed ``source_collection`` filter; the
        facts via Index-Only Scans on their PKs with the year partition
        pruned (``Heap Fetches: 0``).

        Result semantics (PARITY with the removed Mongo reader): EVERY
        requested date is a key in the returned dict — ``[]`` when no contract
        traded that day.  ``_fetch_exp`` does ``chain_index.update(result)``
        then ``chain_index.get(d, [])`` per date, so a present-but-empty list
        and a missing key are equivalent downstream; pre-seeding every date
        keeps the contract explicit and matches ``query_chain``'s per-date
        empty-list behaviour.
        """
        # Pre-seed every requested date (parity with the Mongo reader's
        # ``results = {d: [] for d in dates}``).  Empty input -> empty dict,
        # no query.
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {
            d: [] for d in dates
        }
        if not results:
            return results

        date_list = list(dict.fromkeys(dates))  # de-dupe, preserve order
        try:
            dim_where = [
                "source_collection = %s",
                "asset_class = 'option'",
                "expiration BETWEEN %s AND %s",
            ]
            params: list[Any] = [root, expiration_min, expiration_max]
            if type in ("C", "P"):
                dim_where.append("option_type = %s")
                params.append(type.upper())
            if strike_min is not None:
                dim_where.append("strike >= %s")
                params.append(float(strike_min))
            if strike_max is not None:
                dim_where.append("strike <= %s")
                params.append(float(strike_max))
            _cycle_frag, _cycle_val = _cycle_predicate(expiration_cycle)
            if _cycle_frag is not None:
                dim_where.append(_cycle_frag)
                params.append(_cycle_val)

            # Partition-pruning bound (CRITICAL): the fact tables are RANGE-
            # partitioned by ``trade_date`` (yearly, 1980..2050).  The LEFT JOINs
            # match ``p.trade_date = k.trade_date`` where ``k.trade_date`` is a
            # RUNTIME value from the ``keyset`` CTE — the planner cannot prune on a
            # runtime value, so without help it fans the join out across ALL ~71
            # yearly partitions of BOTH facts (~142 partition scans + locks per
            # call; EXPLAIN-confirmed planning 34ms and a 60s statement_timeout
            # blow-out under a cold cache → the OPT_SP_500 PoolTimeout).  Adding a
            # REDUNDANT CONSTANT ``trade_date BETWEEN <min> AND <max>`` on each join
            # gives the planner a plan-time range to prune on (collapsing to just
            # the spanned year partitions); ``= k.trade_date`` keeps correctness.
            # The bound is redundant — every row matching ``= k.trade_date`` already
            # lies within [min, max] — so the result is byte-identical (verified on
            # live dwh: rows identical, partitions 142→2, exec 98ms→10ms).  This is
            # the same constant-pushdown ``query_chain`` relies on (see :237).
            date_lo, date_hi = min(date_list), max(date_list)

            # The date list is bound twice (once per fact in the key-set UNION);
            # both reference the same Python list object.  The (lo, hi) pair is
            # bound once per fact LEFT JOIN.
            sql = f"""
                WITH ids AS (
                    SELECT instrument_id, symbol AS option_symbol, root_symbol,
                           underlying_symbol, expiration, expiration_cycle,
                           strike, option_type, contract_size, currency, provider
                    FROM {SCHEMA}.dim_instrument
                    WHERE {" AND ".join(dim_where)}
                ),
                keyset AS (
                    SELECT instrument_id, trade_date
                    FROM {SCHEMA}.fact_price_eod
                    WHERE instrument_id IN (SELECT instrument_id FROM ids)
                      AND trade_date = ANY(%s)
                    UNION
                    SELECT instrument_id, trade_date
                    FROM {SCHEMA}.fact_option_greeks
                    WHERE instrument_id IN (SELECT instrument_id FROM ids)
                      AND trade_date = ANY(%s)
                )
                SELECT k.trade_date,
                       i.instrument_id AS option_instrument_id, i.option_symbol,
                       i.root_symbol, i.underlying_symbol,
                       i.strike, i.option_type, i.expiration, i.expiration_cycle,
                       p.bid, p.ask, p.close AS option_close, p.volume, p.open_interest,
                       g.delta, g.gamma, g.vega, g.theta,
                       g.implied_vol, g.underlying_price,
                       i.contract_size, i.currency, i.provider
                FROM keyset k
                JOIN ids i ON i.instrument_id = k.instrument_id
                LEFT JOIN {SCHEMA}.fact_price_eod p
                       ON p.instrument_id = k.instrument_id
                      AND p.trade_date = k.trade_date
                      AND p.trade_date BETWEEN %s AND %s
                LEFT JOIN {SCHEMA}.fact_option_greeks g
                       ON g.instrument_id = k.instrument_id
                      AND g.trade_date = k.trade_date
                      AND g.trade_date BETWEEN %s AND %s
                ORDER BY k.trade_date, i.instrument_id
            """
            params.extend([date_list, date_list, date_lo, date_hi, date_lo, date_hi])

            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    raw = await cur.fetchall()

                # [Gotcha 5] dollarize crypto: per-date coin/USD spot for all
                # requested dates in one lookup (reused from query_chain).
                spot_by_date = await self._coin_spot_map(conn, root, date_list)

            for m in raw:
                row_date: date = m["trade_date"]
                contract = self._chain_meta_to_contract(root, m)
                row = self._row_from_chain(
                    m,
                    target_date=row_date,
                    coin_spot=spot_by_date.get(row_date),
                )
                # Defensive: a fact trade_date outside the requested set
                # cannot occur (the key-set is filtered by ANY(dates)), but
                # guard the dict access so a surprise never KeyErrors.
                bucket = results.get(row_date)
                if bucket is not None:
                    bucket.append((contract, row))
            return results
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error querying chain bulk on '{root}' for "
                f"{len(date_list)} dates: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # list_roots / list_expirations
    # ------------------------------------------------------------------
    async def list_roots(self) -> list[OptionRootInfo]:
        """List every OPT_* collection with display metadata.

        ``stored_greeks_ratio`` uses the measured ``_SEED_RATIOS`` baseline
        (gated by the data-layer block list) rather than a live scan of the
        103M-row greeks fact — an exact per-root DISTINCT count cannot finish
        inside the statement timeout, and the ratio only drives a left-nav
        badge. ``last_trade_date`` is the true latest bar date from
        ``fact_price_eod`` (NOT the last expiration).
        """
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT source_collection,
                                   count(*) AS doc_count,
                                   min(expiration) AS exp_first,
                                   max(expiration) AS exp_last,
                                   array_agg(DISTINCT provider) AS providers
                            FROM {SCHEMA}.dim_instrument
                            WHERE asset_class = 'option'
                            GROUP BY source_collection
                            ORDER BY source_collection""",
                    )
                    summaries = await cur.fetchall()

                    out: list[OptionRootInfo] = []
                    for s in summaries:
                        coll = s["source_collection"]
                        last_trade = await self._last_trade_date(conn, coll)
                        ratio = _SEED_RATIOS.get(coll, 0.0)
                        if not has_greeks_for_root(coll):
                            ratio = 0.0
                        out.append(
                            OptionRootInfo(
                                collection=coll,
                                name=_display_name(coll),
                                has_greeks=ratio > 0.0,
                                providers=tuple(p for p in (s["providers"] or []) if p),
                                expiration_first=s["exp_first"],
                                expiration_last=s["exp_last"],
                                doc_count_estimated=int(s["doc_count"]),
                                strike_factor_verified=STRIKE_FACTOR_VERIFIED.get(
                                    coll, False
                                ),
                                last_trade_date=last_trade,
                                stored_greeks_ratio=ratio,
                                has_computed_greeks=False,  # API layer overrides.
                            )
                        )
                    return out
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(f"SQL error listing roots: {exc}") from exc

    async def list_expirations(self, root: str) -> list[date]:
        """Distinct expirations on *root*, sorted ascending."""
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT expiration FROM {SCHEMA}.dim_instrument
                            WHERE source_collection = %s AND expiration IS NOT NULL
                            ORDER BY expiration""",
                        (root,),
                    )
                    return [r["expiration"] for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error listing expirations on '{root}': {exc}"
            ) from exc

    async def list_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> list[date]:
        """Distinct expirations on *root* filtered by type and/or cycle.

        ``cycle`` accepts a scalar (single tag, byte-identical to before) or a
        sequence of tags (the monthly 3rd-Friday series spans two — see
        :func:`tcg.types.options.expand_cycle`); ``DISTINCT expiration`` de-dupes
        a double-tagged expiration automatically.
        """
        try:
            where = ["source_collection = %s", "expiration IS NOT NULL"]
            params: list[Any] = [root]
            if option_type is not None:
                where.append("option_type = %s")
                params.append(option_type.upper())
            _cycle_frag, _cycle_val = _cycle_predicate(cycle)
            if _cycle_frag is not None:
                where.append(_cycle_frag)
                params.append(_cycle_val)
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""SELECT DISTINCT expiration FROM {SCHEMA}.dim_instrument
                            WHERE {" AND ".join(where)}
                            ORDER BY expiration""",
                        params,
                    )
                    return [r["expiration"] for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error listing filtered expirations on '{root}': {exc}"
            ) from exc

    async def list_expirations_by_date(
        self,
        root: str,
        start: date,
        end: date,
        option_type: Literal["C", "P"] | None = None,
        cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[date]]:
        """Per-trade-date map of expirations that are actually LISTED (quoted).

        Unlike :meth:`list_expirations_filtered` (a DISTINCT scan of
        ``dim_instrument`` = every expiration that ever existed for the root),
        this joins to ``fact_price_eod`` so an expiration only appears on a
        ``trade_date`` when a contract of that expiration has a price row that
        day.  Returns ``{trade_date: [expirations listed that day, sorted]}``.

        WHY: ``NearestToTarget`` on a daily-expiration root (OPT_BTC) snaps to
        the nearest expiration in the whole-window global set, which may not be
        listed yet on early trade dates → a systematic ``no_chain_for_date``.
        The stream resolver consumes this map to snap to an expiration actually
        listed on each date.  ONE distinct scan for the whole window (not a
        per-date query).

        PUSHDOWN + partition pruning: resolve matching option ``instrument_id``s
        via the indexed ``source_collection`` dim lookup (type / cycle /
        ``expiration >= start`` pushed), then join ``fact_price_eod`` on a
        CONSTANT ``trade_date BETWEEN start AND end`` so the planner prunes to
        the spanned year partitions (the same gotcha the bulk chain reader
        honours; a runtime-only join fans out across all ~71 partitions).
        Price-row based (the tradeable universe) — matches what ``mid`` and the
        bulk chain reader can actually return.
        """
        try:
            dim_where = [
                "source_collection = %s",
                "asset_class = 'option'",
                "expiration IS NOT NULL",
                "expiration >= %s",
            ]
            params: list[Any] = [root, start]
            if option_type is not None:
                dim_where.append("option_type = %s")
                params.append(option_type.upper())
            _cycle_frag, _cycle_val = _cycle_predicate(cycle)
            if _cycle_frag is not None:
                dim_where.append(_cycle_frag)
                params.append(_cycle_val)
            sql = f"""
                WITH ids AS (
                    SELECT instrument_id, expiration
                    FROM {SCHEMA}.dim_instrument
                    WHERE {" AND ".join(dim_where)}
                )
                SELECT DISTINCT p.trade_date, i.expiration
                FROM ids i
                JOIN {SCHEMA}.fact_price_eod p
                       ON p.instrument_id = i.instrument_id
                      AND p.trade_date BETWEEN %s AND %s
                ORDER BY p.trade_date, i.expiration
            """
            params.extend([start, end])
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
            out: dict[date, list[date]] = {}
            for r in rows:
                out.setdefault(r["trade_date"], []).append(r["expiration"])
            return out
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error listing per-date expirations on '{root}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal: DTO builders
    # ------------------------------------------------------------------
    def _meta_to_contract(
        self, collection: str, m: dict[str, Any]
    ) -> OptionContractDoc:
        """Build an ``OptionContractDoc`` from a ``dim_instrument`` row."""
        return OptionContractDoc(
            collection=collection,
            contract_id=m["symbol"],
            root_underlying=m["root_symbol"] or "",
            underlying_ref=None,  # COVERAGE GAP: Mongo FUT ref not preserved in dwh.
            underlying_symbol=m["underlying_symbol"],
            expiration=m["expiration"],
            expiration_cycle=m["expiration_cycle"] or "",
            strike=to_float(m["strike"]) or 0.0,
            type=_normalize_type(m["option_type"]),
            contract_size=to_float(m["contract_size"]),
            currency=m["currency"],
            provider=m["provider"],
            strike_factor_verified=STRIKE_FACTOR_VERIFIED.get(collection, False),
        )

    def _chain_meta_to_contract(
        self, collection: str, m: dict[str, Any]
    ) -> OptionContractDoc:
        """Build an ``OptionContractDoc`` from a merged ``v_option_chain`` row."""
        return OptionContractDoc(
            collection=collection,
            contract_id=m["option_symbol"],
            root_underlying=m["root_symbol"] or "",
            underlying_ref=None,  # COVERAGE GAP (see module docstring).
            underlying_symbol=m["underlying_symbol"],
            expiration=m["expiration"],
            expiration_cycle=m["expiration_cycle"] or "",
            strike=to_float(m["strike"]) or 0.0,
            type=_normalize_type(m["option_type"]),
            contract_size=to_float(m["contract_size"]),
            currency=m["currency"],
            provider=m["provider"] or "UNKNOWN",
            strike_factor_verified=STRIKE_FACTOR_VERIFIED.get(collection, False),
        )

    def _row_from_fact(
        self,
        r: dict[str, Any],
        *,
        allow_greeks: bool,
        coin_spot: float | None,
    ) -> OptionDailyRow:
        """Build an ``OptionDailyRow`` from a ``fact_price_eod``+greeks join row."""
        bid = to_float(r["bid"])
        ask = to_float(r["ask"])
        close = to_float(r["close"])
        mid = _mid(bid, ask)
        # [Gotcha 5] dollarize premium-like fields (NOT strike) by coin/USD spot.
        if coin_spot is not None and coin_spot > 0:
            bid = _scale(bid, coin_spot)
            ask = _scale(ask, coin_spot)
            close = _scale(close, coin_spot)
            mid = _scale(mid, coin_spot)

        trade_date: date = r["trade_date"]

        return OptionDailyRow(
            date=trade_date,
            open=to_float(r["open"]),
            high=to_float(r["high"]),
            low=to_float(r["low"]),
            close=close,
            bid=bid,
            ask=ask,
            bid_size=to_float(r["bid_size"]),
            ask_size=to_float(r["ask_size"]),
            volume=to_float(r["volume"]),
            open_interest=to_float(r["open_interest"]),
            mid=mid,
            iv_stored=_sanitize_iv(to_float(r["implied_vol"]))
            if allow_greeks
            else None,
            delta_stored=to_float(r["delta"]) if allow_greeks else None,
            gamma_stored=to_float(r["gamma"]) if allow_greeks else None,
            theta_stored=to_float(r["theta"]) if allow_greeks else None,
            vega_stored=to_float(r["vega"]) if allow_greeks else None,
            underlying_price_stored=to_float(r["underlying_price"]),
        )

    def _row_from_chain(
        self,
        m: dict[str, Any],
        *,
        target_date: date,
        coin_spot: float | None,
    ) -> OptionDailyRow:
        """Build an ``OptionDailyRow`` from a merged ``v_option_chain`` row.

        The view carries no OHLC (only option_close), so open/high/low are None
        and bid_size/ask_size are None (the fact has them NULL for options
        anyway). Greeks come straight from the view; the data-layer block list
        (OPT_ETH) is enforced by NULLing them when greeks are disallowed.
        """
        collection = self._collection_from_symbol(m["option_symbol"])
        allow_greeks = has_greeks_for_root(collection) if collection else True

        bid = to_float(m["bid"])
        ask = to_float(m["ask"])
        close = to_float(m["option_close"])
        mid = _mid(bid, ask)
        if coin_spot is not None and coin_spot > 0:
            bid = _scale(bid, coin_spot)
            ask = _scale(ask, coin_spot)
            close = _scale(close, coin_spot)
            mid = _scale(mid, coin_spot)

        return OptionDailyRow(
            date=target_date,
            open=None,
            high=None,
            low=None,
            close=close,
            bid=bid,
            ask=ask,
            bid_size=None,
            ask_size=None,
            volume=to_float(m["volume"]),
            open_interest=to_float(m["open_interest"]),
            mid=mid,
            iv_stored=_sanitize_iv(to_float(m["implied_vol"]))
            if allow_greeks
            else None,
            delta_stored=to_float(m["delta"]) if allow_greeks else None,
            gamma_stored=to_float(m["gamma"]) if allow_greeks else None,
            theta_stored=to_float(m["theta"]) if allow_greeks else None,
            vega_stored=to_float(m["vega"]) if allow_greeks else None,
            underlying_price_stored=to_float(m["underlying_price"]),
        )

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------
    def _collection_from_symbol(self, option_symbol: str | None) -> str | None:
        """Best-effort OPT_* collection from an option symbol for greek gating.

        Only OPT_ETH is block-listed, and ETH option symbols contain ``ETH``;
        a precise mapping would need a per-row dim lookup, which is not worth a
        round-trip for a single block-list check. Returns None when unknown
        (callers then allow greeks, matching the non-blocked default).
        """
        if not option_symbol:
            return None
        up = option_symbol.upper()
        if "ETH" in up:
            return "OPT_ETH"
        return None

    async def _coin_spot_map(
        self, conn: Any, collection: str, dates: list[date]
    ) -> dict[date, float]:
        """Return ``{trade_date: coin/USD close}`` for crypto roots [Gotcha 5].

        Empty for non-crypto collections (no dollarization needed) or when the
        forex series has no bars in range.
        """
        coin = _COIN_USD_BY_COLLECTION.get(collection)
        if coin is None or not dates:
            return {}
        lo, hi = min(dates), max(dates)
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT f.trade_date, f.close
                    FROM {SCHEMA}.fact_price_eod f
                    JOIN {SCHEMA}.dim_instrument d ON d.instrument_id = f.instrument_id
                    WHERE d.symbol = %s AND d.asset_class = 'forex'
                      AND f.trade_date BETWEEN %s AND %s""",
                (coin, lo, hi),
            )
            out: dict[date, float] = {}
            for r in await cur.fetchall():
                f = to_float(r["close"])
                if f is not None and f > 0:
                    out[r["trade_date"]] = f
            return out

    async def _last_trade_date(self, conn: Any, collection: str) -> date | None:
        """Latest ``trade_date`` with a bar in *collection* (live data cutoff).

        A ``max(trade_date)`` over a whole root joins millions of option bars
        with no usable ``trade_date`` index (the fact's only btree is the
        composite PK ``(instrument_id, trade_date)``) — it seq-scans every
        recent partition and times out. But a ``max`` over a SINGLE
        ``instrument_id`` is a fast PK index scan (~0.3s).

        So: pick ONE representative live contract — the nearest expiry with
        ``expiration >= today`` (the active front month, which trades up to the
        cutoff), via the indexed dim lookup — then ``max(trade_date)`` for just
        that instrument. Mirrors the Mongo ``_peek_last_trade_date`` logic.
        Falls back to the furthest-dated contract if none is live (a fully
        expired root), and returns ``None`` only when the root has no contracts
        at all. The value drives the frontend's default chain date; per-root
        precision is approximate by design (the front contract's last bar is
        the cutoff in practice).
        """
        today = date.today()
        async with conn.cursor() as cur:
            # Nearest live contract (front month). Indexed dim lookup, 1 row.
            await cur.execute(
                f"""SELECT instrument_id FROM {SCHEMA}.dim_instrument
                    WHERE source_collection = %s AND expiration >= %s
                    ORDER BY expiration ASC
                    LIMIT 1""",
                (collection, today),
            )
            row = await cur.fetchone()
            if row is None:
                # Fully expired root → use the furthest-dated contract.
                await cur.execute(
                    f"""SELECT instrument_id FROM {SCHEMA}.dim_instrument
                        WHERE source_collection = %s AND expiration IS NOT NULL
                        ORDER BY expiration DESC
                        LIMIT 1""",
                    (collection,),
                )
                row = await cur.fetchone()
            if row is None:
                return None

            await cur.execute(
                f"""SELECT max(trade_date) AS d
                    FROM {SCHEMA}.fact_price_eod
                    WHERE instrument_id = %s""",
                (row["instrument_id"],),
            )
            res = await cur.fetchone()
            return res["d"] if res else None


def _scale(value: float | None, factor: float) -> float | None:
    """Multiply *value* by *factor* when present; preserve None."""
    return None if value is None else value * factor
