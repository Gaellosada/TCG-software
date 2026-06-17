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
# Roll back-adjustment for the MID stream (futures convention)
# ---------------------------------------------------------------------------
#
# Mirrors ``tcg/data/_rolling/adjustment.py`` (continuous-futures
# back-adjustment) but operates on the per-date *selected-contract* mid
# series the resolver already produces, rather than on raw concatenated
# contracts.  The engine-data isolation contract forbids importing the
# data-layer helper, so the math is re-implemented here as an engine-local
# pure function (no I/O, no library deps beyond NumPy).
#
# Convention (identical to the futures oracle):
#   * Process roll seams BACKWARD (latest first) so factors/offsets cascade
#     — the most-recent segment stays unadjusted ("truth").
#   * A seam is an index ``i`` where the selected ``contract_id`` changes
#     (contracts[i-1] = rolled-OUT/old, contracts[i] = rolled-IN/new).
#   * The gap is taken from the same calendar day for BOTH contracts when the
#     pre-fetched chain has them, mirroring ``_shared_close_at_roll`` (no
#     residual artificial jump).  Preference order:
#       1. SEAM DAY i: old = old-contract mid on day i (looked up via
#          ``mid_at``), new = values[i] (= new-contract mid on day i).
#       2. PRIOR DAY i-1: old = values[i-1] (= old-contract mid on day i-1),
#          new = new-contract mid on day i-1 (looked up via ``mid_at``).
#       3. ADJACENT OBSERVED MARKS (always available; the per-date resolver
#          only fetches the *currently-selected* expiration's chain, so the
#          off-expiration contract is usually absent from ``mid_at`` and the
#          same-day candidates miss): old = values[i-1] (OLD's last selected
#          mark), new = values[i] (NEW's first selected mark).  This is the
#          observed roll P&L gap and, for ratio, makes the adjusted last-old
#          value EXACTLY equal the first-new value (seam continuous by
#          construction).  It mirrors ``_shared_close_at_roll``'s nearest-date
#          fallback (closes on different days) — the unavoidable analogue when
#          no shared chain day exists.
#   * ratio:     factor = new / old; multiply all earlier (pre-seam) mids.
#   * difference: offset = new − old; add to all earlier (pre-seam) mids.
#   * Zero/NaN guard (SYMMETRIC for both modes): if either reference mid is 0
#     or non-finite on BOTH candidate days, skip the seam — leave the gap,
#     warn ("unadjusted gap remains").  Never zero-out or NaN-poison history.
#   * NaNs in the series are preserved: a NaN earlier value stays NaN after a
#     multiply/add (missing stream values are not fabricated).

Adjustment = Literal["none", "ratio", "difference"]


def _seam_gap(
    *,
    seam_idx: int,
    old_cid: str,
    new_cid: str,
    values: NDArray[np.float64],
    mid_at: Callable[[str, int], float | None],
) -> tuple[float, float] | None:
    """Resolve the ``(old_mid, new_mid)`` gap for one roll seam.

    Returns the ``(old, new)`` pair on a single shared trading day when the
    chain has both contracts, else the adjacent observed marks, else ``None``
    (caller skips the seam and warns).  See the module convention block above
    for the full preference order.
    """
    prev = seam_idx - 1

    def _finite_pair(
        old: float | None, new: float | None
    ) -> tuple[float, float] | None:
        if old is None or new is None:
            return None
        if old == 0.0 or new == 0.0:
            return None
        if not np.isfinite(old) or not np.isfinite(new):
            return None
        return float(old), float(new)

    # Candidate 1 (same calendar day): seam date i.
    #   old = old-contract mid looked up on day i; new = values[i] (= NEW@i).
    pair = _finite_pair(mid_at(old_cid, seam_idx), float(values[seam_idx]))
    if pair is not None:
        return pair

    # Candidate 2 (same calendar day): prior day i-1.
    #   old = values[i-1] (= OLD@i-1); new = new-contract mid looked up on i-1.
    pair = _finite_pair(float(values[prev]), mid_at(new_cid, prev))
    if pair is not None:
        return pair

    # Candidate 3 (adjacent observed marks — different days): OLD's last
    # selected mark vs NEW's first selected mark.  Always tried last; this is
    # the only data available when the chain holds a single expiration per
    # date (the production bulk path).
    pair = _finite_pair(float(values[prev]), float(values[seam_idx]))
    if pair is not None:
        return pair

    return None


