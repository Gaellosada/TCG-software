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

Per-date call count and concurrency (no batching available)
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
from datetime import date
from typing import Literal, Protocol, Sequence, runtime_checkable

import numpy as np
from numpy.typing import NDArray


# Bounded concurrency for the per-date resolver loop. Motor's default
# Mongo connection pool is 100 — keeping the inflight bound well below
# that prevents queueing-induced latency while still extracting most of
# the parallelism benefit. Empirically 32 is enough to saturate a remote
# Mongo for typical 252-day windows; raise only if profiling shows
# otherwise.
_MAX_INFLIGHT_PER_DATE = 32

# (Earlier iterations of this module exposed a per-date ``progress_callback``
# hook so a polling-based progress UI could tick once per resolved trade
# date.  That UI was removed in favour of a simpler FE-side elapsed-time
# display; the resolver no longer needs the hook.)

from tcg.engine.options.maturity.protocol import MaturityResolver
from tcg.engine.options.selection._ports import (
    ChainReaderPort,
    UnderlyingPriceResolver,
)
from tcg.engine.options.selection.selector import DefaultOptionsSelector
from tcg.types.options import (
    MaturitySpec,
    OptionContractDoc,
    OptionDailyRow,
    SelectionCriterion,
    SelectionResult,
)


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
        if last_trade_date is not None and d > last_trade_date:
            error_codes[i] = "past_last_trade_date"
            return
        async with sem:
            # selector.select swallows pricer-related branches (we
            # passed pricer=None and compute_missing_for_delta defaults
            # False), so any path through that requires Module 2 is
            # impossible here.
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

            # Selection succeeded — re-query the chain at the resolved
            # expiration to read the row's stream attribute.  The
            # CachedChainReader (when wired) deduplicates this against
            # the selector's narrow query.
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

    await asyncio.gather(*(_resolve_one(i, d) for i, d in enumerate(dates)))

    return values, error_codes


__all__ = ["resolve_option_stream", "StreamLabel"]
