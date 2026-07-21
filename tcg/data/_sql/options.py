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
from typing import Any, Literal, Mapping, Sequence

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


# --------------------------------------------------------------------------- #
# Shared SQL fragments — single-sourced so the byte-identity-critical projection
# and ordering cannot drift between the query builders (audit_d3 INV-5 / audit_d5).
# --------------------------------------------------------------------------- #

# The 23-column chain projection, WITHOUT the leading ``k.trade_date`` (the three
# BULK methods prepend it; :meth:`SqlOptionsDataReader.query_chain` — single-date —
# omits it).  Duplicated verbatim across 4 query builders before this extraction.
_CHAIN_SELECT_COLS = (
    "i.instrument_id AS option_instrument_id, i.option_symbol,\n"
    "                       i.root_symbol, i.underlying_symbol,\n"
    "                       i.strike, i.option_type, i.expiration, i.expiration_cycle,\n"
    "                       p.bid, p.ask, p.close AS option_close, p.volume, p.open_interest,\n"
    "                       g.delta, g.gamma, g.vega, g.theta,\n"
    "                       g.implied_vol, g.underlying_price,\n"
    "                       i.contract_size, i.currency, i.provider"
)

# Final ordering of the bulk chain reads.  FIRST-by-instrument_id within a date is
# LOAD-BEARING: ``_row_for_contract`` returns the first matching row, and the
# symbol-granular delta pushdown relies on a stable per-symbol order (audit_d3
# INV-5).  Single-sourced across the three bulk methods to kill drift.
_CHAIN_ORDER_BY = "ORDER BY k.trade_date, i.instrument_id"

# The delta-pushdown SYMBOL rank, expressed ONCE as SQL text.
# COUPLED: keep byte-identical to ``symbol_delta_rank`` below and to
# ``match_by_delta``'s PRIMARY sort key abs(delta-target) (audit_d3 INV-1).
_DELTA_RANK_ORDER_BY = (
    "ORDER BY best_dist ASC NULLS LAST,\n"
    "                                            best_strike ASC, option_symbol ASC"
)

# Tolerance for treating two |delta-target| distances as tied (audit_d3 INV-2/3).
_DELTA_TIE_TOL = 1e-12


def symbol_delta_rank(
    rows: "Sequence[tuple[OptionContractDoc, OptionDailyRow]]",
    target: float,
    k: int,
) -> "list[tuple[OptionContractDoc, OptionDailyRow]]":
    """SYMBOL-granular delta-rank REFERENCE for ONE (expiration, trade_date).

    Pure-Python encoding of the SQL delta pushdown in
    :meth:`SqlOptionsDataReader.query_chain_bulk_multi` — the SINGLE source of
    truth for the ranking semantics that in-tree tests bind ``match_by_delta``
    against.  Before this, three copies existed (the SQL string, ``match_by_delta``'s
    sort lambda, and the engine test fakes' private rank — audit_d3 INV-1); the
    fakes now import THIS, eliminating copy #3.

    Ranks SYMBOLS (``contract_id``) per group by ``(best |delta-target|, min
    strike, symbol)`` with NULL-delta symbols LAST, keeps the top-``k`` symbols,
    and returns EVERY row of each kept symbol (mirroring the ~2.68%
    duplicate-instrument_id retention) in rank order.

    # COUPLED: keep in sync with the SQL ``top_syms`` ORDER BY
    #   (``_DELTA_RANK_ORDER_BY``: best_dist ASC NULLS LAST, best_strike ASC,
    #    option_symbol ASC) AND with ``match_by_delta``'s PRIMARY sort key
    #   abs(delta-target).  Changing the PRIMARY distance metric on EITHER side
    #   without the other silently diverges the pushdown (audit_d3 INV-1); a
    #   tie-break-only change is safe (the global-min symbol stays rank-1).
    """
    by_symbol: "dict[str, list[tuple[OptionContractDoc, OptionDailyRow]]]" = {}
    for c, r in rows:
        by_symbol.setdefault(c.contract_id, []).append((c, r))

    def _best_dist(sym: str) -> float | None:
        ds = [
            abs(r.delta_stored - target)
            for _c, r in by_symbol[sym]
            if r.delta_stored is not None
        ]
        return min(ds) if ds else None

    def _sort_key(sym: str) -> tuple:
        bd = _best_dist(sym)
        best_strike = min(c.strike for c, _r in by_symbol[sym])
        # NULLS LAST: an all-NULL-delta symbol sorts after every ranked symbol.
        return (bd is None, bd if bd is not None else 0.0, best_strike, sym)

    ranked = sorted(by_symbol, key=_sort_key)
    return [cr for sym in ranked[:k] for cr in by_symbol[sym]]


