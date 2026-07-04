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
from typing import Callable, Literal, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from tcg.engine.options.maturity.protocol import MaturityResolver
from tcg.engine.options.maturity.resolver import _add_months, last_trading_day_of_month
from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.pricing.protocol import PricingKernel
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
#
# ``bs_mid`` is the one COMPUTED stream (not a row field): the contract's
# Black-76 theoretical price from its stored IV + the underlying FUTURE price
# (the Java sim's price basis), intrinsic at expiry.  See ``_price_bs_mid``.
StreamLabel = Literal[
    "mid",
    "bs_mid",
    "iv",
    "delta",
    "gamma",
    "vega",
    "theta",
    "open_interest",
    "volume",
]


# The COMPUTED stream label — priced from IV + underlying, not read off the row.
_BS_MID = "bs_mid"

# Java default: r = 0 (Phase 1 mandate; the Black-76 forward already embeds
# carry, so the discount factor exp(-rT) is identically 1).  ACT/365 day-count
# for DTE → year fraction, matching the kernel's convention.
_BS_RATE: float = 0.0
_DAYS_PER_YEAR: float = 365.0


# Map stream label → ``OptionDailyRow`` attribute.  Centralised so a
# missing-field bug surfaces as a KeyError at construction time, not a
# silent NaN at runtime.  ``bs_mid`` is deliberately ABSENT — it is computed
# (see ``_price_bs_mid`` / ``_extract_stream_value``), not read off a field.
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


def _price_bs_mid(
    *,
    iv: float | None,
    future_price: float | None,
    strike: float,
    option_type: Literal["C", "P"],
    dte_days: int,
    kernel: "PricingKernel",
) -> tuple[float | None, str | None]:
    """Black-76 theoretical premium from surface IV + the underlying FUTURE.

    Reproduces the Java sim's price basis (recon §4): the held option is priced
    as a Black-Scholes/Black-76 theoretical value from the day's stored surface
    IV on the E-mini FUTURE (ACT/365, r=0), intrinsic at expiry — NOT the raw
    bid-ask mid.  For deep-OTM 10Δ puts with wide quotes the two differ a lot.

    Returns ``(price, error_code)`` — exactly one non-None:
      * ``future_price`` missing / ≤ 0 / NaN → ``(None, "missing_underlying_price")``
        (options are on the future, whose price comes from the underlying
        resolver — OPT_SP_500 greeks store NULL underlying_price, so a row field
        is not usable);
      * malformed strike (``K ≤ 0`` or non-finite) → ``(None, "missing_bs_price")``
        — degrade cleanly instead of letting the kernel raise (real strikes > 0);
      * at/after expiry (``dte_days <= 0``) → INTRINSIC value (``max(K−F,0)`` put
        / ``max(F−K,0)`` call), matching the Java expiry rule — needs only the
        future, NOT the IV (a fabricated-IV price at expiry would be wrong);
      * before expiry with ``iv`` missing / ≤ 0 → ``(None, "missing_bs_iv")``
        (no fabrication — a real diagnostic);
      * otherwise the Black-76 price at ``T = dte_days/365``.
    """
    # A missing / non-positive / NaN future is an unusable underlying price.
    # ``nan <= 0.0`` is False, so a NaN future must be caught explicitly here —
    # otherwise it falls through and is mislabelled ``missing_bs_price`` at the
    # non-finite-output guard below (it is a missing UNDERLYING, not a bad price).
    if future_price is None or not np.isfinite(future_price) or future_price <= 0.0:
        return None, "missing_underlying_price"
    F = float(future_price)
    K = float(strike)
    # A malformed strike (K ≤ 0 or non-finite) would make the Black-76 kernel
    # raise (ZeroDivisionError / ValueError / domain error) — degrade cleanly to a
    # missing price so the hold path (which does NOT wrap this, unlike Phase C)
    # never 500s on bad dwh data.  Real strikes are always positive.
    if not np.isfinite(K) or K <= 0.0:
        return None, "missing_bs_price"
    # Expiry (or past): intrinsic value — independent of IV (Java expiry rule).
    if dte_days <= 0:
        intrinsic = max(K - F, 0.0) if option_type == "P" else max(F - K, 0.0)
        return intrinsic, None
    if iv is None or iv <= 0.0:
        return None, "missing_bs_iv"
    T = dte_days / _DAYS_PER_YEAR
    sigma = float(iv)
    if option_type == "P":
        price = kernel.price_put(F, K, T, _BS_RATE, sigma)
    else:
        price = kernel.price_call(F, K, T, _BS_RATE, sigma)
    # A non-finite kernel output (pathological inputs) is a loud diagnostic, not
    # a silent NaN that would poison the P&L series.
    if not np.isfinite(price):
        return None, "missing_bs_price"
    return float(price), None


async def _extract_stream_value(
    *,
    stream: StreamLabel,
    contract: OptionContractDoc,
    row: OptionDailyRow,
    d: date,
    attr_name: str,
    underlying_price_resolver: UnderlyingPriceResolver | None,
    kernel: "PricingKernel | None",
) -> tuple[float | None, str | None]:
    """Extract one stream value for a selected ``(contract, row)`` on date ``d``.

    Row-attribute streams (``mid``/``iv``/greeks/…) read ``getattr(row, attr)``
    synchronously.  ``bs_mid`` is COMPUTED: it awaits the underlying FUTURE price
    and prices via the Black-76 ``kernel`` (see :func:`_price_bs_mid`).  Returns
    ``(value, error_code)`` — exactly one non-None.  Shared by the Phase-C
    per-date path and the select-and-hold path so the two never diverge.
    """
    if stream == _BS_MID:
        if kernel is None:  # pragma: no cover (resolver always builds one)
            return None, "missing_bs_price"
        F: float | None = None
        if underlying_price_resolver is not None:
            F = await underlying_price_resolver(contract, d)
        dte_days = (contract.expiration - d).days
        return _price_bs_mid(
            iv=row.iv_stored,
            future_price=F,
            strike=contract.strike,
            option_type=contract.type,
            dte_days=dte_days,
            kernel=kernel,
        )
    raw = getattr(row, attr_name, None)
    if raw is None:
        return None, _missing_code_for(stream)
    return float(raw), None


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


