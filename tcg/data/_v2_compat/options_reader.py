"""``OptionsDataReader`` implementation backed by the v2 star schema.

Serves ES weekly options out of ``tcg_instruments_v2`` while emitting the
IDENTICAL frozen DTOs (``OptionContractDoc`` / ``OptionDailyRow`` /
``OptionContractSeries`` / ``OptionRootInfo``) that
:class:`tcg.data._sql.options.SqlOptionsDataReader` emits from v1. The engine
therefore cannot tell the two apart — which is the whole point: both sources
must drive the same simulator (guardrail Sign 3).

What is DIFFERENT from v1, and why
----------------------------------
* **One logical collection, five physical objects.** ``OPT_SP_500`` fans out to
  the four EW weekly objects (EW1/EW2/EW3/EW4 = 11/12/7/13) PLUS the standard
  quarterly ES option (14). Cycle routing goes through
  :func:`~tcg.data._v2_compat._mapping.objects_for_cycle`, which adds 14 to the
  ``"W3 Friday"`` route so the quarterly-month 3rd Fridays (Mar/Jun/Sep/Dec),
  which no EW3 weekly lists, resolve; the union merges deterministically by
  ``(ts, contract_id)`` — spec §3.2. Object 14 is quarterly-only and never
  routes on W1/W2/W4; the futures object 6 (same dates, ``kind='future'``) is
  never routed at all.
* **No monthlies.** v1's ``"M"`` cycle (74,930 contracts, 2005-2030) has no v2
  counterpart. Requesting it — or requesting NO cycle at all on a chain call —
  is a hard error, never a silent weeklies-only answer (spec §3.3, D4).
* **No quotes.** v2 option objects carry ``value`` (settlement) and ``greeks``
  series only; there is no ``fact_bbba`` row for any option. So ``bid`` /
  ``ask`` / ``mid`` / ``volume`` / ``open_interest`` are ``None`` on every row,
  and asking for those as a STREAM raises (spec §4.4, D3). Settlement is never
  substituted for mid: that would fabricate agreement between the sources.
* **No duplicate contracts.** v1's ~2.7% duplicate-``instrument_id``-per-symbol
  quirk does not exist here — ``(object_id, expiration, strike, option_type)``
  is unique (spec §2.4 VERIFIED). The delta pushdown is consequently
  ROW-granular and needs none of v1's symbol-granular overflow machinery; the
  k+1 fetch is kept purely as a re-ranking margin, not as a tie safeguard.

The ``serie_id`` fan-out (guardrail Sign 7 — the silent-corruption trap)
-----------------------------------------------------------------------
One contract has a DIFFERENT ``serie_id`` per ``serie.type``: its ``value``
serie and its ``greeks`` serie are two distinct ``serie`` rows. Joining
``fact_value.serie_id = fact_greeks.serie_id`` returns ZERO rows; joining them
on a shared id assumption would pair the wrong greeks to the wrong price. Every
query below therefore pivots through ``serie.contract_id`` — see
:data:`_CHAIN_FROM`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Sequence

from tcg.data._sql.connection import DwhConnectionPool, to_float
from tcg.data._sql.options import _sanitize_iv, symbol_delta_rank
from tcg.data._v2_compat._mapping import (
    ALL_OPTION_OBJECT_IDS,
    EW_OBJECT_BY_CYCLE,
    V2_INDEX_SYMBOL,
    V2_OPTIONS_COLLECTION,
    V2_QUARTERLY_OBJECT_ID,
    date_int_bounds,
    objects_for_cycle,
    option_parts_from_symbol,
    option_symbol_from_parts,
    option_type_from_v2,
    option_type_to_v2,
)
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2MissingCycleFilter,
    V2UnsupportedField,
)
from tcg.types.errors import OptionsContractNotFound, OptionsDataAccessError
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)

logger = logging.getLogger(__name__)

V2_SCHEMA = "tcg_instruments_v2"

_UTC = timezone.utc

#: Constants the v2 adapter stamps on every emitted contract (spec §6.7).
_ROOT_UNDERLYING = V2_INDEX_SYMBOL  # NOT FUT_SP_500 — see spec §8.3.
_PROVIDER = "DATABENTO"
_DISPLAY_NAME = "SP 500"
#: v1 is NULL for OPT_SP_500 and v2 has no currency column; emitting "USD"
#: would invent a divergence that is not in the data (spec §12 Q3).
_CURRENCY: str | None = None

#: Streams v2 cannot serve for options at all (spec §4.4 / §11 E5). Exposed so
#: the wiring layer has ONE place to gate on; the reader itself never sees a
#: stream label.
V2_UNAVAILABLE_OPTION_STREAMS: frozenset[str] = frozenset(
    {"mid", "volume", "open_interest"}
)

#: v2 option ``object_id`` → the v1 ``expiration_cycle`` tag it stands for.
#: Derived from the frozen forward map so the two can never disagree. The
#: quarterly object (14) is tagged "W3 Friday" as well: its 3rd-Friday contracts
#: occupy the same monthly W3 slot as EW3, in the months EW3 omits, so the roll
#: schedule must treat them as the "W3 Friday" contract for that month.
_CYCLE_BY_OBJECT: dict[int, str] = {v: k for k, v in EW_OBJECT_BY_CYCLE.items()}
_CYCLE_BY_OBJECT[V2_QUARTERLY_OBJECT_ID] = "W3 Friday"

#: v2 option ``object_id`` → v1 ``underlying_symbol``. The EW weeklies carry
#: "EW1".."EW4"; the standard quarterly option carries "ES" (its true root).
#: Display-only (spec §3.1 VERIFIED) — never used for selection or rolling.
_UNDERLYING_BY_OBJECT: dict[int, str] = {
    obj: f"EW{cycle[1]}"
    for obj, cycle in _CYCLE_BY_OBJECT.items()
    if obj != V2_QUARTERLY_OBJECT_ID
}
_UNDERLYING_BY_OBJECT[V2_QUARTERLY_OBJECT_ID] = "ES"

#: The literal ``"W"`` tag, which ``expand_cycle`` adds for the UI's generic
#: "Weekly" but which OPT_SP_500 never uses (spec §3.1: zero rows on either
#: source). Dropping it is source-NEUTRAL — v1 matches nothing on it either.
_GENERIC_WEEKLY_TAG = "W"

# --------------------------------------------------------------------------- #
# Shared SQL — single-sourced so the projection and the serie_id pivot cannot
# drift between the query builders.
# --------------------------------------------------------------------------- #

_CHAIN_SELECT_COLS = """
        fv.ts                AS ts,
        c.contract_id        AS contract_id,
        sv.object_id         AS object_id,
        c.expiration         AS expiration,
        c.strike             AS strike,
        c.option_type        AS option_type,
        c.multiplier         AS multiplier,
        fv.value             AS settle,
        fg.delta             AS delta,
        fg.gamma             AS gamma,
        fg.vega              AS vega,
        fg.theta             AS theta,
        fg.implied_vol       AS implied_vol