def _pushdown_overflow_groups(
    results: "Mapping[date, list[tuple[OptionContractDoc, OptionDailyRow]]]",
    target: float,
    k: int,
) -> "tuple[set[tuple[date, date]], dict[tuple[date, date], str]]":
    """Detect >k RANK-1 delta-tie OVERFLOW groups from a ``srn <= k+1`` fetch.

    The pushdown SQL fetches ONE extra symbol (``srn <= k+1``) so the (k+1)th
    retained symbol is observable.  A group is an OVERFLOW iff it retained
    ``≥ k+1`` symbols AND the (k+1)th-ranked symbol shares the rank-1 (minimum)
    ``(best_dist, best_strike)`` pair — best ``|delta-target|`` within
    :data:`_DELTA_TIE_TOL` AND identical ``best_strike``.

    WHY BOTH keys (not best_dist alone): ``match_by_delta`` picks the winner by
    ``(|delta-target| ASC, strike ASC)`` and SQL's ``top_syms`` ranks symbols by
    ``(best_dist ASC, best_strike ASC, option_symbol ASC)``.  So the winner is the
    unique ``(min dist, min strike)`` symbol, which is ALSO SQL's rank-1 → always
    retained.  The ONLY regime where top-k can drop the winner is when MORE than k
    symbols share that EXACT ``(dist, strike)`` pair: then the tertiary key differs
    (SQL ``option_symbol`` vs ``match_by_delta``'s stable input/instrument_id
    order) and the k-subset SQL keeps may exclude ``match_by_delta``'s pick.  A >k
    tie on distance alone but with DISTINCT strikes (e.g. every deep-OTM put at
    delta≈0 on an expiration date) is NOT a risk — both sides pick the lowest
    strike — so it must NOT force a fallback (that would kill the pushdown speedup
    on every expiry-day resolve for no correctness gain).  The caller MUST fall
    back to the full chain for the (narrower) real-overflow groups (audit_d3
    INV-2/3, item E).

    Returns ``(overflow, drop)``:

    * ``overflow`` — the set of ``(expiration, trade_date)`` groups that overflow.
    * ``drop`` — for every NON-overflow group that returned k+1 symbols, maps
      ``(expiration, trade_date) -> contract_id`` of the (k+1)th (surplus) symbol
      to DISCARD, so the result becomes byte-identical to a plain ``srn <= k``
      fetch (the top-k winners are unaffected; only the extra probe symbol goes).

    Symbols are ranked by the IDENTICAL key as the SQL ``top_syms`` /
    :func:`symbol_delta_rank`: ``(best_dist ASC NULLS LAST, best_strike ASC,
    option_symbol ASC)``.  A group whose rank-1 best_dist is ``None`` (all-NULL
    delta) has no real winner — ``match_by_delta`` returns
    ``missing_delta_no_compute`` regardless of which symbols survive — so it is
    NEVER flagged as an overflow.
    """
    # Gather per (expiration, trade_date) group -> per symbol its deltas + strike.
    groups: "dict[tuple[date, date], dict[str, tuple[list[float | None], float]]]" = {}
    for _d, pairs in results.items():
        for c, r in pairs:
            g = groups.setdefault((c.expiration, r.date), {})
            deltas, _ = g.setdefault(c.contract_id, ([], c.strike))
            deltas.append(r.delta_stored)

    overflow: "set[tuple[date, date]]" = set()
    drop: "dict[tuple[date, date], str]" = {}
    for key, syms in groups.items():
        if len(syms) <= k:
            continue  # ≤ k symbols returned: no surplus (k+1)th was fetched
        ranked = sorted(
            syms.items(),
            key=lambda item: _symbol_rank_key(target, item[1][0], item[1][1], item[0]),
        )
        best_dist_1 = _symbol_best_dist(target, ranked[0][1][0])
        best_dist_kp1 = _symbol_best_dist(target, ranked[k][1][0])
        strike_1 = ranked[0][1][1]
        strike_kp1 = ranked[k][1][1]
        if (
            best_dist_1 is not None
            and best_dist_kp1 is not None
            and (best_dist_kp1 - best_dist_1) <= _DELTA_TIE_TOL
            and strike_kp1 == strike_1
        ):
            overflow.add(key)
        else:
            drop[key] = ranked[k][0]  # discard the surplus (k+1)th symbol
    return overflow, drop


def _symbol_best_dist(target: float, deltas: "list[float | None]") -> float | None:
    """Best (minimum) ``|delta - target|`` over a symbol's rows; None if all NULL.

    Mirrors :func:`symbol_delta_rank`'s ``_best_dist`` — the single delta-rank key.
    """
    ds = [abs(d - target) for d in deltas if d is not None]
    return min(ds) if ds else None