# ---------------------------------------------------------------------------
# Coverage-aware expiration selection (opt-in, default OFF)
# ---------------------------------------------------------------------------
#
# WHY.  ``NearestToTarget`` picks the single nearest-DTE expiration from the
# (cycle-filtered) listed set, with NO awareness of whether that expiration
# actually carries strikes near the requested delta / moneyness.  On options
# with sparse or era-varying coverage (e.g. the E-mini ``OPT_SP_500`` M-cycle in
# later years, where a target month can have ZERO delta-bearing puts), the
# nearest-DTE expiration can be a thin listing whose only greeked strikes are
# deep-OTM garbage.  ``ByDelta(-0.10)`` then returns that garbage (moneyness
# ~0.17) instead of the true 10-delta put (~0.88) that exists at a neighbouring
# expiration — corr against a ground-truth sim collapses/inverts in those eras.
#
# Coverage-aware selection resolves the expiration to the nearest-DTE candidate
# that has an IN-TOLERANCE delta/moneyness match, retrying the next-nearest
# within a bounded DTE window; if none is in tolerance it falls back to the
# nearest-DTE best-effort (== the current behaviour) so it never regresses to
# all-NaN.  Gated behind ``coverage_aware`` — OFF by default (byte-identical /
# golden-preserving).

#: Max number of nearest-DTE candidate expirations considered per date in
#: coverage-aware mode.  Bounds the extra Phase-B fetch fan-out (each unique
#: candidate expiration = one bulk query).  Conservative default; the right value
#: depends on the root's expiry spacing (see the live-data request).
_COVERAGE_MAX_CANDIDATES: int = 4

#: Only consider candidate expirations whose DTE is within this many days of the
#: nearest-DTE candidate's DTE.  Keeps the retry local to the target maturity
#: (a ~2-month target should not silently jump to a 6-month expiration).
_COVERAGE_DTE_WINDOW_DAYS: int = 45


def _coverage_candidates(
    ref_date: date,
    target_dte_days: int,
    available: Sequence[date],
) -> list[date]:
    """Ordered candidate expirations for coverage-aware ``NearestToTarget``.

    Sorted by ``(|dte - target|, dte)`` — the SAME key ``resolve_with_chain``
    ranks by, so ``candidates[0]`` is exactly the expiration the coverage-BLIND
    path would have picked.  Truncated to :data:`_COVERAGE_MAX_CANDIDATES` and to
    a ``±`` :data:`_COVERAGE_DTE_WINDOW_DAYS` window around the nearest
    candidate's DTE, so the coverage retry stays near the requested maturity and
    the Phase-B fan-out stays bounded.

    Returns ``[]`` when *available* is empty.
    """
    if not available:
        return []
    target_date = ref_date + timedelta(days=target_dte_days)

    def _key(exp: date) -> tuple[int, int]:
        dte = (exp - ref_date).days
        return (abs((exp - target_date).days), dte)

    ordered = sorted(available, key=_key)
    nearest_dte = (ordered[0] - ref_date).days
    windowed = [
        e
        for e in ordered
        if abs((e - ref_date).days - nearest_dte) <= _COVERAGE_DTE_WINDOW_DAYS
    ]
    return windowed[:_COVERAGE_MAX_CANDIDATES]


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
        expiration_cycle: str | Sequence[str] | None = None,
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
        expiration_cycle: str | Sequence[str] | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]: ...


class _CycleInjectingReader:
    """Wrap a chain reader to always pass a fixed ``expiration_cycle``.

    The selector emits ``query_chain`` calls without an
    ``expiration_cycle`` argument; this wrapper injects the caller's
    cycle so the resolver honours guardrail 11 (no silent cycle mixing).
    ``cycle=None`` is a pass-through — equivalent to no wrapping at all
    — but we still wrap to keep the call site uniform.
    """

    def __init__(
        self, inner: _CycleAwareReader, cycle: str | Sequence[str] | None
    ) -> None:
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

    def __init__(
        self, inner: _CycleAwareBulkReader, cycle: str | Sequence[str] | None
    ) -> None:
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
# Select-and-hold (``hold_between_rolls``) helpers
# ---------------------------------------------------------------------------
#
# See ``resolve_option_stream``'s ``hold_between_rolls`` docstring for the WHY.
# The mode emits, per date, the HELD-CONTRACT PREMIUM (mid) LEVEL of the contract
# that OWNS the step ending on that date (the OLD contract on the roll day), PLUS
# roll info (``is_roll`` segment-start markers + each segment's roll-day OPEN
# premium).  It does NOT stitch/ratio-adjust the option series (a stitched OPTION
# level would court Gael's hard "no ratio-adjustment for options" constraint):
# ``signal_exec`` runs the FIXED-CONTRACT DOLLAR-P&L recurrence over these arrays
# (size a held quantity once per roll off the compounding NAV and the roll
# premium, book ``qty·Δpremium`` daily, realise+resize at the next roll), which is
# oracle-exact against the ground-truth Java close+reopen sim.  One helper:
#   * ``_hold_segments`` — split the queryable dates into contiguous HELD-contract
#     runs (a new run begins where the resolved *expiration* changes — the same
#     maturity-roll discriminator ``derive_rolls`` uses).


def _hold_segments(
    queryable: "list[tuple[int, date]]",
    expirations: "dict[int, date | None]",
) -> "list[list[tuple[int, date]]]":
    """Split ``queryable`` into contiguous runs sharing a resolved expiration.

    ``queryable`` is the chronological ``[(idx, date), ...]`` of dates that got a
    resolved expiration; ``expirations[idx]`` is that date's target expiration.  A
    new segment starts whenever the expiration differs from the previous
    queryable date's (a maturity ROLL — the same rule ``derive_rolls`` applies by
    comparing ``.expiration``).  Dates whose expiration is ``None`` (resolution
    failed / no chain) are DROPPED from segmentation — they keep whatever
    error_code Phase A/B set and contribute NaN; a ``None`` gap does not by itself
    force a new segment (the held contract continues across an isolated missing
    day), matching the WARN-don't-block philosophy of the rest of the resolver.

    Returns a list of segments, each a non-empty ``[(idx, date), ...]`` list.
    """
    segments: list[list[tuple[int, date]]] = []
    prev_exp: date | None = None
    for idx, d in queryable:
        exp = expirations.get(idx)
        if exp is None:
            # No resolved expiration for this date — leave it out of every
            # segment (its NaN + code already stand); do not break the run.
            continue
        if not segments or exp != prev_exp:
            segments.append([(idx, d)])
        else:
            segments[-1].append((idx, d))
        prev_exp = exp
    return segments


