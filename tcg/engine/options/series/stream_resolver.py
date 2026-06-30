"""Materialise a single option-stream time series.

This is the engine-layer counterpart of the API ``OptionStreamRef``
SeriesRef variant.  Given a list of trade dates plus a selection
criterion, maturity rule, root, type, optional cycle filter, and a
target stream label, it produces a dense ``float64`` array (one value
per date, NaN where the value is missing or the selection failed) and
a parallel ``error_code`` list (None on success, otherwise a string
diagnostic).

Independence
------------
This module imports **only** from ``tcg.types.options`` and
``tcg.engine.options.*`` — never from ``tcg.data.*`` and never from
``tcg.core.*``.  The import-linter ``engine-data-isolation`` contract
is the binding constraint.  The wiring layer in
``tcg.core.api.indicators`` translates the Pydantic SeriesRef
(carrying Pydantic v2 ``MaturityRule`` / ``SelectionCriterion``) into
the dataclass twins from ``tcg.types.options`` before calling
:func:`resolve_option_stream`.

Cycle filtering
---------------
``DefaultOptionsSelector.select`` calls ``ChainReaderPort.query_chain``
without passing ``expiration_cycle``.  To honour a caller-supplied
``cycle`` we wrap the injected reader in a thin proxy that always
passes the chosen cycle through.  ``cycle=None`` is a pass-through
(no filter applied).  See guardrail 11 — cycle is first-class.

Bulk pre-fetch path
-------------------
When a ``bulk_chain_reader`` (cycle-aware variant) is supplied, the
resolver uses a three-phase bulk path instead of per-date chain queries:

  Phase A — Pre-resolve expirations for all dates (one probe query for
            NearestToTarget, or pure date arithmetic for other rules).
  Phase B — Group dates by resolved expiration and issue one
            ``query_chain_bulk`` call per unique expiration.
  Phase C — Per-date selection + stream extraction against the pre-fetched
            chain index (no I/O except underlying-price lookups for
            ByMoneyness).

The fallback per-date path remains intact when ``bulk_chain_reader``
is ``None``.

Per-date call count and concurrency (legacy per-date path)
------------------------------------------------------------
The data layer exposes no date-range chain query — see recon doc and
``tcg.data.options.protocol.OptionsDataReader.query_chain``.  This
materialiser issues, per trade date::

    if maturity == NearestToTarget:   2 chain queries
        - one wide-window probe (selector resolves expirations)
        - one narrow-window query at the resolved expiration
        (the per-request ``CachedChainReader`` may coalesce these
         when keys match — currently they differ, see _options_wiring)
    else:                             1 chain query
        - direct query at the resolved expiration

For an N-day backtest this is N (or 2N for NearestToTarget) dwh
round-trips.  The per-date tasks run **concurrently** under
``asyncio.gather`` with a bounded semaphore (see
:data:`_MAX_INFLIGHT_PER_DATE`) — wall-clock latency is therefore
roughly ``ceil(N / _MAX_INFLIGHT_PER_DATE) × dwh_RTT``, not
``N × dwh_RTT`` as a serial loop would give.  Total query count is
unchanged from the serial loop.  The semaphore is sized to the dwh
connection pool (``_DWH_RESOLVE_CONCURRENCY``) — each concurrent task
holds a pool connection, so the fan-out must stay within ``max_size``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Callable, Literal, Protocol, Sequence, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from tcg.engine.options.maturity.protocol import MaturityResolver
from tcg.engine.options.maturity.resolver import _add_months, last_trading_day_of_month
from tcg.engine.options.selection._match import (
    match_by_delta,
    match_by_moneyness,
    match_by_strike,
)
from tcg.engine.options.selection._ports import (
    UnderlyingPriceResolver,
)
from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.market import DEFAULT_DWH_POOL_MAX_SIZE
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    EndOfMonth,
    MaturitySpec,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    RollOffset,
    SelectionCriterion,
    SelectionResult,
)

_log = logging.getLogger(__name__)


# Bounded concurrency for the resolver's per-date / per-expiration chain
# queries.  Each concurrent task independently acquires a dwh pool connection
# (``async with pool.connection()``), so the resolver's fan-out MUST stay within
# the pool's capacity or callers block on ``pool.connection()`` past the 30s
# acquire window → ``PoolTimeout`` (this is exactly the OPT_SP_500 basket-load
# failure: a 2-year window has ~95 expirations → ~95 fetch tasks vying for a
# 4-slot pool).
#
# DERIVE the cap from the single source of truth ``DEFAULT_DWH_POOL_MAX_SIZE``
# (in ``tcg.types.market``, also the pool's ``max_size`` default) so the two
# cannot drift — reserve ONE slot for the interleaved ``list_expirations`` /
# underlying-price / spot-map lookups that share the same pool.  At max_size=4
# this is 3.  (NOTE: previously 16/8, sized for the OLD Mongo/Motor 100-slot
# pool and never re-tuned after the Postgres cutover (#58) — that over-
# subscription is what caused the PoolTimeout.)
_DWH_RESOLVE_CONCURRENCY = max(1, DEFAULT_DWH_POOL_MAX_SIZE - 1)

# Per-date fallback path cap (was 16). Capped at the pool-derived concurrency.
_MAX_INFLIGHT_PER_DATE = _DWH_RESOLVE_CONCURRENCY


# Streams readable off ``OptionDailyRow``.  Mirrors the Pydantic
# ``OptionStreamLabel`` literal at the API boundary; redeclared here
# so the engine layer does not import from ``tcg.core``.
StreamLabel = Literal[
    "mid",
    "iv",
    "delta",
    "gamma",
    "vega",
    "theta",
    "open_interest",
    "volume",
]


# Map stream label → ``OptionDailyRow`` attribute.  Centralised so a
# missing-field bug surfaces as a KeyError at construction time, not a
# silent NaN at runtime.
_STREAM_TO_ATTR: dict[str, str] = {
    "mid": "mid",
    "iv": "iv_stored",
    "delta": "delta_stored",
    "gamma": "gamma_stored",
    "vega": "vega_stored",
    "theta": "theta_stored",
    "open_interest": "open_interest",
    "volume": "volume",
}


def _missing_code_for(stream: str) -> str:
    """Per-date error code when the contract was selected but the stream
    value on the row was ``None``.  Per-stream code keeps the
    diagnostic surface specific (a frontend filter can show
    ``missing_iv`` separately from ``missing_delta``)."""
    return f"missing_{stream}"


def _apply_roll_offset(d: date, roll_offset: RollOffset) -> date:
    """Shift ``d`` forward by the ROLL-EARLY offset (``days`` or ``months``).

    The maturity rule is resolved as of the shifted date, so a positive offset
    makes every roll fire that much earlier.  ``months`` uses the same
    month-arithmetic (day clamped to month-end) as the maturity resolver's own
    ``offset_months`` so the two month axes behave consistently.  ``value == 0``
    returns ``d`` unchanged (the common no-op default)."""
    if roll_offset.value == 0:
        return d
    if roll_offset.unit == "months":
        return _add_months(d, roll_offset.value)
    return d + timedelta(days=roll_offset.value)


def _snap_to_listed(target: date, listed: Sequence[date]) -> date | None:
    """Snap an arithmetic-maturity *target* to the nearest LISTED expiration.

    Mirrors ``NearestToTarget`` (no distance cap, decision D2): the
    arithmetic-maturity rules compute a target date that may not correspond to
    any real contract; this snaps it to the closest expiration the root
    actually lists.  Tie-break: the EARLIER expiration wins (parity with the
    ``(delta, dte)`` tie-break in ``resolve_with_chain`` — a smaller-DTE / earlier
    contract is preferred when equidistant).

    Returns the snapped expiration, or ``None`` when *listed* is empty (caller
    then keeps the raw arithmetic target / its existing fallback).
    """
    if not listed:
        return None
    # listed is pre-sorted ascending by the caller; min() with a stable key
    # therefore returns the earliest on a tie.
    return min(listed, key=lambda e: (abs((e - target).days), e))


@runtime_checkable
class _CycleAwareReader(Protocol):
    """Structural shape of a chain reader that accepts ``expiration_cycle``.

    The default ``ChainReaderPort`` (selection layer) does not include
    the cycle parameter, but the data-layer ``OptionsDataReader`` does
    — see ``tcg/data/options/protocol.py``.  Many real wirings (notably
    the ``_OptionsDataPortAdapter`` and ``CachedChainReader`` in
    ``tcg.core.api._options_wiring``) accept the parameter as well.
    """

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
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]: ...


@runtime_checkable
class _CycleAwareBulkReader(Protocol):
    """Structural shape of a bulk chain reader that accepts ``expiration_cycle``.

    Mirrors ``_CycleAwareReader`` but for bulk queries across multiple dates.
    The engine-side ``BulkChainReaderPort`` does NOT include ``expiration_cycle``
    — cycle injection is handled by ``_CycleInjectingBulkReader``.  The concrete
    data-layer adapter (in ``tcg.core.api._options_wiring``) implements this
    cycle-aware variant.
    """

    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]: ...


class _CycleInjectingReader:
    """Wrap a chain reader to always pass a fixed ``expiration_cycle``.

    The selector emits ``query_chain`` calls without an
    ``expiration_cycle`` argument; this wrapper injects the caller's
    cycle so the resolver honours guardrail 11 (no silent cycle mixing).
    ``cycle=None`` is a pass-through — equivalent to no wrapping at all
    — but we still wrap to keep the call site uniform.
    """

    def __init__(self, inner: _CycleAwareReader, cycle: str | None) -> None:
        self._inner = inner
        self._cycle = cycle

    async def query_chain(
        self,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        return await self._inner.query_chain(
            root=root,
            date=date,
            type=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
            expiration_cycle=self._cycle,
        )


class _CycleInjectingBulkReader:
    """Wrap a cycle-aware bulk reader to always pass a fixed ``expiration_cycle``.

    Analogous to ``_CycleInjectingReader`` but for the bulk path.
    The engine-side ``BulkChainReaderPort`` (in ``_ports.py``) does not
    include ``expiration_cycle``; this wrapper bridges the gap by
    injecting the caller's cycle into the underlying cycle-aware reader.
    """

    def __init__(self, inner: _CycleAwareBulkReader, cycle: str | None) -> None:
        self._inner = inner
        self._cycle = cycle

    async def query_chain_bulk(
        self,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        return await self._inner.query_chain_bulk(
            root=root,
            dates=dates,
            type=type,
            expiration_min=expiration_min,
            expiration_max=expiration_max,
            strike_min=strike_min,
            strike_max=strike_max,
            expiration_cycle=self._cycle,
        )


def _row_for_contract(
    rows: list[tuple[OptionContractDoc, OptionDailyRow]],
    contract: OptionContractDoc,
) -> OptionDailyRow | None:
    """Find the row whose contract matches ``contract``.

    Uses ``contract_id`` equality (the contract id is stable across
    chain queries on a single date).  Returns ``None`` when no row
    matches — defensive; should not happen since ``contract`` came
    from the same chain.
    """
    for c, r in rows:
        if c.contract_id == contract.contract_id:
            return r
    return None  # pragma: no cover (defensive)


# ---------------------------------------------------------------------------
# Bulk pre-fetch path (Phase A → B → C)
# ---------------------------------------------------------------------------


async def _resolve_bulk(
    *,
    dates: Sequence[date],
    collection: str,
    option_type: Literal["C", "P"],
    cycle: str | None,
    maturity: MaturitySpec,
    selection: SelectionCriterion,
    stream: StreamLabel,
    roll_offset: RollOffset = RollOffset(),
    chain_reader: _CycleAwareReader,
    bulk_chain_reader: _CycleAwareBulkReader,
    maturity_resolver: MaturityResolver,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    last_trade_date: date | None,
    progress_callback: Callable[[], None] | None,
    available_expirations: Sequence[date] | None,
    concurrency_gate: "asyncio.Semaphore | None" = None,
) -> tuple[NDArray[np.float64], list[str | None], list[OptionContractDoc | None]]:
    """Three-phase bulk resolver: pre-resolve expirations, bulk fetch,
    per-date selection.

    Phase A: resolve expirations for all dates.  When
             ``available_expirations`` is provided (pre-fetched via
             ``list_expirations`` — a fast distinct index scan), the
             expensive probe query is skipped entirely.  When ``None``,
             falls back to the probe query for NearestToTarget.
    Phase B: group dates by resolved expiration, issue 1 bulk query per
             unique expiration, merge into a chain index.
    Phase C: per-date selection + stream extraction against the
             pre-fetched chain index (no I/O except underlying-price
             lookups for ByMoneyness).

    Returns
    -------
    Tuple ``(values, error_codes, contracts)`` where ``contracts[i]`` is
    the ``OptionContractDoc`` chosen on date ``dates[i]`` (or ``None``
    when selection failed / chain missing).  Roll-event derivation is
    done at the API layer over the parallel ``contracts`` array.
    """
    attr_name = _STREAM_TO_ATTR[stream]
    n = len(dates)
    values: NDArray[np.float64] = np.full(n, np.nan, dtype=np.float64)
    error_codes: list[str | None] = [None] * n
    contracts: list[OptionContractDoc | None] = [None] * n

    # Wrap bulk reader with cycle injection.
    bulk_reader = _CycleInjectingBulkReader(bulk_chain_reader, cycle)

    # ── Phase A: Pre-resolve expirations for all dates ──────────────

    # Identify which dates are queryable (not past last_trade_date).
    queryable: list[tuple[int, date]] = []
    for i, d in enumerate(dates):
        if last_trade_date is not None and d > last_trade_date:
            error_codes[i] = "past_last_trade_date"
        else:
            queryable.append((i, d))

    # Resolve expirations.  For NearestToTarget we need a probe query to
    # enumerate available expirations; for other rules it is pure date
    # arithmetic with no I/O.
    expirations: dict[int, date | None] = {}  # index → resolved expiration
    # index → "snapped_to:<iso>" note for arithmetic targets that were snapped
    # to a listed expiration.  Folded into ``error_codes`` AFTER Phase C, only
    # for dates that resolved to a real value (a success-side diagnostic; the
    # failure channel must stay clean so Phase C still runs selection).
    snap_notes: dict[int, str] = {}

    if isinstance(maturity, NearestToTarget):
        if queryable:
            first_date = queryable[0][1]
            last_date = queryable[-1][1]
            probe_days = max(maturity.target_dte_days * 3, 180)
            # Use last_date for the upper bound so expirations needed by
            # trade dates near the end of the range are included.
            far_future = last_date + timedelta(days=probe_days)
            # Loosen the lower bound so expirations slightly before
            # first_date are included — an expiration that falls between
            # (first_date - target_dte_days) and first_date may still be
            # the nearest-to-target for early trade dates with small DTE.
            lower_bound = first_date - timedelta(days=max(maturity.target_dte_days, 7))

            if available_expirations is not None:
                # Fast path: caller pre-fetched all expirations via
                # list_expirations (distinct index scan — subsecond).
                # Filter to the probe window locally.
                available = [
                    e for e in available_expirations if lower_bound <= e <= far_future
                ]
            else:
                # Fallback: expensive probe query materialises the full
                # chain to extract expiration dates.
                cycle_reader = _CycleInjectingReader(chain_reader, cycle)
                probe_rows = await cycle_reader.query_chain(
                    root=collection,
                    date=first_date,
                    type=option_type,
                    expiration_min=lower_bound,
                    expiration_max=far_future,
                )
                available = sorted({c.expiration for c, _r in probe_rows})

            for idx, d in queryable:
                if not available:
                    expirations[idx] = None
                else:
                    # roll_offset (ROLL-EARLY axis): resolve maturity as of
                    # (d + offset) so every roll happens that much earlier.
                    expirations[idx] = maturity_resolver.resolve_with_chain(
                        ref_date=_apply_roll_offset(d, roll_offset),
                        rule=maturity,
                        available_expirations=available,
                    )
    else:
        # Pure date-arithmetic rules (EndOfMonth / PlusNDays / FixedDate /
        # NextThirdFriday) compute a target expiration with no chain-existence
        # check.  When the caller supplied the root's listed expirations, SNAP
        # the arithmetic target to the nearest listed one (decision D2:
        # unconditional, like NearestToTarget which has no distance cap) so a
        # target that no contract matches (daily-expiry roots, sparse listings)
        # no longer produces a silent all-NaN ``no_chain_for_date`` series.  A
        # per-date ``snapped_to:<iso>`` diagnostic records the substitution.
        listed = sorted(available_expirations) if available_expirations else []

        def _resolve_arith(idx: int, d: date) -> tuple[date | None, str | None]:
            """Resolve the arithmetic maturity for one ref date ``d``.

            Returns ``(expiration_or_None, snap_note_or_None)``.  On a resolver
            failure it sets ``error_codes[idx] = "maturity_resolution_failed"``
            (hardening finding D — a non-TCGError, e.g. a calendar month with
            zero business days, must become a per-date NaN, not a 500) and
            returns ``(None, None)``.  Issue #2's snap-to-listed is applied here
            so it is preserved on BOTH the per-date and the EOM-roll (held) path.
            """
            try:
                target = maturity_resolver.resolve(
                    ref_date=_apply_roll_offset(d, roll_offset), rule=maturity
                )
            except Exception as exc:  # noqa: BLE001
                # Dedicated code (distinct from ``no_chain_for_date``): the
                # maturity RULE itself could not be resolved, not "the chain has
                # no contract on the computed date".  Phase B preserves an
                # already-set code, so this survives the ``exp is None`` branch.
                error_codes[idx] = "maturity_resolution_failed"
                _log.debug("maturity resolve failed date=%s: %s", d, exc)
                return None, None
            snapped = _snap_to_listed(target, listed)
            if snapped is not None and snapped != target:
                # ``snapped_to:`` is a SUCCESS-side note (the value array is the
                # source of truth for NaN-ness); it is folded into ``error_codes``
                # AFTER Phase C only on dates that still resolved to a real value.
                return snapped, f"snapped_to:{snapped.isoformat()}"
            return target, None

        if isinstance(maturity, EndOfMonth):
            # END-OF-MONTH roll (Issue #3, now triggered by the maturity itself):
            # choosing ``EndOfMonth`` as the maturity IS the request to roll at
            # month-end, so hold one contract per month — re-resolve the maturity
            # only on the last TRADING day of each month (and unconditionally on
            # the FIRST queryable date), and HOLD the resolved expiration across
            # all dates until the next month-end roll.  A single forward pass is
            # correct because ``queryable`` is sorted ascending (CME valid_days).
            #
            # ("End of month" now lives in ONE place — this maturity rule.  The
            # former separate ``roll_schedule`` cadence was dropped; its
            # ``end_of_month`` value duplicated exactly this.)
            #
            # This wraps the SAME ``_resolve_arith`` call (so Issue #2's snap is
            # preserved); it just changes the CADENCE from per-date to per-month.
            # The held contract may expire mid-month — that is the WARN-don't-block
            # edge: Phases B/C then naturally produce ``no_chain_for_date`` for
            # the tail of the month (a gap), never a crash.  (EndOfMonth(offset_months)
            # targets the offset month's end, so the held contract is naturally a
            # month+ out — the gap case only arises if a different short-dated
            # target were used, which EndOfMonth is not.)
            held_exp: date | None = None
            held_note: str | None = None
            # (year, month) whose month-end triggered the last SUCCESSFUL resolve
            # — guards against re-resolving more than once per month.
            held_roll_month: tuple[int, int] | None = None
            # Memoise the month-end per (year, month): the sweep visits every
            # trade date but there are only ~N_months distinct month-ends, so
            # this avoids a redundant valid_days() scan on each held date.
            eom_cache: dict[tuple[int, int], date] = {}
            for idx, d in queryable:
                ym = (d.year, d.month)
                cur_eom = eom_cache.get(ym)
                if cur_eom is None:
                    cur_eom = last_trading_day_of_month(d)
                    eom_cache[ym] = cur_eom
                # Init guard keys on ``held_exp is None`` (per the diagnosis), NOT
                # ``held_roll_month is None``: if the FIRST roll-date resolve fails
                # (held_exp stays None while held_roll_month got set), we keep
                # re-trying on each subsequent date until a contract resolves —
                # rather than blanking the whole month. On the success path the two
                # guards are identical (held_exp is non-None after the first
                # resolve, so the cadence is governed by the d >= cur_eom term).
                is_roll_date = held_exp is None or (
                    d >= cur_eom and ym != held_roll_month
                )
                if is_roll_date:
                    held_exp, held_note = _resolve_arith(idx, d)
                    held_roll_month = (d.year, d.month)
                expirations[idx] = held_exp
                # The snap note is a property of the HELD contract, so it travels
                # to every held date (not only the roll date).  Skip dates whose
                # roll-date resolve failed — they already carry the failure code.
                if held_note is not None and error_codes[idx] is None:
                    snap_notes[idx] = held_note
        else:
            # Non-EndOfMonth arithmetic maturity (PlusNDays / FixedDate /
            # NextThirdFriday): stateless per-date resolution — the maturity rule
            # is re-resolved for every trade date.  Only EndOfMonth holds monthly.
            for idx, d in queryable:
                exp, note = _resolve_arith(idx, d)
                expirations[idx] = exp
                if note is not None:
                    snap_notes[idx] = note

    # ── Phase B: Group by expiration and bulk fetch ─────────────────

    # Group queryable dates by their resolved expiration.
    exp_groups: dict[date, list[tuple[int, date]]] = defaultdict(list)
    for idx, d in queryable:
        exp = expirations.get(idx)
        if exp is None:
            # Preserve a more specific code already set in Phase A (e.g.
            # ``maturity_resolution_failed``); default to ``no_chain_for_date``.
            if error_codes[idx] is None:
                error_codes[idx] = "no_chain_for_date"
        else:
            exp_groups[exp].append((idx, d))

    # ── Strike-window narrowing ────────────────────────────────────
    # Pre-compute a strike range from the selection criterion so that
    # Phase B bulk queries download only the contracts near the target
    # instead of the full chain (2000→20-50 docs for ATM on SPX).
    strike_min: float | None = None
    strike_max: float | None = None

    if isinstance(selection, ByStrike):
        strike_min = selection.strike
        strike_max = selection.strike
    elif (
        isinstance(selection, (ByMoneyness, ByDelta))
        and underlying_price_resolver is not None
    ):
        # Need the spot price to compute the strike window.  The
        # underlying_price_resolver requires a real contract (with
        # underlying_ref for option-on-futures routing).  Do one
        # lightweight probe query to get a contract from the first
        # date+expiration, then resolve the spot.
        if queryable and exp_groups:
            _repr_date = queryable[0][1]
            _first_exp = next(iter(exp_groups))
            _probe_reader = _CycleInjectingReader(chain_reader, cycle)
            try:
                _probe_rows = await _probe_reader.query_chain(
                    root=collection,
                    date=_repr_date,
                    type=option_type,
                    expiration_min=_first_exp,
                    expiration_max=_first_exp,
                )
            except Exception:  # noqa: BLE001
                _probe_rows = []
            _spot: float | None = None
            if _probe_rows:
                try:
                    _spot = await underlying_price_resolver(
                        _probe_rows[0][0], _repr_date
                    )
                except Exception:  # noqa: BLE001
                    _spot = None
            if _spot is not None and _spot > 0:
                if isinstance(selection, ByMoneyness):
                    # Margin (10%) on top of tolerance for spot drift.
                    _margin = 0.10
                    _lo = selection.target_K_over_S - selection.tolerance - _margin
                    _hi = selection.target_K_over_S + selection.tolerance + _margin
                    strike_min = _spot * max(_lo, 0.01)
                    strike_max = _spot * _hi
                else:
                    # ByDelta: wide moneyness proxy band.  Base ±30%
                    # around the first-date spot price, widened
                    # proportionally to the date range so that spot
                    # drift over long ranges doesn't push correct
                    # strikes outside the window.
                    _first_d = queryable[0][1]
                    _last_d = queryable[-1][1]
                    _span_days = (_last_d - _first_d).days
                    _extra = min(_span_days / 365.0 * 0.15, 0.30)
                    _margin = 0.30 + _extra
                    strike_min = _spot * (1 - _margin)
                    strike_max = _spot * (1 + _margin)

    # One bulk query per unique expiration, run concurrently but with a
    # semaphore so the concurrent dwh-connection fan-out stays within the pool
    # (multi-decade date ranges can produce 50-95+ expiration groups; each
    # ``query_chain_bulk`` acquires a pool connection).
    #
    # When ``concurrency_gate`` is supplied (production wires ONE process-wide
    # semaphore sized to the dwh pool — see ``tcg.core.api._options_concurrency``),
    # use it so the bound is SHARED across all concurrent resolves: the per-call
    # ``_DWH_RESOLVE_CONCURRENCY`` only bounds ONE resolve, so two concurrent
    # resolves (e.g. the Data-page composite + per-leg basket series) would each
    # take up to 3 slots → 6 > the 4-slot pool → PoolTimeout.  The shared gate
    # bounds the SUM.  ``None`` (e.g. unit tests / a lone resolve) falls back to a
    # fresh per-call semaphore = the prior behaviour.
    _bulk_sem = (
        concurrency_gate
        if concurrency_gate is not None
        else asyncio.Semaphore(_DWH_RESOLVE_CONCURRENCY)
    )
    chain_index: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}

    async def _fetch_exp(
        exp: date,
        group_dates: list[date],
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        async with _bulk_sem:
            result = await bulk_reader.query_chain_bulk(
                root=collection,
                dates=group_dates,
                type=option_type,
                expiration_min=exp,
                expiration_max=exp,
                strike_min=strike_min,
                strike_max=strike_max,
            )
        # Tick progress after each expiration fetch so the frontend
        # sees Phase B movement instead of a stuck 0%.  The progress
        # endpoint clamps fraction to [0, 1] so the extra ticks are safe.
        if progress_callback is not None:
            try:
                progress_callback()
            except Exception:  # pragma: no cover (defensive)
                pass
        return result

    fetch_tasks = [
        _fetch_exp(exp, [d for _idx, d in group]) for exp, group in exp_groups.items()
    ]
    fetch_results = await asyncio.gather(*fetch_tasks)
    for result in fetch_results:
        chain_index.update(result)

    # ── Phase C: Per-date selection + stream extraction ─────────────
    #
    # ByStrike and ByDelta are pure CPU (matching against pre-fetched
    # rows) — no asyncio tasks or semaphore needed.  Only ByMoneyness
    # requires I/O (underlying_price_resolver), so it keeps the async
    # gather + semaphore pattern.

    def _resolve_one_sync(idx: int, d: date) -> None:
        """CPU-only resolution for ByStrike and ByDelta."""
        try:
            rows = chain_index.get(d, [])
            if not rows:
                error_codes[idx] = "no_chain_for_date"
                return

            result: SelectionResult
            if isinstance(selection, ByStrike):
                result = match_by_strike(rows, selection.strike)
            elif isinstance(selection, ByDelta):
                deltas: list[float | None] = [r.delta_stored for _c, r in rows]
                result = match_by_delta(
                    rows=rows,
                    deltas=deltas,
                    target=selection.target_delta,
                    tolerance=selection.tolerance,
                    strict=selection.strict,
                    chain_size=len(rows),
                )
            else:
                raise TypeError(  # pragma: no cover
                    f"Unsupported SelectionCriterion for sync path: "
                    f"{type(selection).__name__}"
                )

            if result.error_code is not None:
                error_codes[idx] = result.error_code
                return
            if result.contract is None:  # pragma: no cover (defensive)
                error_codes[idx] = "no_chain_for_date"
                return

            row = _row_for_contract(rows, result.contract)
            if row is None:  # pragma: no cover (defensive)
                error_codes[idx] = "no_chain_for_date"
                return

            # Capture the selected contract for downstream roll-event
            # derivation.  Set BEFORE reading the stream so a missing
            # stream value still records the contract identity (the
            # selection itself succeeded).
            contracts[idx] = result.contract

            raw = getattr(row, attr_name, None)
            if raw is None:
                error_codes[idx] = _missing_code_for(stream)
                return
            values[idx] = float(raw)
        except Exception as exc:
            error_codes[idx] = "data_access_error"
            _log.debug("resolve_one_bulk date=%s failed: %s", d, exc)
        finally:
            if progress_callback is not None:
                try:
                    progress_callback()
                except Exception:  # pragma: no cover (defensive)
                    pass

    if isinstance(selection, ByMoneyness):
        # ByMoneyness: underlying price lookup requires I/O (each acquires a dwh
        # pool connection) — async path.  Share the SAME process-wide gate as
        # Phase B when supplied so the global bound also covers these lookups
        # (Phases B and C don't overlap, so reusing the one gate is correct);
        # fall back to the per-call cap otherwise.
        sem = (
            concurrency_gate
            if concurrency_gate is not None
            else asyncio.Semaphore(_MAX_INFLIGHT_PER_DATE)
        )

        async def _resolve_one_moneyness(idx: int, d: date) -> None:
            try:
                rows = chain_index.get(d, [])
                if not rows:
                    error_codes[idx] = "no_chain_for_date"
                    return

                async with sem:
                    if underlying_price_resolver is None:
                        error_codes[idx] = "missing_underlying_price"
                        return
                    first_contract = rows[0][0]
                    S = await underlying_price_resolver(first_contract, d)
                    if S is None or S <= 0:
                        error_codes[idx] = "missing_underlying_price"
                        return
                    result = match_by_moneyness(
                        rows=rows,
                        target_K_over_S=selection.target_K_over_S,
                        tolerance=selection.tolerance,
                        underlying_price=float(S),
                    )

                if result.error_code is not None:
                    error_codes[idx] = result.error_code
                    return
                if result.contract is None:  # pragma: no cover (defensive)
                    error_codes[idx] = "no_chain_for_date"
                    return

                row = _row_for_contract(rows, result.contract)
                if row is None:  # pragma: no cover (defensive)
                    error_codes[idx] = "no_chain_for_date"
                    return

                # Capture the selected contract for downstream roll-event
                # derivation.  See _resolve_one_sync for rationale.
                contracts[idx] = result.contract

                raw = getattr(row, attr_name, None)
                if raw is None:
                    error_codes[idx] = _missing_code_for(stream)
                    return
                values[idx] = float(raw)
            except Exception as exc:
                error_codes[idx] = "data_access_error"
                _log.debug("resolve_one_bulk date=%s failed: %s", d, exc)
            finally:
                if progress_callback is not None:
                    try:
                        progress_callback()
                    except Exception:  # pragma: no cover (defensive)
                        pass

        tasks: list[asyncio.Task[None]] = []
        for idx, d in queryable:
            if error_codes[idx] is not None:
                if progress_callback is not None:
                    try:
                        progress_callback()
                    except Exception:  # pragma: no cover (defensive)
                        pass
                continue
            tasks.append(asyncio.ensure_future(_resolve_one_moneyness(idx, d)))

        if tasks:
            await asyncio.gather(*tasks)
    else:
        # ByStrike / ByDelta: pure CPU — synchronous for-loop, no
        # asyncio tasks or semaphore overhead.
        for idx, d in queryable:
            if error_codes[idx] is not None:
                if progress_callback is not None:
                    try:
                        progress_callback()
                    except Exception:  # pragma: no cover (defensive)
                        pass
                continue
            _resolve_one_sync(idx, d)

    # Also tick progress for dates skipped in Phase A (past_last_trade_date).
    if progress_callback is not None:
        for i, d in enumerate(dates):
            if last_trade_date is not None and d > last_trade_date:
                try:
                    progress_callback()
                except Exception:  # pragma: no cover (defensive)
                    pass

    # Fold snap diagnostics in AFTER selection: a ``snapped_to:<iso>`` note is a
    # success-side annotation, so it is recorded only where the date resolved to
    # a real value (``error_codes[idx]`` still None).  Dates where the snapped
    # expiration itself failed selection keep their real failure code (e.g.
    # ``no_match_within_tolerance``) — the snap note must not mask a failure.
    for idx, note in snap_notes.items():
        if error_codes[idx] is None:
            error_codes[idx] = note

    # Option continuous series are RAW stitched mids: no back-adjustment.  A
    # back-adjusted option-premium series represents no tradable instrument
    # (theta decays premia toward 0, so a ratio factor diverges and an additive
    # offset swamps the premium), so ratio/difference are not offered for
    # options — only for continuous FUTURES (see ``tcg/data/_rolling``).

    return values, error_codes, contracts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def resolve_option_stream(
    *,
    dates: Sequence[date],
    collection: str,
    option_type: Literal["C", "P"],
    cycle: str | None,
    maturity: MaturitySpec,
    selection: SelectionCriterion,
    stream: StreamLabel,
    roll_offset: RollOffset = RollOffset(),
    chain_reader: _CycleAwareReader,
    maturity_resolver: MaturityResolver,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    last_trade_date: date | None = None,
    progress_callback: Callable[[], None] | None = None,
    bulk_chain_reader: _CycleAwareBulkReader | None = None,
    available_expirations: Sequence[date] | None = None,
    concurrency_gate: "asyncio.Semaphore | None" = None,
) -> tuple[NDArray[np.float64], list[str | None], list[OptionContractDoc | None]]:
    """Resolve a per-date 1-D ``float64`` stream off the selected option.

    Parameters
    ----------
    dates:
        Trade dates (chronological — the caller's responsibility) for
        which to materialise the stream.
    collection:
        OPT_* root collection — passed verbatim to the chain reader.
    option_type:
        ``"C"`` or ``"P"``.
    cycle:
        ``expiration_cycle`` filter applied to every chain query
        (``None`` = no filter).
    maturity:
        Dataclass ``MaturityRule`` (engine vocabulary — translate
        Pydantic at the wiring layer).
    selection:
        Dataclass ``SelectionCriterion``.
    stream:
        One of the labels in :data:`_STREAM_TO_ATTR`.  The option
        continuous series is always the RAW stitched stream — no
        back-adjustment is applied (ratio/difference are conceptually
        ill-posed for option premia and are offered only for continuous
        FUTURES; see ``tcg/data/_rolling``).
    roll_offset:
        ``RollOffset(value, unit)`` — the ROLL-EARLY axis.  The maturity rule
        is resolved as of ``date + offset`` (``unit`` = ``days`` or ``months``)
        for each date, so every roll happens that much sooner.  ``value == 0``
        (default) = no shift.  Distinct from the maturity's own ``offset_months``
        (the TARGET-month axis — which expiration to aim at).  Honored in the
        bulk path; the legacy per-date fallback resolves maturity inside the
        selector and cannot apply the shift, so a non-zero ``roll_offset``
        without a bulk reader raises ``ValueError``.  ("Roll at end of month" is
        NOT a roll_offset — it is the ``EndOfMonth`` maturity, which makes the
        resolver hold one contract per month.)
    chain_reader:
        Cycle-aware chain reader (any object satisfying
        :class:`_CycleAwareReader`).
    maturity_resolver:
        Engine ``MaturityResolver``.
    underlying_price_resolver:
        ``UnderlyingPriceResolver`` (``Callable[[contract, date],
        Awaitable[float | None]]``) — required when ``selection`` is
        ``ByMoneyness``; may be ``None`` otherwise.
    last_trade_date:
        Optional cutoff: dates strictly after this are not queried;
        the corresponding output entry is NaN with
        ``error_code='past_last_trade_date'``.  ``None`` disables the
        cutoff.
    bulk_chain_reader:
        Optional cycle-aware bulk chain reader.  When provided, the
        resolver uses the three-phase bulk pre-fetch path instead of
        per-date chain queries.  When ``None``, falls back to the
        existing per-date path (backwards compatible).
    available_expirations:
        Pre-fetched list of all expirations on this root (from
        ``list_expirations`` — a fast distinct index scan).  When
        provided, the NearestToTarget probe query (which materialises
        thousands of docs) is skipped entirely.  When ``None``, falls
        back to the expensive probe.
    concurrency_gate:
        Optional SHARED ``asyncio.Semaphore`` bounding the dwh-pool
        connection fan-out across ALL concurrent resolves.  Production
        wires ONE process-wide gate sized to the pool (built in
        ``tcg.core`` — the engine never imports the pool, preserving the
        ``engine``⊥``data`` import-linter contract).  Used for both the
        Phase-B bulk fetch and the Phase-C ByMoneyness underlying lookups.
        ``None`` (a lone resolve / unit test) uses a fresh per-call
        semaphore sized to ``_DWH_RESOLVE_CONCURRENCY`` — the prior
        behaviour, which bounds ONE resolve but not the SUM of concurrent
        resolves (the gap that caused the OPT_SP_500 PoolTimeout when the
        Data page fired the composite + per-leg basket series at once).
        Only consulted on the bulk path.

    Returns
    -------
    values:
        Shape ``(len(dates),)`` ``float64`` array.  NaN where the stream
        value is missing or the selection failed.
    error_codes:
        Parallel list, one entry per date.  Usually ``None`` when the value
        is real; otherwise a string diagnostic — propagated verbatim from
        ``SelectionResult.error_code`` when selection fails, or
        ``f"missing_{stream}"`` when selection succeeded but the row's
        stream field was ``None``.

        One entry is NON-None yet coexists with a REAL value:
        ``f"snapped_to:{iso}"`` is a SUCCESS-side annotation, set when a
        non-NearestToTarget maturity rule's arithmetic expiration was not
        listed and was snapped to the nearest listed expiration (``iso``).
        It is recorded only on dates that resolved to a real value (folded in
        after selection), so a ``snapped_to:`` entry never implies NaN.
        Consumers that treat "error_code present ⇒ failure/NaN" must therefore
        exclude the ``snapped_to:`` prefix (the value array is the source of
        truth for NaN-ness).
    contracts:
        Parallel list of ``OptionContractDoc | None``, one entry per
        date.  ``None`` when chain was missing or the selection match
        itself failed; the selected ``OptionContractDoc`` otherwise
        (including the case where the contract row had a missing
        stream value — selection itself succeeded).  Used by the API
        layer to derive roll events at ``contract_id`` transitions.
    """
    if stream not in _STREAM_TO_ATTR:
        raise ValueError(f"unknown stream label {stream!r}")

    # ── Bulk path: when a bulk reader is provided, use the pre-fetch
    # strategy for drastically fewer Mongo round-trips.
    if bulk_chain_reader is not None:
        return await _resolve_bulk(
            dates=dates,
            collection=collection,
            option_type=option_type,
            cycle=cycle,
            maturity=maturity,
            selection=selection,
            stream=stream,
            roll_offset=roll_offset,
            chain_reader=chain_reader,
            bulk_chain_reader=bulk_chain_reader,
            maturity_resolver=maturity_resolver,
            underlying_price_resolver=underlying_price_resolver,
            last_trade_date=last_trade_date,
            progress_callback=progress_callback,
            available_expirations=available_expirations,
            concurrency_gate=concurrency_gate,
        )

    # ── Legacy per-date path (fallback when no bulk reader wired) ──

    # The legacy per-date path resolves maturity inside the selector, so it
    # cannot honor an early roll (``roll_offset``) — that needs the bulk path's
    # pre-resolved expirations.  Rather than silently diverge (ignore the shift
    # and return a series that looks like the bulk result but is not), fail
    # loudly: production always wires the bulk reader, so this only fires on a
    # misconfigured caller.
    if roll_offset.value != 0:
        raise ValueError(
            "roll_offset requires the bulk chain reader; the legacy per-date "
            "path does not support it"
        )
    # Symmetric guard: the EndOfMonth monthly-hold sweep lives in the bulk
    # Phase A, so the legacy per-date path cannot honour it (it would silently
    # re-resolve EndOfMonth per-date, diverging from the held-monthly result).
    if isinstance(maturity, EndOfMonth):
        raise ValueError(
            "EndOfMonth maturity requires the bulk chain reader; the legacy "
            "per-date path does not support the monthly-hold roll"
        )

    # Wrap the reader so every selector-emitted query carries the cycle.
    cycle_reader = _CycleInjectingReader(chain_reader, cycle)
    selector = DefaultOptionsSelector(
        reader=cycle_reader,
        maturity_resolver=maturity_resolver,
        pricer=None,  # stream resolver only reads stored values; no compute.
        underlying_price_resolver=underlying_price_resolver,
    )

    n = len(dates)
    values: NDArray[np.float64] = np.full(n, np.nan, dtype=np.float64)
    error_codes: list[str | None] = [None] * n
    contracts: list[OptionContractDoc | None] = [None] * n
    attr_name = _STREAM_TO_ATTR[stream]

    # Bounded concurrency: every per-date task takes the semaphore for
    # the full chain-query block.  asyncio is single-threaded so direct
    # ``values[i] = ...`` / ``error_codes[i] = ...`` writes from
    # disjoint indices are safe without locks.
    sem = asyncio.Semaphore(_MAX_INFLIGHT_PER_DATE)

    async def _resolve_one(i: int, d: date) -> None:
        try:
            if last_trade_date is not None and d > last_trade_date:
                error_codes[i] = "past_last_trade_date"
                return
            async with sem:
                # selector.select swallows pricer-related branches (we
                # passed pricer=None and compute_missing_for_delta
                # defaults False), so any path through that requires
                # Module 2 is impossible here.
                result: SelectionResult = await selector.select(
                    root=collection,
                    date=d,
                    type=option_type,
                    criterion=selection,
                    maturity=maturity,
                    compute_missing_for_delta=False,
                )

                if result.error_code is not None:
                    error_codes[i] = result.error_code
                    return
                if result.contract is None:  # pragma: no cover (defensive)
                    error_codes[i] = "no_chain_for_date"
                    return

                # Selection succeeded — re-query the chain at the
                # resolved expiration to read the row's stream
                # attribute.  The CachedChainReader (when wired)
                # deduplicates this against the selector's narrow query.
                rows = await cycle_reader.query_chain(
                    root=collection,
                    date=d,
                    type=option_type,
                    expiration_min=result.contract.expiration,
                    expiration_max=result.contract.expiration,
                )

                row = _row_for_contract(rows, result.contract)
                if row is None:  # pragma: no cover (defensive)
                    error_codes[i] = "no_chain_for_date"
                    return

                # Capture the selected contract for downstream roll-event
                # derivation.  Set BEFORE reading the stream so a missing
                # stream value still records the contract identity.
                contracts[i] = result.contract

                raw = getattr(row, attr_name, None)
                if raw is None:
                    error_codes[i] = _missing_code_for(stream)
                    return
                values[i] = float(raw)
        except Exception as exc:
            # A single date failing (e.g. MongoDB timeout, network
            # blip) must not abort the entire stream — record NaN +
            # diagnostic and let the remaining dates proceed.
            error_codes[i] = "data_access_error"
            _log.debug("resolve_one date=%s failed: %s", d, exc)
        finally:
            # Notify progress regardless of which path we took — every
            # date counts as one tick. Wrapped in try/except so a
            # callback bug never destabilises the resolver itself.
            if progress_callback is not None:
                try:
                    progress_callback()
                except Exception:  # pragma: no cover (defensive)
                    pass

    await asyncio.gather(*(_resolve_one(i, d) for i, d in enumerate(dates)))

    # Option continuous series are RAW stitched values: no back-adjustment is
    # applied on either path (ratio/difference are offered only for continuous
    # FUTURES — see ``tcg/data/_rolling``).

    return values, error_codes, contracts


__all__ = ["resolve_option_stream", "StreamLabel"]