def _back_adjust_mid(
    *,
    values: NDArray[np.float64],
    contracts: Sequence[OptionContractDoc | None],
    mid_at: Callable[[str, int], float | None],
    mode: Adjustment,
) -> NDArray[np.float64]:
    """Back-adjust a per-date selected-contract mid series at roll seams.

    Pure function — no I/O.  ``mid_at(contract_id, date_idx)`` returns the mid
    of ANY contract on the given date index (backed by the resolver's
    pre-fetched chain), or ``None`` when that contract has no quoted mid on
    that date.  ``mode`` is ``"ratio"`` or ``"difference"`` (callers must not
    pass ``"none"`` — that path returns the raw series without calling this).

    Returns a NEW array; the input is not mutated.  The most-recent segment is
    left unchanged; earlier segments accumulate every subsequent seam's
    factor/offset (backward cascade).
    """
    n = len(values)
    if n == 0:
        return values.copy()

    # Identify seam indices (selected contract_id changes; both sides known).
    seams: list[int] = []
    for i in range(1, n):
        prev = contracts[i - 1]
        curr = contracts[i]
        if prev is None or curr is None:
            continue
        if prev.contract_id != curr.contract_id:
            seams.append(i)

    if not seams:
        return values.copy()

    adj = values.copy()

    # Process BACKWARD (latest seam first) so factors/offsets cascade onto all
    # earlier history exactly once per seam.
    for seam_idx in reversed(seams):
        old_cid = contracts[seam_idx - 1].contract_id  # type: ignore[union-attr]
        new_cid = contracts[seam_idx].contract_id  # type: ignore[union-attr]

        gap = _seam_gap(
            seam_idx=seam_idx,
            old_cid=old_cid,
            new_cid=new_cid,
            values=values,
            mid_at=mid_at,
        )
        if gap is None:
            _log.warning(
                "Mid %s roll skipped at index %d (%s -> %s): no finite, "
                "non-zero shared mid on the seam or prior day. Unadjusted "
                "gap remains.",
                mode,
                seam_idx,
                old_cid,
                new_cid,
            )
            continue

        old_mid, new_mid = gap
        # Apply to every date BEFORE the seam (the old segment and all earlier
        # ones).  NaN entries stay NaN under both * and +.
        pre = slice(0, seam_idx)
        if mode == "ratio":
            adj[pre] *= new_mid / old_mid
        else:  # "difference"
            adj[pre] += new_mid - old_mid

    return adj