async def _resolve_hold(
    *,
    dates: Sequence[date],
    queryable: "list[tuple[int, date]]",
    expirations: "dict[int, date | None]",
    chain_index: "dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]",
    attr_name: str,
    stream: StreamLabel,
    values: NDArray[np.float64],
    error_codes: list[str | None],
    contracts: list[OptionContractDoc | None],
    snap_notes: "dict[int, str]",
    select_contract_on_date: "Callable[[date, list[tuple[OptionContractDoc, OptionDailyRow]]], object]",
    progress_callback: Callable[[], None] | None,
    underlying_price_resolver: UnderlyingPriceResolver | None = None,
    kernel: "PricingKernel | None" = None,
    roll_info_out: "dict[str, NDArray[np.float64]] | None" = None,
) -> tuple[NDArray[np.float64], list[str | None], list[OptionContractDoc | None]]:
    """Select-and-hold resolution over the pre-fetched ``chain_index``.

    Runs AFTER Phase A (per-date ``expirations``) and Phase B (``chain_index``)
    on the SAME data the per-date Phase C would use.  Instead of a daily-
    reselected mid LEVEL it emits, PER DATE, the HELD-CONTRACT PREMIUM (mid) LEVEL
    of the contract that OWNS the step ENDING on that date (the OLD contract on the
    roll day), and — via ``roll_info_out`` — the roll structure ``signal_exec``
    needs to run the FIXED-CONTRACT DOLLAR-P&L recurrence (no ``Δmid/mid`` on a
    stitched level, so NO option ratio-adjust):

    1. **Segment** the queryable dates into contiguous maturity runs
       (:func:`_hold_segments` — a new run at each ``expiration`` change, the
       ``derive_rolls`` discriminator).
    2. **Select once per segment**: pick the contract on the segment's FIRST
       date via ``select_contract_on_date`` and FREEZE it for the whole run (no
       daily strike churn).  Record it in ``contracts`` for every date of the run
       (the roll-event array reads maturity transitions off this).
    3. **Own each date's VALUE** by exactly one contract's mid LEVEL:
         * a date INTERIOR to a segment → the segment's held contract's mid;
         * a ROLL date (a segment's first date, when the previous output index
           belongs to the OLD segment) → the OLD (previous) contract's mid ON the
           roll day.  So the step ENDING on the roll day (``values[t]-base`` in
           signal_exec) is the OLD contract's OWN move into the roll — the
           ground-truth Java "realise the OLD" side of a close+reopen.
    4. **Roll info** (``roll_info_out``): ``is_roll`` marks every segment's first
       date (incl. the initial open at index 0); ``roll_premium`` at those dates
       is the NEW (this-segment) contract's roll-day OPEN mid — the base against
       which the NEW segment's daily P&L and its held-quantity sizing are computed.
       This is the ONLY place the NEW open premium is surfaced (``values`` on the
       roll date carries the OLD mid), so the seam is exact (realise OLD @ its
       roll-day mid, open NEW @ its roll-day mid), never a raw old→new level gap.

    Writes the held mid LEVEL into ``values`` (``values[t]`` == the owning
    contract's mid on ``t``; NaN where that contract has no quote), preserving
    Phase-A/B error codes for dates not covered by a segment.  When
    ``roll_info_out`` is a dict it is populated with ``{"is_roll", "roll_premium"}``
    (both length ``T`` ``float64``/bool arrays).  Returns the
    ``(values, error_codes, contracts)`` triple (roll info goes through the
    out-dict so the 3-tuple return stays stable for every non-hold caller).
    NOTE: in hold mode ``values`` is the held-contract mid LEVEL — an honest
    per-leg display of the premium actually held; the signals P&L path is the only
    consumer that pairs it with ``roll_info_out``.
    """
    n = len(dates)

    segments = _hold_segments(queryable, expirations)

    # Per-output-index: the mid LEVEL of the contract owning that date's value
    # (OLD contract on a roll date, held contract otherwise); plus the roll
    # structure the dollar-P&L recurrence needs.
    held_value: NDArray[np.float64] = np.full(n, np.nan, dtype=np.float64)
    is_roll: NDArray[np.bool_] = np.zeros(n, dtype=np.bool_)
    roll_premium: NDArray[np.float64] = np.full(n, np.nan, dtype=np.float64)

    async def _mid_of(contract: OptionContractDoc | None, d: date) -> float:
        """Read ``contract``'s ``stream`` value on date ``d`` from the chain.

        Returns ``NaN`` when the contract is absent from ``d``'s chain or the
        stream value is unavailable (a NaN value makes the adjacent P&L steps
        contribute 0 in signal_exec, mirroring the price path's ``valid`` mask).
        For ``bs_mid`` the value is COMPUTED (Black-76 from IV + the underlying
        future), so this is async — the future price is fetched per date."""
        if contract is None:
            return np.nan
        rows = chain_index.get(d, [])
        row = _row_for_contract(rows, contract) if rows else None
        if row is None:
            return np.nan
        value, _code = await _extract_stream_value(
            stream=stream,
            contract=contract,
            row=row,
            d=d,
            attr_name=attr_name,
            underlying_price_resolver=underlying_price_resolver,
            kernel=kernel,
        )
        return np.nan if value is None else float(value)

    prev_seg_contract: OptionContractDoc | None = None
    prev_seg_last_idx: int | None = None

    for seg_num, seg in enumerate(segments):
        # Select the held contract on the segment's FIRST date, then freeze it.
        first_idx, first_date = seg[0]
        # Restrict selection to THIS segment's resolved expiration: the roll day's
        # merged chain carries BOTH the OLD and NEW expirations (so the OLD's
        # roll-day mid is available), but the NEW segment must select a NEW-
        # expiration contract — never accidentally re-pick an OLD one on a delta
        # tie.  (Non-roll first dates carry only their own expiration, so this
        # filter is a no-op there.)
        seg_exp = expirations.get(first_idx)
        first_rows = [
            (c, r)
            for (c, r) in chain_index.get(first_date, [])
            if seg_exp is None or c.expiration == seg_exp
        ]
        held, sel_err = await select_contract_on_date(  # type: ignore[misc]
            first_date, first_rows
        )
        if held is None:
            # Selection failed at the roll — the whole segment cannot be priced.
            # Mark each of its dates (NaN value + diagnostic); the NEXT segment
            # then treats its first date as a fresh open (no OLD owner), since this
            # segment yielded no contract to realise.
            for idx, _d in seg:
                if error_codes[idx] is None:
                    error_codes[idx] = sel_err or "no_chain_for_date"
            prev_seg_contract = None
            prev_seg_last_idx = None
            continue

        # This segment's roll-day OPEN premium = the NEW held contract's own mid on
        # the segment's first date.  It is the base for the NEW segment's P&L and
        # for sizing the held quantity (surfaced via roll_info_out — values[first]
        # may carry the OLD mid on a true roll date).
        seg_open_premium = await _mid_of(held, first_date)

        for idx, d in seg:
            # Record the held contract on every date of the run.
            contracts[idx] = held

        for j, (idx, d) in enumerate(seg):
            held_mid_today = await _mid_of(held, d)
            is_true_roll = (
                seg_num > 0
                and prev_seg_contract is not None
                and prev_seg_last_idx == idx - 1
            )
            if j == 0 and is_true_roll:
                # Roll date: this date's VALUE is the OLD contract's mid ON the
                # roll day, so the step ENDING here is the OLD's own move into the
                # roll (realise the OLD).  The NEW open premium lives in
                # roll_premium (below).
                old_mid_today = await _mid_of(prev_seg_contract, d)
                held_value[idx] = old_mid_today
                is_roll[idx] = True
                roll_premium[idx] = seg_open_premium
                # Diagnostic keys on the OLD mid (that is this date's value); the
                # NEW open premium is validated when the NEXT step consumes it.
                if not np.isfinite(old_mid_today) and error_codes[idx] is None:
                    rows = chain_index.get(d, [])
                    error_codes[idx] = (
                        "no_chain_for_date" if not rows else _missing_code_for(stream)
                    )
            else:
                # Interior date (or the very first open, seg_num==0 j==0): the
                # value is THIS segment's held contract's mid.
                held_value[idx] = held_mid_today
                if j == 0:
                    # Segment open that is NOT a true roll (first segment overall,
                    # or a gap/failed prior segment): still a sizing point.
                    is_roll[idx] = True
                    roll_premium[idx] = seg_open_premium
                # Per-date diagnostic: the held contract not quoting today is a
                # missing-stream day (the price path's equivalent of a NaN value).
                if not np.isfinite(held_mid_today) and error_codes[idx] is None:
                    rows = chain_index.get(d, [])
                    error_codes[idx] = (
                        "no_chain_for_date" if not rows else _missing_code_for(stream)
                    )

        prev_seg_contract = held
        prev_seg_last_idx = seg[-1][0]

    for i in range(n):
        values[i] = held_value[i]

    if roll_info_out is not None:
        roll_info_out["is_roll"] = is_roll.astype(np.float64)
        roll_info_out["roll_premium"] = roll_premium

    # Fold snap diagnostics in AFTER extraction (success-side note; only where the
    # date resolved a held contract with a quote) — mirrors Phase C's handling.
    for idx, note in snap_notes.items():
        if error_codes[idx] is None and contracts[idx] is not None:
            error_codes[idx] = note

    # Progress: tick once per date (the HOLD path did no per-date async gather,
    # so emit the ticks Phase C would have).
    if progress_callback is not None:
        for _ in range(n):
            try:
                progress_callback()
            except Exception:  # pragma: no cover (defensive)
                pass

    return values, error_codes, contracts


