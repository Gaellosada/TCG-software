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

For an N-day backtest this is N (or 2N for NearestToTarget) Mongo
round-trips.  The per-date tasks run **concurrently** under
``asyncio.gather`` with a bounded semaphore (see
:data:`_MAX_INFLIGHT_PER_DATE`) — wall-clock latency is therefore
roughly ``ceil(N / _MAX_INFLIGHT_PER_DATE) × Mongo_RTT``, not
``N × Mongo_RTT`` as a serial loop would give.  Total query count is
unchanged from the serial loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Awaitable, Callable, Literal, Protocol, Sequence, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from tcg.engine.options.maturity.protocol import MaturityResolver
from tcg.engine.options.selection._match import (
    match_by_delta,
    match_by_moneyness,
    match_by_strike,
)
from tcg.engine.options.selection._ports import (
    ChainReaderPort,
    UnderlyingPriceResolver,
)
from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    MaturitySpec,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    SelectionCriterion,
    SelectionResult,
)

_log = logging.getLogger(__name__)


# Bounded concurrency for the per-date resolver loop.  Each task holds
# a Motor connection for the full cursor iteration of its chain query;
# large collections (OPT_SP_500: 418K docs) produce long-lived cursors
# that starve the pool when too many run in parallel.  16 keeps
# wall-clock latency reasonable while leaving ample headroom in Motor's
# default 100-slot pool for underlying-price lookups and other activity.
_MAX_INFLIGHT_PER_DATE = 16


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
    chain_reader: _CycleAwareReader,
    bulk_chain_reader: _CycleAwareBulkReader,
    maturity_resolver: MaturityResolver,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    last_trade_date: date | None,
    progress_callback: Callable[[], None] | None,
    available_expirations: Sequence[date] | None,
) -> tuple[NDArray[np.float64], list[str | None]]:
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
    """
    attr_name = _STREAM_TO_ATTR[stream]
    n = len(dates)
    values: NDArray[np.float64] = np.full(n, np.nan, dtype=np.float64)
    error_codes: list[str | None] = [None] * n

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

    if isinstance(maturity, NearestToTarget):
        if queryable:
            first_date = queryable[0][1]
            last_date = queryable[-1][1]
            probe_days = max(maturity.target_dte_days * 3, 180)
            # Use last_date for the upper bound so expirations needed by
            # trade dates near the end of the range are included.
            far_future = last_date + timedelta(days=probe_days)

            if available_expirations is not None:
                # Fast path: caller pre-fetched all expirations via
                # list_expirations (distinct index scan — subsecond).
                # Filter to the probe window locally.
                available = [
                    e for e in available_expirations if first_date <= e <= far_future
                ]
            else:
                # Fallback: expensive probe query materialises the full
                # chain to extract expiration dates.
                cycle_reader = _CycleInjectingReader(chain_reader, cycle)
                probe_rows = await cycle_reader.query_chain(
                    root=collection,
                    date=first_date,
                    type=option_type,
                    expiration_min=first_date,
                    expiration_max=far_future,
                )
                available = sorted({c.expiration for c, _r in probe_rows})

            for idx, d in queryable:
                if not available:
                    expirations[idx] = None
                else:
                    expirations[idx] = maturity_resolver.resolve_with_chain(
                        ref_date=d,
                        rule=maturity,
                        available_expirations=available,
                    )
    else:
        for idx, d in queryable:
            expirations[idx] = maturity_resolver.resolve(ref_date=d, rule=maturity)

    # ── Phase B: Group by expiration and bulk fetch ─────────────────

    # Group queryable dates by their resolved expiration.
    exp_groups: dict[date, list[tuple[int, date]]] = defaultdict(list)
    for idx, d in queryable:
        exp = expirations.get(idx)
        if exp is None:
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
                    # ByDelta: wide moneyness proxy band (±30%).
                    # Delta ~0.5 ≈ ATM; conservative but still cuts
                    # ~80-90% of contracts on fat roots.
                    strike_min = _spot * 0.70
                    strike_max = _spot * 1.30

    # One bulk query per unique expiration, run concurrently.
    chain_index: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}

    async def _fetch_exp(
        exp: date,
        group_dates: list[date],
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
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

    sem = asyncio.Semaphore(_MAX_INFLIGHT_PER_DATE)

    async def _resolve_one_bulk(idx: int, d: date) -> None:
        try:
            rows = chain_index.get(d, [])
            if not rows:
                error_codes[idx] = "no_chain_for_date"
                return

            # Apply selection criterion.
            result: SelectionResult
            if isinstance(selection, ByStrike):
                result = match_by_strike(rows, selection.strike)
            elif isinstance(selection, ByMoneyness):
                # Underlying price lookup still requires I/O — use semaphore.
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
            elif isinstance(selection, ByDelta):
                # Stored-only path (compute_missing_for_delta is always
                # False in the stream resolver — no pricer injected).
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
                    f"Unsupported SelectionCriterion: {type(selection).__name__}"
                )

            if result.error_code is not None:
                error_codes[idx] = result.error_code
                return
            if result.contract is None:  # pragma: no cover (defensive)
                error_codes[idx] = "no_chain_for_date"
                return

            # Find the row for the selected contract and extract stream.
            row = _row_for_contract(rows, result.contract)
            if row is None:  # pragma: no cover (defensive)
                error_codes[idx] = "no_chain_for_date"
                return

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

    # Gather tasks only for dates that survived Phase A+B (have rows
    # in the chain_index or need underlying-price I/O).  Dates already
    # assigned an error_code in Phase A/B still need their progress tick.
    tasks: list[asyncio.Task[None]] = []
    for idx, d in queryable:
        if error_codes[idx] is not None:
            # Already resolved (no_chain_for_date) — just tick progress.
            if progress_callback is not None:
                try:
                    progress_callback()
                except Exception:  # pragma: no cover (defensive)
                    pass
            continue
        tasks.append(asyncio.ensure_future(_resolve_one_bulk(idx, d)))

    if tasks:
        await asyncio.gather(*tasks)

    # Also tick progress for dates skipped in Phase A (past_last_trade_date).
    if progress_callback is not None:
        for i, d in enumerate(dates):
            if last_trade_date is not None and d > last_trade_date:
                try:
                    progress_callback()
                except Exception:  # pragma: no cover (defensive)
                    pass

    return values, error_codes


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
    chain_reader: _CycleAwareReader,
    maturity_resolver: MaturityResolver,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    last_trade_date: date | None = None,
    progress_callback: Callable[[], None] | None = None,
    bulk_chain_reader: _CycleAwareBulkReader | None = None,
    available_expirations: Sequence[date] | None = None,
) -> tuple[NDArray[np.float64], list[str | None]]:
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
        One of the labels in :data:`_STREAM_TO_ATTR`.
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

    Returns
    -------
    values:
        Shape ``(len(dates),)`` ``float64`` array.  NaN where the stream
        value is missing or the selection failed.
    error_codes:
        Parallel list, one entry per date.  ``None`` when the value is
        real; otherwise a string diagnostic — propagated verbatim from
        ``SelectionResult.error_code`` when selection fails, or
        ``f"missing_{stream}"`` when selection succeeded but the row's
        stream field was ``None``.
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
            chain_reader=chain_reader,
            bulk_chain_reader=bulk_chain_reader,
            maturity_resolver=maturity_resolver,
            underlying_price_resolver=underlying_price_resolver,
            last_trade_date=last_trade_date,
            progress_callback=progress_callback,
            available_expirations=available_expirations,
        )

    # ── Legacy per-date path (fallback when no bulk reader wired) ──

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

    return values, error_codes


__all__ = ["resolve_option_stream", "StreamLabel"]