def _apply_mid_adjustment(
    *,
    values: NDArray[np.float64],
    contracts: Sequence[OptionContractDoc | None],
    dates: Sequence[date],
    mid_by_cid_date: dict[tuple[str, date], float],
    stream: StreamLabel,
    adjustment: Adjustment,
) -> NDArray[np.float64]:
    """Apply MID back-adjustment iff ``stream == "mid"`` and ``adjustment`` is
    not ``"none"``; otherwise return ``values`` unchanged (raw).

    ``mid_by_cid_date`` maps ``(contract_id, date)`` → mid for every contract
    the resolver observed in any chain query — the source for the seam gap
    lookup.  The non-price streams (iv / greeks / volume / open_interest) and
    ``adjustment == "none"`` short-circuit to the raw series with no warning
    (an adjustment request on a non-price stream is silently ignored, per the
    locked design).
    """
    if stream != "mid" or adjustment == "none":
        return values

    def mid_at(contract_id: str, date_idx: int) -> float | None:
        return mid_by_cid_date.get((contract_id, dates[date_idx]))

    return _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=mid_at,
        mode=adjustment,
    )


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
    adjustment: Adjustment,
    roll_offset: int = 0,
    chain_reader: _CycleAwareReader,
    bulk_chain_reader: _CycleAwareBulkReader,
    maturity_resolver: MaturityResolver,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    last_trade_date: date | None,
    progress_callback: Callable[[], None] | None,
    available_expirations: Sequence[date] | None,
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
                    # roll_offset: resolve maturity as of (d + roll_offset) so
                    # every roll happens roll_offset calendar days earlier.
                    expirations[idx] = maturity_resolver.resolve_with_chain(
                        ref_date=d + timedelta(days=roll_offset),
                        rule=maturity,
                        available_expirations=available,
                    )
    else:
        for idx, d in queryable:
            expirations[idx] = maturity_resolver.resolve(
                ref_date=d + timedelta(days=roll_offset), rule=maturity
            )

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
    # semaphore to avoid exhausting the MongoDB connection pool on
    # multi-decade date ranges (e.g. 1990–2026 can produce 50+ groups).
    _BULK_FETCH_CONCURRENCY = 8
    _bulk_sem = asyncio.Semaphore(_BULK_FETCH_CONCURRENCY)
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
        # ByMoneyness: underlying price lookup requires I/O — async path.
        sem = asyncio.Semaphore(_MAX_INFLIGHT_PER_DATE)

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

    # MID back-adjustment (futures convention).  No-op unless stream == "mid"
    # and adjustment != "none".  ``_seam_gap`` reads contract mids from the
    # already-fetched ``chain_index`` (no extra I/O).  A shared-day gap is
    # available only for same-expiration strike re-selections (both contracts
    # sit in that date's chain); a maturity roll has one expiration per date,
    # so the rolled-out contract is absent and ``_seam_gap`` falls back to the
    # adjacent observed marks (see its candidate order).
    if stream == "mid" and adjustment != "none":
        mid_by_cid_date: dict[tuple[str, date], float] = {}
        for d, rows in chain_index.items():
            for c, r in rows:
                if r.mid is not None:
                    mid_by_cid_date[(c.contract_id, d)] = float(r.mid)
        values = _apply_mid_adjustment(
            values=values,
            contracts=contracts,
            dates=dates,
            mid_by_cid_date=mid_by_cid_date,
            stream=stream,
            adjustment=adjustment,
        )

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
    adjustment: Adjustment = "none",
    roll_offset: int = 0,
    chain_reader: _CycleAwareReader,
    maturity_resolver: MaturityResolver,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    last_trade_date: date | None = None,
    progress_callback: Callable[[], None] | None = None,
    bulk_chain_reader: _CycleAwareBulkReader | None = None,
    available_expirations: Sequence[date] | None = None,
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
        One of the labels in :data:`_STREAM_TO_ATTR`.
    adjustment:
        Roll back-adjustment for the rolled series, mirroring the
        continuous-futures convention (``"none"`` | ``"ratio"`` |
        ``"difference"``).  Applied ONLY when ``stream == "mid"``; for every
        other stream it is ignored and the raw series is returned (no error).
        ``"ratio"`` multiplies pre-seam mids by ``new/old`` at each roll;
        ``"difference"`` adds ``new-old``; processed backward so the most
        recent segment is unadjusted.  A zero/NaN reference mid at a seam
        leaves that gap unadjusted (warned).  Default ``"none"``.

        NOTE: the seam-gap lookup needs the rolled-out / rolled-in contract's
        mid on the boundary day, which is only present in the **bulk** path's
        pre-fetched chain.  In the legacy per-date fallback (no
        ``bulk_chain_reader``) the off-expiration contract is not re-queried,
        so unresolvable seams are left unadjusted.  Production always wires the
        bulk reader.
    roll_offset:
        Calendar days to roll EARLY: the maturity rule is resolved as of
        ``date + roll_offset`` for each date, so every roll happens
        ``roll_offset`` days sooner (mirrors the futures roll offset).
        ``0`` (default) = no shift.  Honored in the bulk path; the legacy
        per-date fallback resolves maturity inside the selector and does
        not apply the shift.
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
            adjustment=adjustment,
            roll_offset=roll_offset,
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
    contracts: list[OptionContractDoc | None] = [None] * n
    attr_name = _STREAM_TO_ATTR[stream]

    # (contract_id, date) → mid, accumulated for MID back-adjustment.  Only
    # populated for stream == "mid" with a real adjustment; captures every row
    # the per-date query returned (the selected expiration's chain).  asyncio
    # is single-threaded so distinct-key writes from disjoint dates are safe.
    mid_by_cid_date: dict[tuple[str, date], float] = {}
    _capture_mids = stream == "mid" and adjustment != "none"

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
                # Capture mids from the queried chain for MID back-adjustment
                # (best-effort; only the selected expiration's rows are here).
                if _capture_mids:
                    for c, r in rows:
                        if r.mid is not None:
                            mid_by_cid_date[(c.contract_id, d)] = float(r.mid)

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

    # MID back-adjustment (no-op unless stream == "mid" and adjustment !=
    # "none").  Legacy path: the off-expiration contract is not re-queried, so
    # seams the mid-map cannot resolve are left unadjusted (warned) rather than
    # corrupted.  See the ``adjustment`` parameter note.
    values = _apply_mid_adjustment(
        values=values,
        contracts=contracts,
        dates=dates,
        mid_by_cid_date=mid_by_cid_date,
        stream=stream,
        adjustment=adjustment,
    )

    return values, error_codes, contracts


__all__ = ["resolve_option_stream", "StreamLabel"]