# ---------------------------------------------------------------------------
# Bulk pre-fetch path (Phase A → B → C)
# ---------------------------------------------------------------------------


async def _resolve_bulk(
    *,
    dates: Sequence[date],
    collection: str,
    option_type: Literal["C", "P"],
    cycle: str | Sequence[str] | None,
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
    available_expirations_by_date: Mapping[date, Sequence[date]] | None = None,
    concurrency_gate: "asyncio.Semaphore | None" = None,
    hold_between_rolls: bool = False,
    hold_roll_info_out: "dict[str, NDArray[np.float64]] | None" = None,
    coverage_aware: bool = False,
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
    # ``bs_mid`` is COMPUTED, not a row attribute — it has no ``_STREAM_TO_ATTR``
    # entry (``attr_name`` is unused for it; ``_extract_stream_value`` prices it
    # from IV + the underlying future).  All other labels map to a row field.
    attr_name = "" if stream == _BS_MID else _STREAM_TO_ATTR[stream]
    # Build the Black-76 kernel once per resolve for the bs_mid computed stream
    # (stateless, pure — engine-local, so no wiring/param threading needed).
    kernel: PricingKernel | None = BS76Kernel() if stream == _BS_MID else None
    n = len(dates)
    values: NDArray[np.float64] = np.full(n, np.nan, dtype=np.float64)
    error_codes: list[str | None] = [None] * n
    contracts: list[OptionContractDoc | None] = [None] * n

    # Coverage-aware selection is a Phase-C re-pick; hold mode bypasses Phase C
    # (it segments + freezes ONE contract per roll via ``_resolve_hold``).  The
    # two are not composed in this pass — fail loudly rather than silently
    # ignoring coverage in hold mode.  (A future pass can teach ``_resolve_hold``
    # to select its per-segment contract coverage-aware.)
    if coverage_aware and hold_between_rolls:
        raise ValueError(
            "coverage_aware is not supported together with hold_between_rolls yet"
        )

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

    # Coverage-aware mode only: per-date ORDERED candidate expirations
    # (nearest-DTE first).  ``candidates[idx][0]`` is always the coverage-blind
    # pick, so ``expirations[idx]`` (set below) still equals the legacy choice —
    # the extra candidates are only consulted by the coverage-aware Phase-C
    # selection, which re-picks the nearest-DTE candidate that is actually
    # covered.  Empty dict when the mode is off (the common default).
    candidates: dict[int, list[date]] = {}

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
                # Issue #2 (daily-expiration global-snap bug): NearestToTarget
                # must snap to an expiration that is actually LISTED on THIS trade
                # date, not to the nearest in the whole-window global set.  When a
                # per-date listing map is supplied (``available_expirations_by_date``
                # — one distinct scan over the window in the caller), pick from the
                # expirations quoted on ``d``; otherwise fall back to the global
                # ``available`` (legacy behaviour).
                #
                # NOT just a daily-root (OPT_BTC) fix: sparse monthly/weekly roots
                # like SPX (OPT_SP_500) carry the SAME latent hole.  SPX lists
                # weeklies with the same listing-lag — an expiration exists in the
                # global set (dim scan) before it is ever quoted (price row) on
                # early trade dates.  When the global-nearest is one of those
                # not-yet-listed expirations, the legacy path snaps to it, Phase B
                # finds 0 rows on ``d`` → a silent ``no_chain_for_date`` NaN.  The
                # per-date map moves the pick to an expiration actually listed on
                # ``d`` → a real value.  This is strictly NaN→value: the per-date
                # pick differs from the global pick ONLY when the global pick is
                # unlisted that day, so an already-valid global pick is never
                # changed to a different value (verified live on OPT_SP_500 P,
                # target_dte=30, 2023-01..03: 7/47 dates changed, all NaN→value,
                # zero value→value — e.g. 2023-03-02 global pick 2023-04-07 had 0
                # price rows, per-date pick 2023-03-24 had 222).
                avail_for_d = available
                if available_expirations_by_date is not None:
                    day_listed = available_expirations_by_date.get(d)
                    if day_listed:
                        windowed = [
                            e for e in day_listed if lower_bound <= e <= far_future
                        ]
                        # Defensive: if every listed expiration falls outside the
                        # probe window, still prefer the date's real listings over
                        # a global pick that cannot exist on this date.
                        avail_for_d = windowed if windowed else sorted(day_listed)
                if not avail_for_d:
                    expirations[idx] = None
                else:
                    # roll_offset (ROLL-EARLY axis): resolve maturity as of
                    # (d + offset) so every roll happens that much earlier.
                    ref = _apply_roll_offset(d, roll_offset)
                    expirations[idx] = maturity_resolver.resolve_with_chain(
                        ref_date=ref,
                        rule=maturity,
                        available_expirations=avail_for_d,
                    )
                    if coverage_aware and isinstance(selection, ByDelta):
                        # Build the ordered candidate list for the coverage-aware
                        # Phase-C re-pick.  candidates[idx][0] == the resolve above
                        # (same ranking key), so the coverage-blind grouping/output
                        # below is unchanged; the extras only enable the retry.
                        # Scoped to ByDelta — the only criterion with a delta
                        # "coverage" notion (ByStrike is exact; ByMoneyness support
                        # is a later pass, see the design note).
                        candidates[idx] = _coverage_candidates(
                            ref, maturity.target_dte_days, avail_for_d
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

    # COVERAGE-AWARE mode only: also fetch each date's NON-primary candidate
    # expirations, so Phase C can re-pick the nearest-DTE candidate that is
    # actually covered.  The primary candidate (== ``expirations[idx]``) is
    # already in the group above; add the rest here.  (Default mode never
    # populates ``candidates`` → this loop is a no-op, so Phase B is
    # byte-identical.)
    if coverage_aware and candidates:
        for idx, d in queryable:
            for cand in candidates.get(idx, ()):
                if cand != expirations.get(idx):
                    exp_groups[cand].append((idx, d))

    # HOLD mode only: the OLD contract's return INTO the roll day needs the OLD
    # contract's mid ON the roll day, but the roll day's resolved expiration is
    # the NEW one — so Phase B would fetch only the NEW chain there.  Add each
    # roll day (the first queryable date whose expiration differs from the prior
    # queryable date's) to the PREVIOUS (OLD) expiration's fetch group too, so the
    # merged ``chain_index`` for the roll day carries BOTH chains.  (Default mode
    # never does this — each date stays in exactly one group, so Phase B is
    # byte-identical.)
    if hold_between_rolls:
        prev_exp: date | None = None
        for idx, d in queryable:
            exp = expirations.get(idx)
            if exp is not None and prev_exp is not None and exp != prev_exp:
                # ``idx`` is a roll day → also fetch the OLD (prev) expiration here.
                exp_groups[prev_exp].append((idx, d))
            if exp is not None:
                prev_exp = exp

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
        group: list[tuple[int, date]],
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        group_dates = [d for _idx, d in group]
        try:
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
        except Exception as exc:  # noqa: BLE001
            # A single expiration's bulk fetch failing (a transient dwh error,
            # or a PoolTimeout under contention) must NOT abort the whole
            # resolve.  Mirror Phase C's per-date degradation: mark THIS
            # expiration's dates ``data_access_error`` (-> NaN) and return an
            # empty chain so the other expirations still resolve.  Without this,
            # the exception propagates out of the gather and the API returns a
            # hard 500 for the entire series (the OPT_SP_500 PoolTimeout
            # symptom).
            for idx, _d in group:
                error_codes[idx] = "data_access_error"
            _log.debug(
                "Phase-B bulk fetch failed exp=%s (%d dates): %s",
                exp,
                len(group_dates),
                exc,
            )
            result = {}
        # Tick progress after each expiration fetch so the frontend
        # sees Phase B movement instead of a stuck 0%.  The progress
        # endpoint clamps fraction to [0, 1] so the extra ticks are safe.
        if progress_callback is not None:
            try:
                progress_callback()
            except Exception:  # pragma: no cover (defensive)
                pass
        return result

    fetch_tasks = [_fetch_exp(exp, group) for exp, group in exp_groups.items()]
    fetch_results = await asyncio.gather(*fetch_tasks)
    for result in fetch_results:
        # MERGE (extend), do not overwrite: in HOLD mode a roll day appears in
        # TWO expiration groups (OLD + NEW), so its rows arrive from two fetches
        # and must be concatenated.  In the default path each date is in exactly
        # one group, so this is a plain insert (byte-identical).
        for d, rows in result.items():
            if d in chain_index:
                chain_index[d] = chain_index[d] + rows
            else:
                chain_index[d] = rows

    # ── Shared per-date selection (used by BOTH Phase C and the HOLD path) ──
    async def _select_contract_on_date(
        d: date,
        rows: list[tuple[OptionContractDoc, OptionDailyRow]],
    ) -> tuple[OptionContractDoc | None, str | None]:
        """Run the configured selection criterion against ``rows`` for date ``d``.

        Returns ``(contract, error_code)`` — exactly one non-None.  Mirrors the
        per-date Phase-C matching (ByStrike / ByDelta pure-CPU; ByMoneyness needs
        the underlying price), but yields the CONTRACT rather than writing a
        stream value, so the HOLD path can select once per segment and freeze it.
        """
        if not rows:
            return None, "no_chain_for_date"
        if isinstance(selection, ByStrike):
            result = match_by_strike(rows, selection.strike)
        elif isinstance(selection, ByDelta):
            deltas = [r.delta_stored for _c, r in rows]
            result = match_by_delta(
                rows=rows,
                deltas=deltas,
                target=selection.target_delta,
                tolerance=selection.tolerance,
                strict=selection.strict,
                chain_size=len(rows),
            )
        elif isinstance(selection, ByMoneyness):
            if underlying_price_resolver is None:
                return None, "missing_underlying_price"
            S = await underlying_price_resolver(rows[0][0], d)
            if S is None or S <= 0:
                return None, "missing_underlying_price"
            result = match_by_moneyness(
                rows=rows,
                target_K_over_S=selection.target_K_over_S,
                tolerance=selection.tolerance,
                underlying_price=float(S),
            )
        else:  # pragma: no cover (defensive — union is closed)
            return None, "data_access_error"
        if result.error_code is not None:
            return None, result.error_code
        if result.contract is None:  # pragma: no cover (defensive)
            return None, "no_chain_for_date"
        return result.contract, None

    # ── HOLD path (select-and-hold): select ONCE per maturity segment, freeze
    #    the contract between rolls, and emit the per-date HELD-CONTRACT MID LEVEL
    #    (the OLD contract's mid on the roll day) plus the is_roll/roll_premium
    #    side-channel (via hold_roll_info_out) that signal_exec's fixed-contract
    #    dollar-P&L recurrence consumes — NOT a stitched level, so no option
    #    ratio-adjust.  Bypasses the per-date Phase C entirely and returns early. ─
    if hold_between_rolls:
        return await _resolve_hold(
            dates=dates,
            queryable=queryable,
            expirations=expirations,
            chain_index=chain_index,
            attr_name=attr_name,
            stream=stream,
            values=values,
            error_codes=error_codes,
            contracts=contracts,
            snap_notes=snap_notes,
            select_contract_on_date=_select_contract_on_date,
            progress_callback=progress_callback,
            underlying_price_resolver=underlying_price_resolver,
            kernel=kernel,
            roll_info_out=hold_roll_info_out,
        )

    # ── Phase C: Per-date selection + stream extraction ─────────────
    #
    # ByStrike and ByDelta SELECT with pure CPU (matching against pre-fetched
    # rows).  For row-attribute streams that whole path is sync (no I/O).  But
    # ``bs_mid`` needs the underlying FUTURE price to price the contract, so it
    # routes ByStrike/ByDelta through an async gather too (like ByMoneyness).
    # ByMoneyness always needs I/O (underlying_price_resolver) for selection.

    def _match_delta(
        rows: list[tuple[OptionContractDoc, OptionDailyRow]],
        *,
        strict: bool,
    ) -> SelectionResult:
        """``ByDelta`` match over *rows* (``strict`` overridable for coverage)."""
        deltas = [r.delta_stored for _c, r in rows]
        return match_by_delta(
            rows=rows,
            deltas=deltas,
            target=selection.target_delta,  # type: ignore[union-attr]
            tolerance=selection.tolerance,  # type: ignore[union-attr]
            strict=strict,
            chain_size=len(rows),
        )

    def _coverage_aware_delta_select(
        idx: int, d: date
    ) -> tuple[OptionContractDoc, OptionDailyRow] | None:
        """Coverage-aware ``ByDelta`` selection over the date's CANDIDATE expiries.

        Walks ``candidates[idx]`` nearest-DTE first (the same order the
        coverage-blind path ranks by), restricting ``chain_index[d]`` to each
        candidate's expiration and running a STRICT ``ByDelta`` match (delta must
        be within ``selection.tolerance``).  Returns the FIRST candidate expiry
        that has such a match — i.e. the nearest-DTE expiration that is actually
        COVERED near the target delta.

        Fallback: when NO candidate is covered, re-runs the match on the PRIMARY
        candidate (``candidates[idx][0]`` == the coverage-blind pick) with the
        selection's OWN ``strict`` — identical to the non-coverage result, so the
        worst case never regresses to all-NaN (it degrades to today's behaviour).
        A ``coverage_skipped:<iso>`` note is recorded when a strictly-nearer
        candidate was skipped for a covered farther one.
        """
        cand = candidates.get(idx) or []
        if not cand:
            # No candidate list (e.g. date resolved to None) — defer to the plain
            # path over whatever chain_index holds.
            return _select_sync(idx, d, _restrict_expiration=None)
        primary = cand[0]
        for pos, exp in enumerate(cand):
            exp_rows = [
                (c, r) for (c, r) in chain_index.get(d, []) if c.expiration == exp
            ]
            if not exp_rows:
                continue
            result = _match_delta(exp_rows, strict=True)
            if result.error_code is None and result.contract is not None:
                row = _row_for_contract(exp_rows, result.contract)
                if row is None:  # pragma: no cover (defensive)
                    continue
                contracts[idx] = result.contract
                if pos > 0:
                    # A strictly-nearer candidate existed but was not covered.
                    snap_notes[idx] = f"coverage_skipped:{primary.isoformat()}"
                return result.contract, row
        # No covered candidate → fall back to the primary's best-effort match
        # (the coverage-blind result, so no regression vs. today).
        return _select_sync(idx, d, _restrict_expiration=primary)

    def _select_sync(
        idx: int,
        d: date,
        _restrict_expiration: date | None = None,
    ) -> tuple[OptionContractDoc, OptionDailyRow] | None:
        """Run ByStrike/ByDelta selection for date ``d`` (pure CPU).

        Returns the selected ``(contract, row)`` or ``None`` (having set the
        per-date error_code).  Does NOT extract the stream value — that is done
        by the caller (sync for row-attr streams, async for bs_mid).

        ``_restrict_expiration`` (coverage-aware fallback only): when set, the
        match runs against ONLY that expiration's rows from the merged
        ``chain_index`` (in coverage mode the index carries several candidate
        expiries).  ``None`` (the default / non-coverage path) uses the full
        chain, byte-identical to before."""
        rows = chain_index.get(d, [])
        if _restrict_expiration is not None:
            rows = [(c, r) for (c, r) in rows if c.expiration == _restrict_expiration]
        if not rows:
            error_codes[idx] = "no_chain_for_date"
            return None
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
            return None
        if result.contract is None:  # pragma: no cover (defensive)
            error_codes[idx] = "no_chain_for_date"
            return None
        row = _row_for_contract(rows, result.contract)
        if row is None:  # pragma: no cover (defensive)
            error_codes[idx] = "no_chain_for_date"
            return None
        # Capture the selected contract BEFORE extracting the value so a missing
        # stream value still records the contract identity (selection succeeded).
        contracts[idx] = result.contract
        return result.contract, row

    # Coverage-aware routing lives in ONE place: ByDelta in coverage mode walks
    # the candidate expiries; everything else (ByStrike, or coverage off) uses the
    # plain full-chain match.  (ByMoneyness is handled on its own async path
    # below.)
    _use_coverage_delta = coverage_aware and isinstance(selection, ByDelta)

    def _do_delta_or_strike_select(
        idx: int, d: date
    ) -> tuple[OptionContractDoc, OptionDailyRow] | None:
        if _use_coverage_delta:
            return _coverage_aware_delta_select(idx, d)
        return _select_sync(idx, d)

    def _resolve_one_sync(idx: int, d: date) -> None:
        """CPU-only resolution for ByStrike/ByDelta with a ROW-ATTRIBUTE stream."""
        try:
            sel = _do_delta_or_strike_select(idx, d)
            if sel is None:
                return
            contract, row = sel
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

    async def _resolve_one_bs_mid_sync_select(idx: int, d: date) -> None:
        """ByStrike/ByDelta selection (sync) + bs_mid pricing (async I/O).

        Shares the Phase-B/-C gate so the underlying-price fan-out stays within
        the dwh pool."""
        _sem = (
            concurrency_gate
            if concurrency_gate is not None
            else asyncio.Semaphore(_MAX_INFLIGHT_PER_DATE)
        )
        try:
            sel = _do_delta_or_strike_select(idx, d)
            if sel is None:
                return
            contract, row = sel
            async with _sem:
                value, code = await _extract_stream_value(
                    stream=stream,
                    contract=contract,
                    row=row,
                    d=d,
                    attr_name=attr_name,
                    underlying_price_resolver=underlying_price_resolver,
                    kernel=kernel,
                )
            if code is not None:
                error_codes[idx] = code
                return
            if value is not None:
                values[idx] = value
        except Exception as exc:
            error_codes[idx] = "data_access_error"
            _log.debug("resolve_one_bulk bs_mid date=%s failed: %s", d, exc)
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

                # Extract the value via the shared extractor so ``bs_mid`` (priced
                # from the underlying future) works on the ByMoneyness path too.
                # The underlying was already fetched above for selection; the
                # bs_mid extractor fetches it again (result-invariant; the futures
                # adapter memoizes per resolve, so no extra dwh round-trip in
                # production).
                value, code = await _extract_stream_value(
                    stream=stream,
                    contract=result.contract,
                    row=row,
                    d=d,
                    attr_name=attr_name,
                    underlying_price_resolver=underlying_price_resolver,
                    kernel=kernel,
                )
                if code is not None:
                    error_codes[idx] = code
                    return
                if value is not None:
                    values[idx] = value
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
    elif stream == _BS_MID:
        # ByStrike / ByDelta with the COMPUTED bs_mid stream: selection is pure
        # CPU but pricing needs the underlying future (I/O) → async gather.
        bs_tasks: list[asyncio.Task[None]] = []
        for idx, d in queryable:
            if error_codes[idx] is not None:
                if progress_callback is not None:
                    try:
                        progress_callback()
                    except Exception:  # pragma: no cover (defensive)
                        pass
                continue
            bs_tasks.append(
                asyncio.ensure_future(_resolve_one_bs_mid_sync_select(idx, d))
            )
        if bs_tasks:
            await asyncio.gather(*bs_tasks)
    else:
        # ByStrike / ByDelta with a ROW-ATTRIBUTE stream: pure CPU — synchronous
        # for-loop, no asyncio tasks or semaphore overhead.
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
    cycle: str | Sequence[str] | None,
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
    available_expirations_by_date: Mapping[date, Sequence[date]] | None = None,
    concurrency_gate: "asyncio.Semaphore | None" = None,
    hold_between_rolls: bool = False,
    hold_roll_info_out: "dict[str, NDArray[np.float64]] | None" = None,
    coverage_aware: bool = False,
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
    hold_between_rolls:
        SELECT-AND-HOLD mode (default ``False`` = the current behaviour,
        byte-identical).  When ``True``, the resolver picks the contract ONCE
        at each maturity ROLL (a roll = the resolved *expiration* changing — the
        ``derive_rolls`` discriminator) and FREEZES it between rolls, then emits
        — PER DATE — the HELD-CONTRACT PREMIUM (mid) LEVEL of the contract that
        OWNS that date's value: the held contract on interior dates, and the OLD
        contract's mid ON the roll day (so the step ending on the roll day is the
        OLD's OWN move into the roll — realise the OLD).  Requires the bulk chain
        reader (like ``roll_offset`` / ``EndOfMonth``); raises ``ValueError`` on
        the legacy per-date path.  This is the input for ``signal_exec``'s
        FIXED-CONTRACT DOLLAR-P&L recurrence (size a held quantity once per roll
        off the compounding NAV and the roll premium, book ``qty·Δpremium`` daily,
        realise+resize at the next roll), which is oracle-exact against the
        ground-truth Java sim — fixing the meaningless P&L a ByDelta/ByMoneyness
        signal otherwise gets from the daily strike churn.  It does NOT
        stitch/ratio-adjust the option series (which would court the hard "no
        ratio-adjustment for options" constraint).  NOTE: in hold mode ``values``
        is the held-contract mid LEVEL (an honest per-leg premium display); the
        signals P&L path is the only consumer that pairs it with
        ``hold_roll_info_out`` (the display materialiser never enables hold mode,
        so the Data-page/chart stream is unchanged).
    hold_roll_info_out:
        Optional out-dict populated ONLY in hold mode.  Receives
        ``{"is_roll", "roll_premium"}`` — both length-``T`` arrays aligned to
        ``dates``.  ``is_roll`` (float 0/1) marks each hold segment's first date
        (incl. the initial open at index 0); ``roll_premium`` at those dates is
        the NEW (this-segment) contract's roll-day OPEN mid — the base against
        which that segment's daily P&L and its held-quantity sizing are computed
        (``values`` on a roll date carries the OLD mid, so this is the ONLY place
        the NEW open premium is surfaced → the seam is exact, never a raw old→new
        level gap).  The 3-tuple return is unchanged; roll info travels through
        this out-dict so every non-hold caller is unaffected.
    coverage_aware:
        COVERAGE-AWARE expiration selection (default ``False`` = the current
        behaviour, byte-identical).  Effective ONLY on the bulk path, ONLY for
        ``NearestToTarget`` maturity + ``ByDelta`` selection.  When ``True`` the
        resolver, instead of the single nearest-DTE listed expiration, considers a
        small bounded set of nearest-DTE candidate expirations (see
        :data:`_COVERAGE_MAX_CANDIDATES` / :data:`_COVERAGE_DTE_WINDOW_DAYS`) and
        picks the nearest-DTE one that actually has an IN-TOLERANCE delta strike —
        skipping thinly-listed expirations whose only greeked strikes are far from
        the target delta (the OPT_SP_500 later-era failure where ``ByDelta(-0.10)``
        picked deep-OTM garbage from a gappy target month).  If NO candidate is
        covered it falls back to the nearest-DTE best-effort match (== the
        coverage-blind result), so it never regresses to all-NaN.  A
        ``coverage_skipped:<iso>`` success-side note (same channel as
        ``snapped_to:``) records when a strictly-nearer expiration was skipped.
        Requires the bulk chain reader; not composable with ``hold_between_rolls``
        yet — both raise ``ValueError``.

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
    # ``bs_mid`` is a valid COMPUTED label (priced, not read off a row) — it is
    # deliberately absent from ``_STREAM_TO_ATTR``; every OTHER label must map to
    # a row field.
    if stream != _BS_MID and stream not in _STREAM_TO_ATTR:
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
            available_expirations_by_date=available_expirations_by_date,
            concurrency_gate=concurrency_gate,
            hold_between_rolls=hold_between_rolls,
            hold_roll_info_out=hold_roll_info_out,
            coverage_aware=coverage_aware,
        )

    # ── Legacy per-date path (fallback when no bulk reader wired) ──

    # Symmetric guard: coverage-aware selection walks the pre-fetched candidate
    # chains from Phase B, which only the bulk path materialises.  The legacy
    # per-date path resolves + selects one expiration per date with no candidate
    # set, so it cannot honour the flag — fail loudly rather than silently
    # returning the coverage-BLIND series.  Production always wires the bulk
    # reader.
    if coverage_aware:
        raise ValueError(
            "coverage_aware requires the bulk chain reader; the legacy per-date "
            "path does not support coverage-aware expiration selection"
        )

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
    # Symmetric guard: select-and-hold needs the bulk path's pre-resolved
    # per-date expirations to segment the roll boundaries and re-read the HELD
    # contract off the pre-fetched chain.  The legacy per-date path re-selects a
    # fresh contract each day (the churn this mode exists to eliminate), so it
    # cannot honour the flag — fail loudly rather than silently returning the
    # unheld daily-reselect series.  Production always wires the bulk reader.
    if hold_between_rolls:
        raise ValueError(
            "hold_between_rolls requires the bulk chain reader; the legacy "
            "per-date path does not support select-and-hold"
        )
    # Symmetric guard: the EndOfMonth monthly-hold sweep lives in the bulk
    # Phase A, so the legacy per-date path cannot honour it (it would silently
    # re-resolve EndOfMonth per-date, diverging from the held-monthly result).
    if isinstance(maturity, EndOfMonth):
        raise ValueError(
            "EndOfMonth maturity requires the bulk chain reader; the legacy "
            "per-date path does not support the monthly-hold roll"
        )
    # Symmetric guard: the COMPUTED ``bs_mid`` stream (Black-76 from IV + the
    # underlying future) is implemented on the bulk path's extraction only.
    # Production always wires the bulk reader; fail loudly rather than KeyError on
    # the absent ``_STREAM_TO_ATTR`` entry or silently return a wrong series.
    if stream == _BS_MID:
        raise ValueError(
            "bs_mid stream requires the bulk chain reader; the legacy per-date "
            "path does not support the computed Black-76 price stream"
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