def _symbol_rank_key(
    target: float, deltas: "list[float | None]", strike: float, symbol: str
) -> tuple:
    """The SYMBOL rank sort key shared with the SQL ``top_syms`` ORDER BY and
    :func:`symbol_delta_rank`: ``(best_dist ASC NULLS LAST, best_strike ASC,
    option_symbol ASC)``."""
    bd = _symbol_best_dist(target, deltas)
    return (bd is None, bd if bd is not None else 0.0, strike, symbol)


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
                SELECT {_CHAIN_SELECT_COLS}
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
                       {_CHAIN_SELECT_COLS}
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
                {_CHAIN_ORDER_BY}
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
    # query_chain_bulk_multi (year-chunk fast path)
    # ------------------------------------------------------------------
    async def query_chain_bulk_multi(
        self,
        root: str,
        type: Literal["C", "P", "both"],
        groups: Sequence[tuple[date, Sequence[date]]],
        expiration_cycle: str | Sequence[str] | None = None,
        delta_pushdown: "tuple[float, int] | None" = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Multi-EXPIRATION bulk chain fetch in ONE query (year-chunk fast path).

        Collapses the per-expiration :meth:`query_chain_bulk` fan-out into a
        SINGLE round-trip covering several expirations, each restricted to its
        OWN trade-date window.  ``groups`` is ``[(expiration, [trade_dates...]),
        ...]`` — typically one calendar year's worth of monthly expirations
        (~12), the granularity the Wave 2 design proved index-only.

        The per-expiration DATE restriction is LOAD-BEARING: a ``win(exp, lo,
        hi)`` VALUES table is joined so each expiration's contracts are fetched
        only on ``[min..max]`` of ITS OWN dates.  That keeps the keyset tiny
        (contract × ~21 dates) so the planner stays on Index-Only PK scans (Heap
        Fetches 0) across the whole year even WITHOUT a strike bound
        (EXPLAIN-proven live, 1406 ms / year).  The redundant CONSTANT
        ``trade_date BETWEEN <chunk-min> AND <chunk-max>`` on EVERY partitioned-
        fact reference prunes to the spanned yearly partitions (the same gotcha
        :meth:`query_chain_bulk` honours — a runtime-only join fans across all
        ~71 partitions).

        Candidate-set semantics MATCH :meth:`query_chain_bulk` per (expiration,
        trade_date):

        * FULL CHAIN (``delta_pushdown=None``, default) — ALL strikes of ``type``
          are returned: a strict SUPERSET of the old per-group
          ``spot*0.40..1.30`` band.  Selection (``match_by_delta`` /
          ``match_by_strike`` / ``match_by_moneyness``) is UNCHANGED and picks
          the identical contract for any target well inside the old band.
        * ``delta_pushdown`` — a ``(target_delta, k)`` tuple engages
          the single-scan DELTA PUSHDOWN: the greeks fact is scanned ONCE
          (``cand``) and option SYMBOLS are ranked per ``(expiration,
          trade_date)`` by their nearest ``|delta - target|`` (tie-break lower
          strike, then symbol) — the IDENTICAL key ``match_by_delta`` uses (see
          :func:`symbol_delta_rank`, the shared Python reference) — with
          only the top-``k`` SYMBOLS retained; EVERY physical row of each retained
          symbol is returned (via a keyset whose greeks side REUSES ``cand`` and
          whose price side is a k-symbol PK lookup — no second greeks scan).
          OVERFLOW SAFEGUARD: the SQL actually fetches ``k+1`` symbols so a >k
          rank-1 tie is detectable; if MORE than k symbols share the best
          ``(|delta-target|, strike)`` pair (the one regime the SQL
          ``option_symbol`` tie-break could resolve differently from
          ``match_by_delta``'s instrument_id order), the whole chunk
          HARD-FALLS-BACK to the full chain (``delta_pushdown=None``) so the pick
          is provably complete — otherwise the surplus (k+1)th symbol is discarded
          so the result is byte-identical to a plain top-k fetch.  A >k tie on
          distance alone with DISTINCT strikes (all deep-OTM puts at delta≈0 on an
          expiration date) is NOT flagged — both sides pick the lowest strike, so
          the pushdown speedup is preserved (audit_d3 INV-2/3, item E).
          Symbol-granular (not row-level) so the ~2.68% DUPLICATE-instrument_id-
          per-symbol quirk is byte-identical to the full chain: the winner's whole
          duplicate set is present, so both ``match_by_delta``'s pick AND
          ``_row_for_contract``'s first-by-instrument_id row match.  Returned ROW
          SHAPE is unchanged — selection consumes the retained symbols' rows
          instead of the full chain.  NULL
          deltas are NOT filtered — ``min`` ignores them and ``NULLS LAST`` ranks
          an all-NULL symbol last, so the non-null winner is unaffected AND an
          all-NULL-delta chain returns rows that ``match_by_delta`` classifies
          identically (byte-parity of the error path).  Correct ONLY for
          STORED-delta selection (the caller's engine gate guarantees that —
          never engage it under ``compute_missing_for_delta``).

        Result shape is identical to :meth:`query_chain_bulk`: a dict keyed by
        EVERY requested trade_date (``[]`` when nothing traded), rows grouped
        under the fact ``trade_date`` and ordered by ``instrument_id`` within a
        date.  When a trade_date falls in TWO expirations' windows (a HOLD roll
        day appended to the prior group), its list carries BOTH expirations'
        rows — exactly what the old per-expiration gather produced once merged.
        """
        # Pre-seed every requested trade_date (parity with query_chain_bulk's
        # ``results = {d: [] for d in dates}``) and build the per-expiration
        # windows.  De-dupe each group's dates, drop empty groups.
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        win_rows: list[tuple[date, date, date]] = []
        all_dates_set: set[date] = set()
        for exp, dts in groups:
            dl = list(dict.fromkeys(dts))
            for d in dl:
                results.setdefault(d, [])
            if not dl:
                continue
            win_rows.append((exp, min(dl), max(dl)))
            all_dates_set.update(dl)
        if not win_rows:
            return results

        all_dates = sorted(all_dates_set)
        # Partition-pruning bound (CRITICAL, see query_chain_bulk): the redundant
        # constant range over the whole chunk lets the planner prune the fact
        # partitions to just the spanned year(s).
        chunk_lo, chunk_hi = all_dates[0], all_dates[-1]

        try:
            dim_where = ["source_collection = %s", "asset_class = 'option'"]
            dim_params: list[Any] = [root]
            if type in ("C", "P"):
                dim_where.append("option_type = %s")
                dim_params.append(type.upper())
            _cycle_frag, _cycle_val = _cycle_predicate(expiration_cycle)
            if _cycle_frag is not None:
                dim_where.append(_cycle_frag)
                dim_params.append(_cycle_val)

            pushdown = delta_pushdown is not None
            # Build the win VALUES table.  The FIRST row casts each column so the
            # CTE's column types are fixed for the joins.
            win_params: list[Any] = []
            value_rows: list[str] = []
            for i, (exp, lo, hi) in enumerate(win_rows):
                value_rows.append(
                    "(%s::date, %s::date, %s::date)" if i == 0 else "(%s, %s, %s)"
                )
                win_params.extend([exp, lo, hi])

            # SHARED scaffolding (both branches): the ``win`` VALUES prune and
            # ``ids`` (this root's contracts of the requested type/cycle whose
            # expiration is one of the chunk's expirations, JOIN win).  Written
            # ONCE so the load-bearing per-expiration window + chunk prune cannot
            # drift between the full-chain and pushdown branches.
            cte_prefix = f"""
                WITH win (exp, lo, hi) AS (
                    VALUES {", ".join(value_rows)}
                ),
                ids AS (
                    SELECT d.instrument_id, d.symbol AS option_symbol, d.root_symbol,
                           d.underlying_symbol, d.expiration, d.expiration_cycle,
                           d.strike, d.option_type, d.contract_size, d.currency,
                           d.provider
                    FROM {SCHEMA}.dim_instrument d
                    JOIN win w ON d.expiration = w.exp
                    WHERE {" AND ".join(dim_where)}
                )"""

            # SHARED final projection + price/greeks join tail.  The driving CTE
            # (``keyset`` OR ``pick_keyset``) is aliased ``k`` in BOTH branches, so
            # the returned ROW SHAPE is IDENTICAL and ``match_by_delta`` consumes
            # the pushdown output unchanged (k rows/group instead of the whole
            # chain).
            def _select_tail(driver: str) -> str:
                return f"""
                SELECT k.trade_date,
                       {_CHAIN_SELECT_COLS}
                FROM {driver} k
                JOIN ids i ON i.instrument_id = k.instrument_id
                LEFT JOIN {SCHEMA}.fact_price_eod p
                       ON p.instrument_id = k.instrument_id
                      AND p.trade_date = k.trade_date
                      AND p.trade_date BETWEEN %s AND %s
                LEFT JOIN {SCHEMA}.fact_option_greeks g
                       ON g.instrument_id = k.instrument_id
                      AND g.trade_date = k.trade_date
                      AND g.trade_date BETWEEN %s AND %s
                {_CHAIN_ORDER_BY}
            """

            params: list[Any] = []
            params.extend(win_params)  # VALUES
            params.extend(dim_params)  # ids WHERE

            if pushdown:
                target, k = delta_pushdown  # (target_delta, top-k)
                # DELTA PUSHDOWN (SYMBOL-granular, single-scan).  Rank this root's
                # option SYMBOLS per (expiration, trade_date) by their nearest
                # |delta - target| (tie-break lower strike) — the IDENTICAL key
                # ``match_by_delta`` sorts on — keep the top-k SYMBOLS, and return
                # EVERY physical row of each retained symbol.
                #
                # WHY symbol-granular (not row-level ``ROW_NUMBER`` top-k):
                # the dwh stores ~2.68% DUPLICATE instrument_ids per option symbol
                # (same symbol/contract_id/strike, differing delta + quotes).  The
                # full-chain path returns BOTH rows; ``match_by_delta`` picks the
                # winning CONTRACT (by symbol) and ``_row_for_contract`` then
                # surfaces the FIRST row of that contract_id in instrument_id order
                # — which may be the FAR-delta sibling.  A row-level top-k keeps
                # only the near-delta sibling and drops the far one, so
                # ``_row_for_contract`` surfaced a DIFFERENT physical row → a ~20%
                # mid divergence on ~16% of SPX-put bars.  Ranking by symbol and
                # returning all of a retained symbol's rows makes the candidate set
                # (hence both the pick AND the resolved row) byte-identical to
                # ``query_chain_bulk``.
                #
                # SINGLE big greeks scan: ``cand`` reads the greeks fact ONCE (the
                # only expensive scan) and every later CTE is a window/aggregate
                # over ``cand`` or a targeted PK lookup on the k retained symbols'
                # instrument_ids.  The keyset's greeks side REUSES ``cand`` (no
                # re-scan); its price side is a k-symbol PK lookup — so byte-parity
                # is regained WITHOUT the double greeks read that made the earlier
                # symbol-granular build slow.
                #
                # NULL deltas are DELIBERATELY kept (no ``delta IS NOT NULL``):
                # ``abs(NULL-target)`` is NULL, ``min`` ignores it, and
                # ``ORDER BY best_dist NULLS LAST`` ranks an all-NULL symbol last —
                # so the non-null winner's symbol is unaffected AND a chain whose
                # rows are ALL null-delta still returns rows that ``match_by_delta``
                # classifies ``missing_delta_no_compute`` (byte-parity of the error
                # path).  Correct only for STORED-delta selection (engine-gated).
                body = f""",
                cand AS (
                    SELECT i.expiration, g.trade_date, g.instrument_id,
                           i.option_symbol, i.strike,
                           abs(g.delta - %s) AS dist
                    FROM {SCHEMA}.fact_option_greeks g
                    JOIN ids i ON i.instrument_id = g.instrument_id
                    JOIN win w ON w.exp = i.expiration
                    WHERE g.trade_date = ANY(%s)
                      AND g.trade_date BETWEEN w.lo AND w.hi
                      AND g.trade_date BETWEEN %s AND %s
                ),
                sym AS (
                    SELECT expiration, trade_date, option_symbol,
                           min(dist) AS best_dist, min(strike) AS best_strike
                    FROM cand
                    GROUP BY expiration, trade_date, option_symbol
                ),
                top_syms AS (
                    SELECT expiration, trade_date, option_symbol
                    FROM (
                        SELECT expiration, trade_date, option_symbol,
                               ROW_NUMBER() OVER (
                                   PARTITION BY expiration, trade_date
                                   {_DELTA_RANK_ORDER_BY}
                               ) AS srn
                        FROM sym
                    ) s
                    WHERE srn <= %s
                ),
                pick_keyset AS (
                    SELECT c.instrument_id, c.trade_date
                    FROM cand c
                    JOIN top_syms t ON t.expiration = c.expiration
                                   AND t.trade_date = c.trade_date
                                   AND t.option_symbol = c.option_symbol
                    UNION
                    SELECT p.instrument_id, p.trade_date
                    FROM {SCHEMA}.fact_price_eod p
                    JOIN ids i ON i.instrument_id = p.instrument_id
                    JOIN top_syms t ON t.expiration = i.expiration
                                   AND t.option_symbol = i.option_symbol
                                   AND t.trade_date = p.trade_date
                    WHERE p.trade_date = ANY(%s)
                      AND p.trade_date BETWEEN %s AND %s
                )"""
                sql = cte_prefix + body + _select_tail("pick_keyset")
                params.append(float(target))  # cand abs(delta - target)
                params.append(all_dates)  # cand trade_date = ANY
                params.extend([chunk_lo, chunk_hi])  # cand chunk prune
                # Fetch ONE extra symbol (srn <= k+1) so the (k+1)th is observable
                # to the overflow detector below; non-overflow groups discard it to
                # stay byte-identical to a plain top-k fetch (audit_d3 INV-2/3).
                params.append(int(k) + 1)  # top_syms srn <= k+1
                params.append(all_dates)  # pick_keyset price trade_date = ANY
                params.extend([chunk_lo, chunk_hi])  # pick_keyset price chunk prune
                params.extend([chunk_lo, chunk_hi])  # final price join
                params.extend([chunk_lo, chunk_hi])  # final greeks join
            else:
                # keyset: the (instrument_id, trade_date) pairs that actually
                # traded, each expiration bounded to ITS OWN window via
                # ``BETWEEN w.lo AND w.hi`` (the load-bearing restriction) plus
                # the chunk-constant prune.
                body = f""",
                keyset AS (
                    SELECT p.instrument_id, p.trade_date
                    FROM {SCHEMA}.fact_price_eod p
                    JOIN ids i ON i.instrument_id = p.instrument_id
                    JOIN win w ON w.exp = i.expiration
                    WHERE p.trade_date = ANY(%s)
                      AND p.trade_date BETWEEN w.lo AND w.hi
                      AND p.trade_date BETWEEN %s AND %s
                    UNION
                    SELECT g.instrument_id, g.trade_date
                    FROM {SCHEMA}.fact_option_greeks g
                    JOIN ids i ON i.instrument_id = g.instrument_id
                    JOIN win w ON w.exp = i.expiration
                    WHERE g.trade_date = ANY(%s)
                      AND g.trade_date BETWEEN w.lo AND w.hi
                      AND g.trade_date BETWEEN %s AND %s
                )"""
                sql = cte_prefix + body + _select_tail("keyset")
                params.extend([all_dates, chunk_lo, chunk_hi])  # keyset price
                params.extend([all_dates, chunk_lo, chunk_hi])  # keyset greeks
                params.extend([chunk_lo, chunk_hi])  # final price join
                params.extend([chunk_lo, chunk_hi])  # final greeks join

            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    raw = await cur.fetchall()

                # [Gotcha 5] dollarize crypto premiums per trade date.
                spot_by_date = await self._coin_spot_map(conn, root, all_dates)

            for m in raw:
                row_date: date = m["trade_date"]
                contract = self._chain_meta_to_contract(root, m)
                row = self._row_from_chain(
                    m,
                    target_date=row_date,
                    coin_spot=spot_by_date.get(row_date),
                )
                bucket = results.get(row_date)
                if bucket is not None:
                    bucket.append((contract, row))

            if pushdown:
                # Correctness safeguard (audit_d3 INV-2/3).  The pushdown's ``k``
                # cushions the ~2.68% duplicate-instrument_id quirk + delta ties,
                # and k=1 is exact when the rank-1 symbol is unique.  A >k tie at
                # the rank-1 best-|delta-target| distance is the ONE regime where
                # the SQL symbol tie-break (best_strike, option_symbol) could keep a
                # DIFFERENT k-subset than ``match_by_delta`` would pick from — a
                # possible WRONG pick.  Detect it from the k+1 fetch and, for
                # overflow groups, HARD-FALL-BACK to the full chain so the candidate
                # set (hence the pick) is provably complete and byte-identical to
                # the pre-pushdown path — the fast path can NEVER return a wrong
                # pick, even on pathological data.  Item F already rejects the only
                # VALID input (wrong-signed target) that could reach a real
                # overflow, so this guards an input-unreachable edge.
                target_f, k_i = float(target), int(k)
                overflow, drop = _pushdown_overflow_groups(results, target_f, k_i)
                if overflow:
                    logger.warning(
                        "delta pushdown on '%s' (target=%s, k=%d): %d (expiration, "
                        "trade_date) group(s) have MORE than k symbols sharing the "
                        "rank-1 (best-|delta-target|, strike) pair — falling back to "
                        "the FULL chain for this chunk so the pick is provably "
                        "correct (audit_d3 INV-2/3). Investigate if the target delta "
                        "is degenerate.",
                        root,
                        target,
                        k_i,
                        len(overflow),
                    )
                    return await self.query_chain_bulk_multi(
                        root=root,
                        type=type,
                        groups=groups,
                        expiration_cycle=expiration_cycle,
                        delta_pushdown=None,
                    )
                # No overflow: discard each non-overflow group's surplus (k+1)th
                # symbol so the returned rows are byte-identical to a top-k fetch.
                for (exp, dt), sym_id in drop.items():
                    bucket = results.get(dt)
                    if bucket:
                        results[dt] = [
                            (c, r)
                            for (c, r) in bucket
                            if not (c.expiration == exp and c.contract_id == sym_id)
                        ]
            return results
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error querying multi-expiration chain bulk on '{root}' "
                f"for {len(win_rows)} expirations: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # query_held_rows (hold-leg two-phase Phase 2)
    # ------------------------------------------------------------------
    async def query_held_rows(
        self,
        root: str,
        type: Literal["C", "P", "both"],
        held_windows: Sequence[tuple[str, date, date]],
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Identity keyset fetch of specific HELD option SYMBOLS over per-symbol
        date windows (hold-leg two-phase Phase 2).

        ``held_windows`` is ``[(symbol, lo, hi), ...]`` — each frozen held
        contract's dwh ``symbol`` (== ``OptionContractDoc.contract_id``) and its
        held date-range ``[lo, hi]``.  ``hi`` MUST include the NEXT segment's roll
        date, because ``_resolve_hold`` reads the OLD contract's mid on the roll
        seam — see ``hold_pushdown_design.md`` §2.

        Returns EVERY physical row of each symbol over its window keyed by the fact
        ``trade_date``; a date with no row is simply absent (the caller reads
        ``.get(d, [])``).

        Symbol-keyed (``VALUES(sym, lo, hi) JOIN d.symbol``): the held symbol is
        already SELECTED in Python (from the Phase-1 candidate chain), so this is a
        pure IDENTITY fetch — SQL never ranks or picks.  The redundant CONSTANT
        ``trade_date BETWEEN <chunk-min> AND <chunk-max>`` prune on every
        partitioned-fact reference collapses the yearly-partition fan-out (the same
        gotcha :meth:`query_chain_bulk_multi` honours — a runtime-only join fans
        across all ~71 partitions).

        ``expiration_cycle`` applies the SAME cycle predicate the full-chain path
        (:meth:`query_chain_bulk` / :meth:`query_chain_bulk_multi`) uses.  This is
        LOAD-BEARING for byte-identity, NOT a redundant filter: a symbol is NOT
        unique across cycles — the ~2.68% OPT_SP_500 "duplicate-instrument_id"
        quirk is a SINGLE symbol carrying two ``instrument_id`` rows under DIFFERENT
        ``expiration_cycle`` tags (e.g. the 3rd-Friday contract tagged both ``"M"``
        and ``"W3 Friday"``), with different quotes.  The full-chain path's cycle
        filter drops the off-cycle sibling, so a weekly leg only ever sees the
        ``"W3 Friday"`` row; without the SAME filter here, this fetch re-admits the
        ``"M"`` sibling and ``_row_for_contract``'s first-by-``instrument_id`` pick
        surfaces the WRONG physical row (live 4970_P/2024-03-06: 7.40 vs 4.15).
        ``None`` = no filter (all cycles), matching the full-chain ``None`` case.
        """
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        # De-dupe by symbol (a symbol could in principle appear twice with the
        # same window); drop empty/None symbols defensively.
        seen: dict[str, tuple[str, date, date]] = {}
        for sym, lo, hi in held_windows:
            if not sym:
                continue
            key = str(sym)
            if key in seen:
                # Widen to cover both windows (never shrink) — a symbol held over
                # two runs (degenerate) keeps the union range.
                _s, _lo, _hi = seen[key]
                seen[key] = (key, min(_lo, lo), max(_hi, hi))
            else:
                seen[key] = (key, lo, hi)
        win_rows: list[tuple[str, date, date]] = list(seen.values())
        if not win_rows:
            return results

        # Partition-pruning bound (CRITICAL, see query_chain_bulk_multi): the
        # redundant constant range over ALL windows lets the planner prune the fact
        # partitions to just the spanned year(s).
        chunk_lo = min(lo for _s, lo, _h in win_rows)
        chunk_hi = max(hi for _s, _l, hi in win_rows)

        try:
            dim_where = ["source_collection = %s", "asset_class = 'option'"]
            dim_params: list[Any] = [root]
            if type in ("C", "P"):
                dim_where.append("option_type = %s")
                dim_params.append(type.upper())
            # Cycle predicate — MUST match the full-chain path so a cross-cycle
            # duplicate symbol (~2.68% OPT_SP_500) collapses to the SAME surviving
            # instrument_id(s).  See the method docstring.
            _cycle_frag, _cycle_val = _cycle_predicate(expiration_cycle)
            if _cycle_frag is not None:
                dim_where.append(_cycle_frag)
                dim_params.append(_cycle_val)

            value_rows: list[str] = []
            win_params: list[Any] = []
            for i, (sym, lo, hi) in enumerate(win_rows):
                value_rows.append(
                    "(%s::text, %s::date, %s::date)" if i == 0 else "(%s, %s, %s)"
                )
                win_params.extend([sym, lo, hi])

            sql = f"""
                WITH heldwin (sym, lo, hi) AS (
                    VALUES {", ".join(value_rows)}
                ),
                ids AS (
                    SELECT d.instrument_id, d.symbol AS option_symbol, d.root_symbol,
                           d.underlying_symbol, d.expiration, d.expiration_cycle,
                           d.strike, d.option_type, d.contract_size, d.currency,
                           d.provider
                    FROM {SCHEMA}.dim_instrument d
                    JOIN heldwin h ON d.symbol = h.sym
                    WHERE {" AND ".join(dim_where)}
                ),
                keyset AS (
                    SELECT p.instrument_id, p.trade_date
                    FROM {SCHEMA}.fact_price_eod p
                    JOIN ids i ON i.instrument_id = p.instrument_id
                    JOIN heldwin h ON h.sym = i.option_symbol
                    WHERE p.trade_date BETWEEN h.lo AND h.hi
                      AND p.trade_date BETWEEN %s AND %s
                    UNION
                    SELECT g.instrument_id, g.trade_date
                    FROM {SCHEMA}.fact_option_greeks g
                    JOIN ids i ON i.instrument_id = g.instrument_id
                    JOIN heldwin h ON h.sym = i.option_symbol
                    WHERE g.trade_date BETWEEN h.lo AND h.hi
                      AND g.trade_date BETWEEN %s AND %s
                )
                SELECT k.trade_date,
                       {_CHAIN_SELECT_COLS}
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
                {_CHAIN_ORDER_BY}
            """
            params: list[Any] = []
            params.extend(win_params)  # VALUES
            params.extend(dim_params)  # ids WHERE
            params.extend([chunk_lo, chunk_hi])  # keyset price prune
            params.extend([chunk_lo, chunk_hi])  # keyset greeks prune
            params.extend([chunk_lo, chunk_hi])  # final price join
            params.extend([chunk_lo, chunk_hi])  # final greeks join

            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    raw = await cur.fetchall()

                # [Gotcha 5] dollarize crypto premiums per trade date.
                row_dates = sorted({m["trade_date"] for m in raw})
                spot_by_date = await self._coin_spot_map(conn, root, row_dates)

            for m in raw:
                row_date: date = m["trade_date"]
                contract = self._chain_meta_to_contract(root, m)
                row = self._row_from_chain(
                    m,
                    target_date=row_date,
                    coin_spot=spot_by_date.get(row_date),
                )
                results.setdefault(row_date, []).append((contract, row))
            return results
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error querying held rows on '{root}' for "
                f"{len(win_rows)} symbols: {exc}"
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
        expiration_max: date | None = None,
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

        ``expiration_max`` (optional) caps the expirations considered.  A
        ``NearestToTarget`` caller passes ``end + max(3*target_dte_days, 180)``
        — the SAME upper bound the resolver's own probe window uses (see
        ``stream_resolver`` ``far_future``), so no expiration the resolver could
        pick is dropped, but far-dated LEAPS (which are never nearest-to-target)
        no longer inflate the scan (measured up to ~9s/leg on a wide window).
        ``None`` = no upper bound (legacy behaviour).

        PUSHDOWN + partition pruning: resolve matching option ``instrument_id``s
        via the indexed ``source_collection`` dim lookup (type / cycle /
        ``expiration >= start`` [+ optional ``expiration <= expiration_max``]
        pushed), then join ``fact_price_eod`` on a CONSTANT ``trade_date BETWEEN
        start AND end`` so the planner prunes to the spanned year partitions
        (the same gotcha the bulk chain reader honours; a runtime-only join fans
        out across all ~71 partitions).  Price-row based (the tradeable
        universe): the ``fact_price_eod`` join means an expiration appears only
        when a contract of it has an EOD price row that day.  (A greeks-only
        listing — a row present in ``fact_option_greeks`` but not
        ``fact_price_eod`` — would be excluded; none are observed in dwh today,
        and the bulk chain reader's keyset UNIONs greeks so it could in
        principle surface one this listing would miss.)
        """
        try:
            dim_where = [
                "source_collection = %s",
                "asset_class = 'option'",
                "expiration IS NOT NULL",
                "expiration >= %s",
            ]
            params: list[Any] = [root, start]
            if expiration_max is not None:
                dim_where.append("expiration <= %s")
                params.append(expiration_max)
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
            # COUPLED (audit_d3 INV-4): the delta-pushdown SQL rank
            # (query_chain_bulk_multi) ranks on RAW ``g.delta`` with NO transform.
            # If any sanitize/scale/sign step is ever added to delta_stored here
            # (mirroring _sanitize_iv / _scale), it MUST be mirrored into that CTE
            # or the pushdown pick silently diverges from match_by_delta.
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

    async def _first_trade_date(self, conn: Any, collection: str) -> date | None:
        """Earliest ``trade_date`` with a bar in *collection* (data start).

        Mirror image of ``_last_trade_date``: a ``min(trade_date)`` over a whole
        root seq-scans every partition and times out (the fact's only btree is
        the composite PK), but a ``min`` over a SINGLE ``instrument_id`` is a
        fast PK index scan.

        So: pick ONE representative early contract — the earliest-expiring
        contract (``expiration ASC``, indexed dim lookup) — then
        ``min(trade_date)`` for just that instrument. The earliest-expiring
        contract is the first to be listed, so its first bar closely tracks the
        root's true data start.

        NOTE (approximation, by design — matches the ``_last_trade_date``
        contract): a longer-dated contract listed even earlier could carry a
        marginally earlier bar, so the returned start may lag the true first
        bar by a small margin. This is acceptable — the value seeds the
        portfolio date-slider floor, and the goal is to expose the real
        multi-decade history (~2005/2006 for SPX/VIX) rather than an artificial
        recent floor; a few weeks of slack at the very start is immaterial.
        Returns ``None`` only when the root has no dated contracts at all.
        """
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT instrument_id FROM {SCHEMA}.dim_instrument
                    WHERE source_collection = %s AND expiration IS NOT NULL
                    ORDER BY expiration ASC
                    LIMIT 1""",
                (collection,),
            )
            row = await cur.fetchone()
            if row is None:
                return None

            await cur.execute(
                f"""SELECT min(trade_date) AS d
                    FROM {SCHEMA}.fact_price_eod
                    WHERE instrument_id = %s""",
                (row["instrument_id"],),
            )
            res = await cur.fetchone()
            return res["d"] if res else None

    async def trade_date_coverage(self, root: str) -> tuple[date | None, date | None]:
        """``(first_trade_date, last_trade_date)`` bar coverage for *root*.

        Both bounds reuse the single-representative-contract heuristic (see
        ``_first_trade_date`` / ``_last_trade_date``) to stay inside the
        statement timeout on the huge option fact. Either element is ``None``
        when the root has no usable contract. Backs the portfolio date-slider
        floor for option-only portfolios so they default to the option
        collection's TRUE history instead of an artificial recent default.
        """
        try:
            async with self._pool.connection() as conn:
                first = await self._first_trade_date(conn, root)
                last = await self._last_trade_date(conn, root)
                return (first, last)
        except Exception as exc:  # noqa: BLE001
            raise OptionsDataAccessError(
                f"SQL error reading trade-date coverage for {root}: {exc}"
            ) from exc


def _scale(value: float | None, factor: float) -> float | None:
    """Multiply *value* by *factor* when present; preserve None."""
    return None if value is None else value * factor