"""

# GUARDRAIL Sign 7 lives here. ``sv`` is the contract's *value* serie and ``sg``
# its *greeks* serie — two different ``serie_id``s for the same contract. They
# are tied together ONLY by ``serie.contract_id``. The ``fg.ts`` bound sits in
# the LEFT JOIN's ON clause (never the WHERE) so a contract-day with a
# settlement but no greeks still yields a row with NULL greeks — which is what
# preserves v1's ``missing_delta_no_compute`` classification (spec §11 E9).
_CHAIN_FROM = f"""
    FROM {V2_SCHEMA}.serie sv
    JOIN {V2_SCHEMA}.fact_value fv ON fv.serie_id = sv.serie_id
    JOIN {V2_SCHEMA}.contract   c  ON c.contract_id = sv.contract_id
    LEFT JOIN {V2_SCHEMA}.serie sg
           ON sg.contract_id = sv.contract_id AND sg.type = 'greeks'
    LEFT JOIN {V2_SCHEMA}.fact_greeks fg
           ON fg.serie_id = sg.serie_id
          AND fg.ts = fv.ts
          AND fg.ts >= %s AND fg.ts < %s
"""

# Row order within a date is LOAD-BEARING: the resolver's ``_row_for_contract``
# takes the FIRST matching row. ``contract_id`` is v2's stable unique key and
# plays the exact role v1's ``instrument_id`` plays (spec §5.2 rule 2).
_CHAIN_ORDER_BY = "ORDER BY fv.ts, c.contract_id"

# The delta rank, expressed once. COUPLED with ``symbol_delta_rank`` (the shared
# Python reference that ``match_by_delta`` is bound to) and therefore with
# ``match_by_delta``'s PRIMARY key ``abs(delta - target)``. ``contract_id``
# substitutes for v1's ``option_symbol`` third key — on v2 the second key
# (strike) is already unique within an object, so the third only ever fires
# across two cycles sharing an (expiration, strike).
_DELTA_RANK_ORDER_BY = (
    "ORDER BY abs(fg.delta - %s) ASC NULLS LAST, c.strike ASC, c.contract_id ASC"
)


def _as_tag_list(cycle: "str | Sequence[str] | None") -> list[str] | None:
    """Normalise the cycle argument to a tag list (``None`` = no filter).

    A ``str`` is itself a ``Sequence[str]``, so the scalar test comes first —
    the same trap ``_cycle_predicate`` guards against on the v1 side.
    """
    if cycle is None:
        return None
    if isinstance(cycle, str):
        return [cycle]
    tags = list(cycle)
    return tags or None


def _route_objects(
    cycle: "str | Sequence[str] | None", *, require_filter: bool
) -> list[int]:
    """Resolve a cycle filter to the v2 ``object_id``s that serve it (spec §3.3).

    ``require_filter`` splits the two families of caller:

    * ``True`` — chain / selection reads. ``None`` raises (spec §3.3c, E4):
      answering "weeklies only" where v1 would answer "weeklies AND monthlies"
      would silently compare two different strategies.
    * ``False`` — pure INVENTORY reads (``list_expirations`` and friends, some
      of which have no cycle parameter at all). ``None`` means "everything v2
      has", which is an honest and complete answer to "what do you have?".

    An EXPLICITLY requested unavailable tag (``"M"``, ``""``) raises in both
    modes — the user asked for something v2 does not have.
    """
    tags = _as_tag_list(cycle)
    if tags is None:
        if require_filter:
            # NOT ``V2UnsupportedCycle`` — that constructor interpolates its
            # argument into "...expiration cycle '{cycle}'...", so passing a
            # sentence nests the whole paragraph inside the sibling's message.
            raise V2MissingCycleFilter()
        # Inventory "everything v2 has": include the quarterly object so the
        # answer is complete. This is a coverage path, not cycle routing.
        return list(ALL_OPTION_OBJECT_IDS)

    # Drop the generic "W" umbrella tag: OPT_SP_500 has zero rows under it on
    # BOTH sources, so dropping it changes nothing relative to v1 (spec §3.3a).
    concrete = [t for t in tags if t != _GENERIC_WEEKLY_TAG]
    if not concrete:
        # The request was exactly ("W",) — the union of every weekly cycle,
        # which on v2 is the union of all option objects (each concrete weekly
        # route's target, W3's quarterly included). Coverage-agnostic, so no
        # W1/W2/W4 leak: those are only reached via their explicit concrete tag.
        return list(ALL_OPTION_OBJECT_IDS)

    objects: list[int] = []
    for tag in concrete:
        # ``objects_for_cycle`` raises V2UnsupportedCycle on M / '' and returns
        # (7, 14) for "W3 Friday" — quarterly-aware routing.
        for obj in objects_for_cycle(tag):
            if obj not in objects:
                objects.append(obj)
    return objects


def _require_options_root(root: str) -> None:
    """Guard the single option collection v2 serves (spec §11 E1)."""
    if root != V2_OPTIONS_COLLECTION:
        # The constructor takes the COLLECTION NAME and builds the sentence;
        # passing a pre-built sentence would nest it inside itself.
        raise V2CollectionUnavailable(root)


def assert_option_stream_available(stream: str) -> None:
    """Raise when *stream* is one v2 cannot serve for options (spec §11 E5).

    The reader never sees a stream label — it returns rows — so this gate is
    exported for the wiring layer to call once, at the point the user's stream
    choice is known. Rows always carry ``mid=None``, so nothing can silently
    substitute settlement for a quote even if this gate is missed.
    """
    if stream in V2_UNAVAILABLE_OPTION_STREAMS:
        # The constructor takes the FIELD NAME and builds the sentence.
        raise V2UnsupportedField(stream)


def _type_predicate(
    type_: "Literal['C', 'P', 'both'] | None",
) -> tuple[str | None, str | None]:
    """``('C'|'P'|'both'|None)`` → the ``c.option_type`` fragment + bind value."""
    if type_ in (None, "both"):
        return None, None
    return "c.option_type = %s", option_type_to_v2(type_)


class V2OptionsDataReader:
    """Read-only ``OptionsDataReader`` over ``tcg_instruments_v2``.

    Implements the FULL protocol including the two optional bulk capabilities.
    Both flags below are truthy on purpose: the engine's ``_choose_path`` gates
    its fast paths on ``callable(getattr(reader, ...))`` / these flags, and a
    v2 run must be exactly as fast as a v1 run.
    """

    #: Feature flags the wiring layer (`_reader_supports_bulk_multi`) and the
    #: engine's `_choose_path` probe for.
    supports_bulk_multi: bool = True
    supports_held_rows: bool = True

    def __init__(self, pool: DwhConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ #
    # Single contract
    # ------------------------------------------------------------------ #
    async def get_contract(
        self, collection: str, contract_id: str
    ) -> OptionContractSeries:
        """One contract with its full chronological day series.

        ``contract_id`` is the v1 option symbol. It does NOT encode which
        object the contract belongs to, and ``get_contract`` has no cycle
        parameter, so all option objects are searched — the four EW weeklies
        AND the quarterly standard option (14), else a quarterly 3rd-Friday
        symbol would not resolve. In practice the expiration date pins the
        object; where two objects did list the same (expiration, strike, type)
        the lowest ``contract_id`` wins — deterministic, and the same
        first-by-id discipline the chain path uses.
        """
        _require_options_root(collection)
        exp_int, strike, kind = option_parts_from_symbol(contract_id)
        exp_date = date(exp_int // 10000, (exp_int // 100) % 100, exp_int % 100)
        lo, hi = date_int_bounds(None, None)

        sql = f"""
            SELECT {_CHAIN_SELECT_COLS}
            {_CHAIN_FROM}
            WHERE sv.type = 'value'
              AND sv.object_id = ANY(%s)
              AND fv.ts >= %s AND fv.ts < %s
              AND c.expiration = %s
              AND c.strike = %s
              AND c.option_type = %s
            {_CHAIN_ORDER_BY}
        """
        params = [
            lo,
            hi,
            list(ALL_OPTION_OBJECT_IDS),
            lo,
            hi,
            exp_date,
            strike,
            option_type_to_v2(kind),
        ]
        rows = await self._fetch(sql, params, what=f"contract '{contract_id}'")
        if not rows:
            raise OptionsContractNotFound(
                f"Option contract '{contract_id}' not found in v2 collection "
                f"'{collection}'."
            )

        # Pin to ONE contract_id (the lowest) so a hypothetical cross-root
        # collision cannot interleave two contracts' series.
        chosen = min(r["contract_id"] for r in rows)
        picked = [r for r in rows if r["contract_id"] == chosen]
        contract = self._contract_from_row(picked[0])
        return OptionContractSeries(
            contract=contract,
            rows=tuple(self._daily_row(r) for r in picked),
        )

    # ------------------------------------------------------------------ #
    # Chains
    # ------------------------------------------------------------------ #
    async def query_chain(
        self,
        root: str,
        date: date,  # noqa: A002 — protocol parameter name
        type: Literal["C", "P", "both"],  # noqa: A002 — protocol parameter name
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: "str | Sequence[str] | None" = None,
        limit: int | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        """One ``(contract, row)`` pair per option active on *date*."""
        _require_options_root(root)
        objects = _route_objects(expiration_cycle, require_filter=True)
        lo, hi = date_int_bounds(date, date)

        where, params = self._chain_where(
            objects=objects,
            lo=lo,
            hi=hi,
            type_=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
        )
        limit_sql = ""
        if limit is not None:
            limit_sql = "\n            LIMIT %s"

        sql = f"""
            SELECT {_CHAIN_SELECT_COLS}
            {_CHAIN_FROM}
            WHERE {where}
            {_CHAIN_ORDER_BY}{limit_sql}
        """
        if limit is not None:
            params = params + [int(limit)]
        rows = await self._fetch(sql, params, what=f"chain on '{root}'")
        return [self._pair(r) for r in rows]

    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],  # noqa: A002 — protocol parameter name
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: "str | Sequence[str] | None" = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """``(contract, row)`` pairs for ALL *dates* in one cursor pass."""
        _require_options_root(root)
        objects = _route_objects(expiration_cycle, require_filter=True)
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {
            d: [] for d in dates
        }
        if not dates:
            return results

        lo, hi = date_int_bounds(min(dates), max(dates))
        where, params = self._chain_where(
            objects=objects,
            lo=lo,
            hi=hi,
            type_=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
        )
        where += " AND fv.ts = ANY(%s)"
        params = params + [_to_ts_list(dates)]

        sql = f"""
            SELECT {_CHAIN_SELECT_COLS}
            {_CHAIN_FROM}
            WHERE {where}
            {_CHAIN_ORDER_BY}
        """
        rows = await self._fetch(sql, params, what=f"bulk chain on '{root}'")
        self._accumulate(rows, results)
        return results

    async def query_chain_bulk_multi(
        self,
        root: str,
        type: Literal["C", "P", "both"],  # noqa: A002 — protocol parameter name
        groups: Sequence[tuple[date, Sequence[date]]],
        expiration_cycle: "str | Sequence[str] | None" = None,
        delta_pushdown: "tuple[float, int] | None" = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Multi-EXPIRATION bulk chain fetch in ONE query.

        ``groups`` is ``[(expiration, [trade_dates...]), ...]``; each expiration
        is restricted to its OWN date window via a ``VALUES`` keyset, and the
        whole read is bounded by one constant ``ts`` range (BRIN — guardrail
        Sign 6; v2 fact tables are NOT partitioned, so v1's redundant-constant
        partition-pruning idiom does not transfer, but the bound still helps).

        ``delta_pushdown = (target_delta, k)`` engages the DELTA PUSHDOWN. SQL
        ranks candidates per ``(expiration, trade_date)`` by
        ``|delta - target|`` (NULLS LAST, then strike, then contract_id) and
        keeps the top ``k+1``; Python then re-ranks those candidates with
        :func:`~tcg.data._sql.options.symbol_delta_rank` — the SHARED reference
        ``match_by_delta`` is bound to — and truncates to ``k``. Two-stage on
        purpose: the SQL stage is a cheap superset filter, and the Python stage
        is the authority, so the retained set provably agrees with the matcher
        rather than merely being intended to. The rank-1 winner is exact for any
        ``k >= 1`` because SQL computes ``|delta - target|`` exactly.
        """
        _require_options_root(root)
        objects = _route_objects(expiration_cycle, require_filter=True)
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        windows: list[tuple[date, date, date]] = []
        for expiration, dates in groups:
            ds = list(dates)
            if not ds:
                continue
            for d in ds:
                results.setdefault(d, [])
            windows.append((expiration, min(ds), max(ds)))
        if not windows:
            return results

        lo, hi = date_int_bounds(min(w[1] for w in windows), max(w[2] for w in windows))
        where, params = self._chain_where(objects=objects, lo=lo, hi=hi, type_=type)

        # Per-expiration date window: keeps each expiration's keyset tiny even
        # though the outer ts bound spans every group.
        values_sql = ", ".join(
            ["(%s::date, %s::timestamptz, %s::timestamptz)"] * len(windows)
        )
        win_params: list[Any] = []
        for expiration, w_lo, w_hi in windows:
            b_lo, b_hi = date_int_bounds(w_lo, w_hi)
            win_params.extend([expiration, b_lo, b_hi])

        where += (
            " AND EXISTS (SELECT 1 FROM (VALUES "
            + values_sql
            + ") AS w(expiration, lo, hi)"
            " WHERE w.expiration = c.expiration AND fv.ts >= w.lo AND fv.ts < w.hi)"
        )
        params = params + win_params

        if delta_pushdown is None:
            sql = f"""
                SELECT {_CHAIN_SELECT_COLS}
                {_CHAIN_FROM}
                WHERE {where}
                {_CHAIN_ORDER_BY}
            """
            rows = await self._fetch(
                sql, params, what=f"multi-expiration chain on '{root}'"
            )
            self._accumulate(rows, results)
            return results

        target, k = delta_pushdown
        k = max(1, int(k))
        # Fetch k+1 so the Python re-rank has a margin at the boundary.
        sql = f"""
            WITH cand AS (
                SELECT {_CHAIN_SELECT_COLS},
                       row_number() OVER (
                           PARTITION BY c.expiration, fv.ts
                           {_DELTA_RANK_ORDER_BY}
                       ) AS rn
                {_CHAIN_FROM}
                WHERE {where}
            )
            SELECT * FROM cand WHERE rn <= %s
            ORDER BY ts, contract_id
        """
        # The window's ``%s`` (target) is bound BEFORE the FROM/WHERE params
        # because it appears earlier in the statement text.
        rank_params = [target] + params + [k + 1]
        rows = await self._fetch(
            sql, rank_params, what=f"delta-pushdown chain on '{root}'"
        )

        # Stage per (expiration, trade_date) — the pushdown's rank partition —
        # keeping each row's raw ``contract_id`` so the final merge can restore
        # the SAME ``ORDER BY ts, contract_id`` the full-chain path emits.
        staged: dict[tuple[date, date], list[tuple[int, Any]]] = {}
        for r in rows:
            pair = self._pair(r)
            staged.setdefault((pair[0].expiration, pair[1].date), []).append(
                (int(r["contract_id"]), pair)
            )
        by_date: dict[date, list[tuple[int, Any]]] = {}
        for (_expiration, trade_date), entries in staged.items():
            kept = symbol_delta_rank([p for _cid, p in entries], target, k)
            # ``symbol_delta_rank`` REBUILDS its pair tuples, so the retained
            # set must be identified by SYMBOL, not by object identity. On v2
            # a symbol maps to exactly one row per date (the natural key is
            # unique — spec §2.4), so this is an exact selection.
            keep_symbols = {c.contract_id for c, _r in kept}
            by_date.setdefault(trade_date, []).extend(
                e for e in entries if e[1][0].contract_id in keep_symbols
            )
        # ``_row_for_contract`` returns the FIRST matching row, so the merge
        # order across the routed EW objects must be deterministic and must
        # match the full-chain path's.
        for trade_date, entries in by_date.items():
            entries.sort(key=lambda e: e[0])
            results.setdefault(trade_date, []).extend(p for _cid, p in entries)
        return results

    async def query_held_rows(
        self,
        root: str,
        type: Literal["C", "P", "both"],  # noqa: A002 — protocol parameter name
        held_windows: Sequence[tuple[str, date, date]],
        expiration_cycle: "str | Sequence[str] | None" = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        """Identity keyset fetch of already-SELECTED held symbols.

        SQL never ranks or picks here — selection stayed in Python. On v1 the
        ``expiration_cycle`` argument is load-bearing because a symbol is NOT
        unique across cycles (the duplicate-``instrument_id`` quirk); on v2 the
        natural key is unique, so the filter is only ROUTING. It is still
        honoured, because routing to the wrong object would return nothing.
        """
        _require_options_root(root)
        objects = _route_objects(expiration_cycle, require_filter=True)
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}

        seen: dict[str, tuple[str, date, date]] = {}
        for sym, w_lo, w_hi in held_windows:
            if not sym:
                continue
            key = str(sym)
            if key in seen:
                _s, p_lo, p_hi = seen[key]
                seen[key] = (key, min(p_lo, w_lo), max(p_hi, w_hi))
            else:
                seen[key] = (key, w_lo, w_hi)
        if not seen:
            return results

        keyset: list[Any] = []
        for sym, w_lo, w_hi in seen.values():
            exp_int, strike, kind = option_parts_from_symbol(sym)
            b_lo, b_hi = date_int_bounds(w_lo, w_hi)
            keyset.extend(
                [
                    date(exp_int // 10000, (exp_int // 100) % 100, exp_int % 100),
                    strike,
                    option_type_to_v2(kind),
                    b_lo,
                    b_hi,
                ]
            )
        values_sql = ", ".join(
            ["(%s::date, %s::numeric, %s::text, %s::timestamptz, %s::timestamptz)"]
            * len(seen)
        )

        lo, hi = date_int_bounds(
            min(w[1] for w in seen.values()), max(w[2] for w in seen.values())
        )
        where, params = self._chain_where(objects=objects, lo=lo, hi=hi, type_=type)
        where += (
            " AND EXISTS (SELECT 1 FROM (VALUES "
            + values_sql
            + ") AS h(expiration, strike, option_type, lo, hi)"
            " WHERE h.expiration = c.expiration AND h.strike = c.strike"
            " AND h.option_type = c.option_type"
            " AND fv.ts >= h.lo AND fv.ts < h.hi)"
        )
        params = params + keyset

        sql = f"""
            SELECT {_CHAIN_SELECT_COLS}
            {_CHAIN_FROM}
            WHERE {where}
            {_CHAIN_ORDER_BY}
        """
        rows = await self._fetch(sql, params, what=f"held rows on '{root}'")
        for r in rows:
            pair = self._pair(r)
            results.setdefault(pair[1].date, []).append(pair)
        return results

    # ------------------------------------------------------------------ #
    # Inventory / metadata
    # ------------------------------------------------------------------ #
    async def list_roots(self) -> list[OptionRootInfo]:
        """The ONE union root v2 serves (spec §6.11, §12 Q4).

        The five option objects are an implementation detail of ``OPT_SP_500``;
        exposing them separately here would let a user build a leg on a root
        that has no v1 counterpart.
        """
        sql = f"""
            SELECT min(c.expiration) AS exp_first,
                   max(c.expiration) AS exp_last,
                   count(*)          AS n
            FROM {V2_SCHEMA}.contract c
            WHERE c.object_id = ANY(%s)
        """
        rows = await self._fetch(
            sql, [list(ALL_OPTION_OBJECT_IDS)], what="option roots"
        )
        head: Mapping[str, Any] = rows[0] if rows else {}
        _first_td, last_td = await self.trade_date_coverage(V2_OPTIONS_COLLECTION)
        return [
            OptionRootInfo(
                collection=V2_OPTIONS_COLLECTION,
                name=_DISPLAY_NAME,
                has_greeks=True,
                providers=(_PROVIDER,),
                expiration_first=head.get("exp_first"),
                expiration_last=head.get("exp_last"),
                doc_count_estimated=int(head.get("n") or 0),
                strike_factor_verified=True,
                last_trade_date=last_td,
                # Display-only (drives the nav badge); any value >= 0.9 renders
                # identically. ~89% of contract-days carry greeks (spec §6.11 Q5).
                stored_greeks_ratio=1.0,
                has_computed_greeks=True,
            )
        ]

    async def list_expirations(self, root: str) -> list[date]:
        """Distinct expirations available on *root*, ascending."""
        return await self.list_expirations_filtered(root)

    async def get_option_root_symbol(self, root: str) -> str | None:
        """The value the adapter places on every ``root_underlying``.

        Constant for this collection, so no round-trip is needed — which is
        exactly what the caller wants (it exists to avoid a probe fetch).
        """
        _require_options_root(root)
        return _ROOT_UNDERLYING

    async def trade_date_coverage(self, root: str) -> tuple[date | None, date | None]:
        """``(first, last)`` settlement-bar coverage across all option objects.

        An EXACT global ``min/max`` over every option ``fact_value`` bar. There
        is no ``ts``-usable index for an unbounded extreme (the PK is
        ``(serie_id, ts)``; a BRIN on ``ts`` does not order an unbounded
        min/max) and — unlike v1 — v2 has no cheap representative shortcut, so
        this genuinely scans the fact (~10s). A single-representative-serie
        heuristic was rejected: it is not provably exact (a later-expiring serie
        could carry an earlier/later bar). This no-cycle read is NOT on the
        portfolio cache-status hot path (that goes through
        :meth:`cycle_trade_date_span`); it backs the nav badge / collection-wide
        coverage endpoint only.
        """
        _require_options_root(root)
        sql = f"""
            SELECT min(fv.ts) AS lo, max(fv.ts) AS hi
            FROM {V2_SCHEMA}.serie sv
            JOIN {V2_SCHEMA}.fact_value fv ON fv.serie_id = sv.serie_id
            WHERE sv.type = 'value' AND sv.object_id = ANY(%s)
        """
        rows = await self._fetch(
            sql, [list(ALL_OPTION_OBJECT_IDS)], what=f"trade-date coverage on '{root}'"
        )
        if not rows:
            return None, None
        lo, hi = rows[0]["lo"], rows[0]["hi"]
        return (_ts_date(lo), _ts_date(hi))

    async def list_expirations_filtered(
        self,
        root: str,
        option_type: Literal["C", "P"] | None = None,
        cycle: "str | Sequence[str] | None" = None,
    ) -> list[date]:
        """Distinct expirations filtered by type and/or cycle.

        An INVENTORY read: ``cycle=None`` answers "every expiration v2 has",
        which is complete and honest. An explicitly requested unavailable cycle
        still raises.
        """
        _require_options_root(root)
        objects = _route_objects(cycle, require_filter=False)
        where = ["c.object_id = ANY(%s)"]
        params: list[Any] = [objects]
        frag, bind = _type_predicate(option_type)
        if frag:
            where.append(frag)
            params.append(bind)
        sql = f"""
            SELECT DISTINCT c.expiration AS expiration
            FROM {V2_SCHEMA}.contract c
            WHERE {" AND ".join(where)}
            ORDER BY expiration
        """
        rows = await self._fetch(sql, params, what=f"expirations on '{root}'")
        return [r["expiration"] for r in rows]

    async def list_expirations_by_date(
        self,
        root: str,
        start: date,
        end: date,
        option_type: Literal["C", "P"] | None = None,
        cycle: "str | Sequence[str] | None" = None,
        expiration_max: date | None = None,
    ) -> dict[date, list[date]]:
        """Per-trade-date map of expirations actually LISTED (settlement-quoted).

        A fact join, not a dim-only set — the resolver's ``NearestToTarget``
        needs to snap to an expiration that really traded that day.
        """
        _require_options_root(root)
        objects = _route_objects(cycle, require_filter=False)
        lo, hi = date_int_bounds(start, end)
        where = [
            "sv.type = 'value'",
            "sv.object_id = ANY(%s)",
            "fv.ts >= %s",
            "fv.ts < %s",
        ]
        params: list[Any] = [objects, lo, hi]
        frag, bind = _type_predicate(option_type)
        if frag:
            where.append(frag)
            params.append(bind)
        if expiration_max is not None:
            where.append("c.expiration <= %s")
            params.append(expiration_max)

        sql = f"""
            SELECT DISTINCT fv.ts AS ts, c.expiration AS expiration
            FROM {V2_SCHEMA}.serie sv
            JOIN {V2_SCHEMA}.fact_value fv ON fv.serie_id = sv.serie_id
            JOIN {V2_SCHEMA}.contract   c  ON c.contract_id = sv.contract_id
            WHERE {" AND ".join(where)}
            ORDER BY ts, expiration
        """
        rows = await self._fetch(sql, params, what=f"per-date expirations on '{root}'")
        out: dict[date, list[date]] = {}
        for r in rows:
            out.setdefault(_ts_date(r["ts"]), []).append(r["expiration"])
        return out

    async def cycle_trade_date_span(
        self,
        root: str,
        start: date | None = None,
        end: date | None = None,
        cycle: "str | Sequence[str] | None" = None,
    ) -> tuple[date | None, date | None]:
        """EXACT ``(first, last)`` settlement ``ts`` for ONE ``cycle``.

        A two-value ``min/max`` aggregate over ``fact_value`` for the cycle's
        routed ``object_id``s (:func:`_route_objects`) — the bounded counterpart
        of :meth:`list_expirations_by_date`, identical to ``min``/``max`` of the
        per-date map's keys for the same arguments, without materialising every
        settlement bar. A cycle's objects are a SUBSET of all option objects, so
        the object-scoped scan is far cheaper than the collection-wide
        no-cycle coverage (e.g. W3's two objects are ~4s vs the ~10s union) and
        stays EXACT — unlike a representative-serie heuristic, which is not
        provably correct.

        ``start``/``end`` are OPTIONAL. When given they bound the scan to the
        half-open ``[start, end)`` UTC window; when omitted the true unbounded
        cycle extent is returned (byte-identical whenever the window covers the
        data, which lets the coverage handler skip the collection pre-fetch).
        Returns ``(None, None)`` when the cycle has no bar.
        """
        _require_options_root(root)
        objects = _route_objects(cycle, require_filter=False)
        where = ["sv.type = 'value'", "sv.object_id = ANY(%s)"]
        params: list[Any] = [objects]
        if start is not None or end is not None:
            ts_lo, ts_hi = date_int_bounds(start, end)
            where.append("fv.ts >= %s")
            params.append(ts_lo)
            where.append("fv.ts < %s")
            params.append(ts_hi)
        sql = f"""
            SELECT min(fv.ts) AS lo, max(fv.ts) AS hi
            FROM {V2_SCHEMA}.serie sv
            JOIN {V2_SCHEMA}.fact_value fv ON fv.serie_id = sv.serie_id
            WHERE {" AND ".join(where)}
        """
        rows = await self._fetch(
            sql, params, what=f"cycle trade-date span on '{root}'"
        )
        if not rows:
            return None, None
        return (_ts_date(rows[0]["lo"]), _ts_date(rows[0]["hi"]))

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _chain_where(
        self,
        *,
        objects: Sequence[int],
        lo: datetime,
        hi: datetime,
        type_: "Literal['C', 'P', 'both'] | None",
        expiration_min: date | None = None,
        expiration_max: date | None = None,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the shared WHERE fragment + its binds, in statement order.

        The two leading ``lo, hi`` binds belong to :data:`_CHAIN_FROM`'s greeks
        LEFT JOIN, which appears BEFORE the WHERE clause in the statement — so
        they must lead the parameter list.
        """
        params: list[Any] = [lo, hi]  # _CHAIN_FROM's greeks ts bound
        where = [
            "sv.type = 'value'",
            "sv.object_id = ANY(%s)",
            "fv.ts >= %s",
            "fv.ts < %s",
        ]
        params.extend([list(objects), lo, hi])
        frag, bind = _type_predicate(type_)
        if frag:
            where.append(frag)
            params.append(bind)
        if expiration_min is not None:
            where.append("c.expiration >= %s")
            params.append(expiration_min)
        if expiration_max is not None:
            where.append("c.expiration <= %s")
            params.append(expiration_max)
        if strike_min is not None:
            where.append("c.strike >= %s")
            params.append(strike_min)
        if strike_max is not None:
            where.append("c.strike <= %s")
            params.append(strike_max)
        return " AND ".join(where), params

    async def _fetch(
        self, sql: str, params: Sequence[Any], *, what: str
    ) -> list[dict[str, Any]]:
        """Run a read, wrapping transport failures as ``OptionsDataAccessError``.

        The typed ``V2*`` errors are raised BEFORE any query runs (they are
        request-shape problems, HTTP 400); anything that fails here is a real
        warehouse failure and keeps the pre-existing 502 semantics.
        """
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(sql, list(params))
                return await cur.fetchall()
        except Exception as exc:  # noqa: BLE001 — re-raised as a typed error
            raise OptionsDataAccessError(f"v2 SQL error reading {what}: {exc}") from exc

    def _accumulate(
        self,
        rows: Iterable[Mapping[str, Any]],
        results: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        """Group ``(contract, row)`` pairs under their fact trade date."""
        for r in rows:
            pair = self._pair(r)
            results.setdefault(pair[1].date, []).append(pair)

    def _pair(self, r: Mapping[str, Any]) -> tuple[OptionContractDoc, OptionDailyRow]:
        return self._contract_from_row(r), self._daily_row(r)

    def _contract_from_row(self, r: Mapping[str, Any]) -> OptionContractDoc:
        """Build the v1-shaped ``OptionContractDoc`` (spec §6.7)."""
        object_id = int(r["object_id"])
        expiration: date = r["expiration"]
        strike = to_float(r["strike"]) or 0.0
        kind = option_type_from_v2(r["option_type"])
        exp_int = expiration.year * 10000 + expiration.month * 100 + expiration.day
        return OptionContractDoc(
            collection=V2_OPTIONS_COLLECTION,
            contract_id=option_symbol_from_parts(exp_int, strike, kind),
            root_underlying=_ROOT_UNDERLYING,
            # v1 also hardcodes None here (the Mongo FUT ref was never carried
            # into the warehouse) — matching it avoids a gratuitous divergence.
            underlying_ref=None,
            underlying_symbol=_UNDERLYING_BY_OBJECT.get(object_id),
            expiration=expiration,
            expiration_cycle=_CYCLE_BY_OBJECT.get(object_id, ""),
            strike=strike,
            type=kind,  # type: ignore[arg-type]  # option_type_from_v2 → 'C'|'P'
            contract_size=to_float(r["multiplier"]),
            currency=_CURRENCY,
            provider=_PROVIDER,
            strike_factor_verified=True,
        )

    def _daily_row(self, r: Mapping[str, Any]) -> OptionDailyRow:
        """Build the v1-shaped ``OptionDailyRow`` (spec §4.4 / §6.8).

        Every quote-derived field is ``None`` — v2 has no option order book at
        all. Settlement goes on ``close`` and is NEVER copied onto ``mid``:
        the two are different quantities (they differ by a 2.64% median, spec
        D1), and silently equating them would make a v1↔v2 comparison agree for
        a reason that is not true.
        """
        return OptionDailyRow(
            date=_ts_date(r["ts"]),
            open=None,
            high=None,
            low=None,
            close=to_float(r["settle"]),
            bid=None,
            ask=None,
            bid_size=None,
            ask_size=None,
            volume=None,
            open_interest=None,
            mid=None,
            iv_stored=_sanitize_iv(to_float(r["implied_vol"])),
            # COUPLED (mirrors v1's audit_d3 INV-4): the delta-pushdown SQL
            # ranks on RAW ``fg.delta`` with NO transform, and spec §5.2 VERIFIED
            # that v2 uses the same signed [-1, 1] convention as v1. Any
            # sanitize/scale/sign step added here MUST be mirrored into
            # _DELTA_RANK_ORDER_BY or the pushdown pick diverges from
            # match_by_delta.
            delta_stored=to_float(r["delta"]),
            gamma_stored=to_float(r["gamma"]),
            # theta is per YEAR and vega per 1.00 vol on v2 (v1: per day / per
            # 1%). Deliberately NOT rescaled — spec §5.3 / §11 E6: a fabricated
            # agreement between two different pricing models is worse than a
            # visible, explained disagreement.
            theta_stored=to_float(r["theta"]),
            vega_stored=to_float(r["vega"]),
            # v1's fact_option_greeks.underlying_price is entirely NULL for
            # OPT_SP_500, so None is exact parity, not a gap (spec §5.4).
            underlying_price_stored=None,
        )


def _ts_date(ts: Any) -> Any:
    """v2 ``timestamptz`` → ``date``, explicitly in UTC (spec §7)."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.date() if ts.tzinfo is None else ts.astimezone(_UTC).date()
    return ts


def _to_ts_list(dates: Sequence[date]) -> list[datetime]:
    """Engine dates → the exact 00:00 UTC ``ts`` values v2 stores (spec §7)."""
    return [datetime(d.year, d.month, d.day, tzinfo=_UTC) for d in dates]
