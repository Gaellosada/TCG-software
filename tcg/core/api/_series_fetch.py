"""Shared per-instrument series-fetching machinery.

Extracted from ``signals.py`` so it can be reused by BOTH the signal
compute path and the standalone Data-page basket-series endpoint
(``tcg.core.api._basket_compute``) without ``data.py`` importing the
signals router (no router→router edge; import-linter ``core.api`` is a
flat layer).

Everything here is general series machinery — it builds typed leaf
instruments from Pydantic refs, enumerates per-instrument date axes, and
returns the price-fetcher closure that dispatches on ``InputInstrument``
kind.  None of it depends on the signal-only request carriers
(``_InputIn`` / ``_ResolvedBasketInput`` / ``_parse_input``), which is
why it lives in its own neutral module rather than ``signals.py``.

The function BODIES are kept byte-identical to their previous home so
behaviour is unchanged; ``signals.py`` re-imports them.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np
import numpy.typing as npt

from tcg.core.api._adapters import build_roll_config
from tcg.core.api._models import (
    ContinuousInstrumentRef,
    OptionStreamRef,
    SpotInstrumentRef,
)
from tcg.data.protocols import MarketDataService
from tcg.engine.signal_exec import SignalDataError, SignalValidationError
from tcg.types.errors import DataNotFoundError
from tcg.types.options import expand_cycle
from tcg.types.signal import (
    InputInstrument,
    InstrumentBasket,
    InstrumentContinuous,
    InstrumentOptionStream,
    InstrumentSpot,
)

logger = logging.getLogger(__name__)


def _hold_cache_key(instrument: InstrumentOptionStream) -> Any:
    """Identity for the per-signal hold-mode roll-info / multiplier caches.

    Mirrors the engine's option-stream identity (collection/type/cycle/maturity/
    selection/stream/roll_offset) — the axes that determine the held-premium + roll
    structure.  ``nav_times`` is EXCLUDED (a downstream sizing multiple; the series
    is identical).

    Guardrail Sign 4: ``sizing_mode`` + ``futures_reference`` ARE part of the key.
    futures_notional mode attaches a ``roll_future_ref`` array (and picks a
    different reference per ``futures_reference``), so a premium_notional cached
    result must NOT be served for a futures_notional leg with otherwise identical
    axes (and vice-versa) — a collision would silently mis-size.
    """
    return (
        instrument.collection,
        instrument.option_type,
        instrument.cycle or "",
        repr(instrument.maturity),
        repr(instrument.selection),
        instrument.stream,
        (int(instrument.roll_offset.value), instrument.roll_offset.unit),
        instrument.sizing_mode,
        instrument.futures_reference,
    )


def _continuous_cache_key(instrument: InstrumentContinuous) -> Any:
    """Identity for the per-signal continuous-roll-info cache.

    Mirrors the axes ``get_continuous`` rolls on (collection + roll config) — the
    same axes that determine the stitched series and thus its interior roll
    boundaries.  Two continuous inputs with identical axes share the cached roll
    dates (resolved once during the normal ``fetch``).
    """
    return (
        instrument.collection,
        instrument.adjustment,
        instrument.cycle or "",
        int(instrument.roll_offset),
        instrument.strategy,
    )


# ---------------------------------------------------------------------------
# Leg materialisation — Pydantic ref → typed leaf instrument
# ---------------------------------------------------------------------------


def _materialise_leg_instrument(
    instrument_ref: SpotInstrumentRef | ContinuousInstrumentRef | OptionStreamRef,
    *,
    input_id: str,
    leg_index: int,
) -> "InstrumentSpot | InstrumentContinuous | InstrumentOptionStream":
    """Build a typed leaf-instrument from a Pydantic instrument-ref.

    Shared between inline-basket leg dispatch (wire-side
    :class:`BasketLeg.instrument`) and saved-basket leg materialisation
    (Mongo-side ``BasketDoc.legs[i]["instrument"]`` dicts re-validated
    through the same Pydantic refs).  Mirrors the non-basket
    :func:`_parse_input` branches.
    """
    if isinstance(instrument_ref, SpotInstrumentRef):
        if not instrument_ref.collection or not instrument_ref.instrument_id:
            raise SignalValidationError(
                f"input {input_id!r}: basket leg {leg_index} spot "
                f"instrument requires collection + instrument_id"
            )
        return InstrumentSpot(
            collection=instrument_ref.collection,
            instrument_id=instrument_ref.instrument_id,
        )
    if isinstance(instrument_ref, ContinuousInstrumentRef):
        if not instrument_ref.collection:
            raise SignalValidationError(
                f"input {input_id!r}: basket leg {leg_index} continuous "
                f"instrument requires collection"
            )
        return InstrumentContinuous(
            collection=instrument_ref.collection,
            adjustment=instrument_ref.adjustment,
            cycle=instrument_ref.cycle,
            roll_offset=int(instrument_ref.rollOffset),
            strategy=instrument_ref.strategy,
        )
    if isinstance(instrument_ref, OptionStreamRef):
        if not instrument_ref.collection:
            raise SignalValidationError(
                f"input {input_id!r}: basket leg {leg_index} option_stream "
                f"instrument requires collection"
            )
        # Reject select-and-hold on a BASKET LEG (single-place guard).  The
        # fixed-contract dollar-P&L accounting sizes a held quantity off the whole
        # signal's NAV per option leg and books qty·Δpremium — that is defined for
        # a STANDALONE option input, not a leg blended into a basket's weighted
        # composite (a multi-leg held book is a Phase-2 / delta-hedge concern).
        # Fail loudly at parse rather than silently ignore the flag or emit
        # meaningless P&L.
        if instrument_ref.hold_between_rolls:
            raise SignalValidationError(
                f"input {input_id!r}: basket leg {leg_index} option_stream cannot "
                f"use hold_between_rolls (fixed-contract dollar-P&L is a standalone "
                f"option input; multi-leg held books are not supported)"
            )
        # ONE shared ref→dataclass converter (see options.py) — identical field
        # mapping to signals._parse_input and the portfolio hold path.  The
        # hold-on-a-basket-leg rejection above runs FIRST (this path is only ever
        # reached with hold OFF).
        from tcg.core.api.options import option_stream_ref_to_instrument

        return option_stream_ref_to_instrument(instrument_ref)
    raise SignalValidationError(
        f"input {input_id!r}: basket leg {leg_index} has unsupported "
        f"instrument shape {type(instrument_ref).__name__!r}"
    )


def _validate_saved_basket_leg_against_asset_class(
    *, asset_class: str, instrument_type: str, basket_id: str, leg_index: int
) -> None:
    """Mirror the strict per-class mapping on the saved-basket path.

    Saved baskets are written via the CRUD route which already enforces
    the mapping at write-time, but a basket created before the iter-3
    schema would slip through; verifying again at resolve time keeps
    the invariant locally checkable and surfaces re-save instructions
    in the error envelope.
    """
    expected = {
        "equity": "spot",
        "index": "spot",
        "future": "continuous",
        "option": "option_stream",
    }.get(asset_class)
    if expected is None:
        raise SignalValidationError(
            f"basket {basket_id!r} leg {leg_index}: unsupported "
            f"asset_class={asset_class!r}"
        )
    if instrument_type != expected:
        raise SignalValidationError(
            f"basket {basket_id!r} leg {leg_index}: asset_class="
            f"{asset_class!r} requires instrument.type={expected!r}, "
            f"got {instrument_type!r}"
        )


def _saved_basket_leg_to_typed(
    leg_dict: dict,
    *,
    basket_id: str,
    leg_index: int,
    asset_class: str,
) -> tuple["InstrumentSpot | InstrumentContinuous | InstrumentOptionStream", float]:
    """Materialise one persisted ``BasketDoc.legs[i]`` dict into a
    ``(typed-instrument, weight)`` pair.

    Re-validates the persisted ``instrument`` sub-dict through the
    Pydantic instrument refs so we get the same field validation
    saved-basket creates went through, then dispatches via
    :func:`_materialise_leg_instrument`.
    """
    if not isinstance(leg_dict, dict):
        raise SignalValidationError(
            f"basket {basket_id!r} leg {leg_index}: persisted leg is "
            f"not a dict ({type(leg_dict).__name__})"
        )
    instrument_payload = leg_dict.get("instrument")
    weight = leg_dict.get("weight")
    if instrument_payload is None or weight is None:
        raise SignalValidationError(
            f"basket {basket_id!r} leg {leg_index}: persisted leg is "
            f"missing 'instrument' or 'weight' — re-save the basket"
        )
    inst_type = (
        instrument_payload.get("type") if isinstance(instrument_payload, dict) else None
    )
    _validate_saved_basket_leg_against_asset_class(
        asset_class=asset_class,
        instrument_type=inst_type or "",
        basket_id=basket_id,
        leg_index=leg_index,
    )
    if inst_type == "spot":
        instrument_ref = SpotInstrumentRef.model_validate(instrument_payload)
    elif inst_type == "continuous":
        instrument_ref = ContinuousInstrumentRef.model_validate(instrument_payload)
    elif inst_type == "option_stream":
        instrument_ref = OptionStreamRef.model_validate(instrument_payload)
    else:
        raise SignalValidationError(
            f"basket {basket_id!r} leg {leg_index}: unsupported "
            f"instrument.type={inst_type!r}"
        )
    typed_inst = _materialise_leg_instrument(
        instrument_ref, input_id=f"saved:{basket_id}", leg_index=leg_index
    )
    return typed_inst, float(weight)


# ---------------------------------------------------------------------------
# Per-leaf date-array enumeration (used for input-overlap windowing)
# ---------------------------------------------------------------------------


async def _date_array_for_leaf_instrument(
    inst: "InstrumentSpot | InstrumentContinuous | InstrumentOptionStream",
    svc: MarketDataService,
    *,
    start: date | None,
    end: date | None,
    err_prefix: str,
) -> npt.NDArray[np.int64]:
    """Fetch the date array for a non-basket leaf instrument.

    Shared between the top-level :func:`compute_input_overlap` loop and
    its basket-leg recursion; mirrors exactly the per-type branches
    that already lived inside the loop.
    """
    if isinstance(inst, InstrumentSpot):
        try:
            series = await svc.get_prices(
                inst.collection,
                inst.instrument_id,
                start=start,
                end=end,
            )
        except DataNotFoundError as exc:
            raise SignalDataError(f"{err_prefix}: {exc}") from exc
        if series is None:
            raise SignalDataError(
                f"{err_prefix}: instrument {inst.instrument_id!r} not "
                f"found in {inst.collection!r}"
            )
        return series.dates
    if isinstance(inst, InstrumentContinuous):
        try:
            roll_config = build_roll_config(
                inst.adjustment, inst.cycle, inst.roll_offset, strategy=inst.strategy
            )
        except ValueError as exc:
            raise SignalDataError(f"{err_prefix}: {exc}") from exc
        try:
            cseries = await svc.get_continuous(
                inst.collection, roll_config, start=start, end=end
            )
        except DataNotFoundError as exc:
            raise SignalDataError(f"{err_prefix}: {exc}") from exc
        if cseries is None:
            raise SignalDataError(
                f"{err_prefix}: continuous series unavailable for {inst.collection!r}"
            )
        return cseries.prices.dates
    if isinstance(inst, InstrumentOptionStream):
        from tcg.core.api._options_materialise import _business_dates_in_range
        from tcg.data._utils import date_to_int

        # Broaden 'M' to the full 3rd-Friday series so the date axis covers the
        # real monthly across eras (see expand_cycle); other cycles unchanged.
        all_expirations = await svc.list_option_expirations_filtered(
            inst.collection,
            option_type=inst.option_type,
            cycle=expand_cycle(inst.cycle),
        )
        if not all_expirations:
            raise SignalDataError(
                f"{err_prefix}: no option expirations found for "
                f"{inst.collection} {inst.option_type} cycle={inst.cycle}"
            )
        lo_date = min(all_expirations)
        hi_date = max(all_expirations)
        if start is not None:
            lo_date = max(lo_date, start)
        if end is not None:
            hi_date = min(hi_date, end)
        trade_dates = _business_dates_in_range(lo_date, hi_date)
        if not trade_dates:
            raise SignalDataError(
                f"{err_prefix}: no business days in option date "
                f"range [{lo_date}, {hi_date}]"
            )
        return np.array([date_to_int(d) for d in trade_dates], dtype=np.int64)
    raise SignalDataError(
        f"{err_prefix}: unsupported leaf instrument type {type(inst).__name__!r}"
    )


async def basket_leg_date_intersection(
    basket: InstrumentBasket,
    svc: MarketDataService,
    *,
    start: date | None,
    end: date | None,
    err_prefix: str,
) -> npt.NDArray[np.int64]:
    """Intersect a basket's per-leg date arrays into one int-date axis.

    Shared by ``compute_input_overlap``'s ``InstrumentBasket`` branch (which
    then intersects this against the OTHER inputs) and the standalone
    ``_basket_compute._resolve_window`` (which converts it to a date window).
    ``err_prefix`` is threaded through so each caller keeps its exact error
    message.  Raises :class:`SignalDataError` when no dates overlap.
    """
    basket_dates: npt.NDArray[np.int64] | None = None
    for leg_index, (leg_inst, _leg_weight) in enumerate(basket.legs):
        leg_dates = await _date_array_for_leaf_instrument(
            leg_inst,
            svc,
            start=start,
            end=end,
            err_prefix=f"{err_prefix} basket leg {leg_index}",
        )
        if basket_dates is None:
            basket_dates = leg_dates
        else:
            basket_dates = np.intersect1d(basket_dates, leg_dates, assume_unique=False)
    if basket_dates is None or basket_dates.size == 0:
        raise SignalDataError(f"{err_prefix}: basket has no overlapping dates")
    return basket_dates


def _has_option_stream_dependency(
    inst: "InstrumentSpot | InstrumentContinuous | InstrumentOptionStream "
    "| InstrumentBasket",
) -> bool:
    """True iff resolving this instrument requires an option-stream date
    enumeration (which needs an explicit date window via
    `_business_dates_in_range`).

    The single-input short-circuit in ``compute_input_overlap`` MUST
    fall through into the per-input loop for these — otherwise the
    fetcher inherits ``start=end=None`` from the envelope and raises
    "option_stream requires explicit start/end dates" at the leaf
    resolver (`signals.py` option_stream branch).  Spot and continuous
    legs do not need this because their date axis is borrowed from the
    underlying price series.
    """
    if isinstance(inst, InstrumentOptionStream):
        return True
    if isinstance(inst, InstrumentBasket):
        return any(
            isinstance(leg_inst, InstrumentOptionStream) for leg_inst, _w in inst.legs
        )
    return False


# ---------------------------------------------------------------------------
# Price fetcher adapter — dispatches on InputInstrument kind
# ---------------------------------------------------------------------------


def _pick_field(series, field: str) -> npt.NDArray[np.float64]:
    if field == "close":
        return series.close.astype(np.float64, copy=False)
    if field == "open":
        return series.open.astype(np.float64, copy=False)
    if field == "high":
        return series.high.astype(np.float64, copy=False)
    if field == "low":
        return series.low.astype(np.float64, copy=False)
    if field == "volume":
        return series.volume.astype(np.float64, copy=False)
    raise SignalValidationError(
        f"instrument field {field!r} is not supported; "
        f"expected one of close/open/high/low/volume"
    )


def make_signal_fetcher(
    svc: MarketDataService,
    start: date | None,
    end: date | None,
    *,
    diag_sink: list[dict[str, Any]] | None = None,
    use_chain_cache: bool = True,
) -> Any:
    # ``diag_sink`` (opt-in): when a list is supplied, every option_stream fetch
    # appends a per-leg coverage record ``{descriptor, dates, error_codes}`` so a
    # caller (the Data-page basket path) can explain WHY points are missing rather
    # than draw a silently broken line.  ``None`` (signals / portfolio) = no
    # collection, and the ``fetch`` return signature is unchanged.
    # Lazy-init cache for option_stream wiring — built once on first
    # option_stream fetch, then reused for all subsequent option_stream
    # inputs within this signal evaluation.
    _os_wiring_cache: dict[str, Any] = {}
    # Per-signal cache of hold-mode option roll info, keyed by the instrument
    # identity.  Populated during the normal ``fetch`` of a hold-mode option
    # input (which runs FIRST, via operand resolution) and read by
    # ``fetch_hold_roll_info`` (which ``signal_exec`` calls afterwards for the
    # fixed-contract dollar-P&L path) — so the resolver runs ONCE per hold input.
    _hold_roll_info_cache: dict[Any, tuple[Any, Any, Any, Any]] = {}
    # Companion: the resolved (m_fut, m_opt) multipliers for a futures-notional
    # hold input, keyed the same way, populated during the same ``fetch``.  Read by
    # ``fetch_hold_multipliers`` (signal_exec / portfolio) — the engine never reads
    # dwh, so the live-first / config-fallback resolution happens here in core.
    _hold_mult_cache: dict[Any, tuple[float, float]] = {}
    # Companion cache: the resolver's per-date diagnostics (``error_codes``) for a
    # hold-mode option input, keyed the same way and populated during the SAME
    # ``fetch``.  Read by the portfolio all-NaN error path via
    # ``fetch_hold_diagnostics`` so a failed hold resolve can name the dominant
    # cause without a second resolve.
    _hold_diag_cache: dict[Any, list[str | None]] = {}
    # Companion cache: the resolver's per-date close→mid fallback markers for a
    # hold-mode option input, keyed the same way and populated during the SAME
    # ``fetch``.  ``(dates, close_mid_fallback, roll_premium_fallback)`` — 0.0/1.0
    # float arrays aligned to ``dates`` marking where a false-zero/NULL settlement
    # was replaced by the row mid (daily value series / roll-day open premium).
    # Read by the portfolio trade-log path via ``fetch_hold_close_fallback`` so the
    # roll rows can flag WHERE the fallback fired.  Purely diagnostic.
    _hold_fallback_cache: dict[
        Any,
        tuple[npt.NDArray[np.int64], npt.NDArray[np.float64], npt.NDArray[np.float64]],
    ] = {}

    # Companion cache: the interior roll-boundary dates of a CONTINUOUS input,
    # keyed by its roll axes, populated during the SAME ``fetch`` (which already
    # calls ``get_continuous`` and discards ``roll_dates``).  Read by
    # ``fetch_continuous_roll_info`` so the signal cost overlay can charge a roll
    # round-trip at each boundary (parity with the portfolio engine).
    _continuous_roll_cache: dict[Any, tuple[int, ...]] = {}

    # Module-level ``_hold_cache_key`` is the single source of truth (also unit-
    # tested directly for the Sign-4 collision guarantee).
    _hold_key = _hold_cache_key
    _continuous_key = _continuous_cache_key

    async def fetch(
        instrument: InputInstrument, field: str
    ) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
        if isinstance(instrument, InstrumentSpot):
            try:
                series = await svc.get_prices(
                    instrument.collection,
                    instrument.instrument_id,
                    start=start,
                    end=end,
                )
            except DataNotFoundError as exc:
                raise SignalDataError(
                    f"instrument {instrument.collection}/"
                    f"{instrument.instrument_id}: {exc}"
                ) from exc
            if series is None:
                raise SignalDataError(
                    f"instrument '{instrument.instrument_id}' not found in "
                    f"collection '{instrument.collection}'"
                )
            values = _pick_field(series, field)
            return series.dates, values

        if isinstance(instrument, InstrumentOptionStream):
            # Lazy imports — keeps the engine/options dependency
            # function-scoped (same pattern as _options_materialise).
            from tcg.core.api._options_wiring import build_stream_resolver_wiring
            from tcg.core.api._options_materialise import _business_dates_in_range
            from tcg.data._utils import date_to_int
            from tcg.engine.options.series.stream_resolver import (
                resolve_option_stream,
            )

            trade_dates = _business_dates_in_range(start, end)
            if not trade_dates:
                raise SignalDataError("option_stream requires explicit start/end dates")

            # Build wiring once per signal evaluation, capture in closure.  Pass the
            # fetcher window so the futures adapter memoizes the underlying (one
            # ranged fetch per distinct future, not per trade date — the Phase-C
            # N+1).  All option legs share this window, so the cache is reused
            # across legs; result-invariant.
            if "wiring" not in _os_wiring_cache:
                _os_wiring_cache["wiring"] = build_stream_resolver_wiring(
                    svc,
                    underlying_prefetch_window=(trade_dates[0], trade_dates[-1]),
                    use_chain_cache=use_chain_cache,
                )
            chain_reader, mat_resolver, ul_resolver, bulk_reader = _os_wiring_cache[
                "wiring"
            ]

            # Shared process-wide dwh-pool gate: a basket with several option
            # legs resolves them in turn HERE, but the Data page fires the
            # composite + per-leg series concurrently (and other panels/requests
            # may overlap), all sharing the ONE 4-slot pool.  The gate bounds the
            # SUM across all concurrent resolves so the pool is not exhausted
            # (the OPT_SP_500 basket PoolTimeout).  See _options_concurrency.
            from tcg.core.api._options_concurrency import get_dwh_concurrency_gate

            gate = get_dwh_concurrency_gate()

            # Pre-fetch available expirations filtered by type + cycle.
            # ``expand_cycle`` broadens the "Monthly" filter ('M') to the full
            # 3rd-Friday series ({'M','W3 Friday'}) so a ~2mo delta-selected option
            # tracks the real monthly across ALL eras (later years tag the monthly
            # 'W3 Friday' and leave 'M' for quarterlies).  Every other cycle passes
            # through unchanged.  The SAME expanded value feeds the expiration list
            # AND the chain fetch so the two never disagree.
            _cycle = expand_cycle(instrument.cycle)
            all_expirations = await svc.list_option_expirations_filtered(
                instrument.collection,
                option_type=instrument.option_type,
                cycle=_cycle,
            )

            # Issue #2 fix: for NearestToTarget, fetch the per-date LISTED
            # expiration map (one distinct scan over the window) so the resolver
            # snaps to an expiration actually quoted on each trade date instead of
            # the whole-window global nearest.  ONE shared helper (also used by
            # materialise_option_streams) returns None for every other maturity
            # rule (arithmetic rules snap via _snap_to_listed already) and bounds
            # the scan by expiration_max, so the LEAPS scan can't blow up.
            from tcg.core.api._options_materialise import (
                fetch_nearest_target_expirations_by_date,
            )

            available_by_date = await fetch_nearest_target_expirations_by_date(
                svc=svc,
                maturity=instrument.maturity,
                collection=instrument.collection,
                option_type=instrument.option_type,
                cycle=_cycle,
                trade_dates=trade_dates,
            )

            # Select-and-hold P&L for delta/moneyness-selected option signals:
            # freeze the contract between rolls and book fixed-contract dollar P&L
            # (default False = daily-reselect mid LEVEL).  In hold mode capture the
            # resolver's roll structure (is_roll + roll_premium) into the per-signal
            # cache so signal_exec's ``fetch_hold_roll_info`` reads it without a
            # second resolve.  Contracts are bound to ``_`` (rolls are visualised
            # via the options router, not the signals path).
            roll_info_out: dict[str, Any] | None = (
                {} if instrument.hold_between_rolls else None
            )
            # Futures-notional sizing: build the per-roll reference-future price
            # resolver (OPT_→FUT_ by name).  Only in hold + futures_notional mode;
            # premium_notional passes None → resolver never touches futures data →
            # byte-identical to the shipped path.
            futures_ref_resolver = None
            if (
                instrument.hold_between_rolls
                and instrument.sizing_mode == "futures_notional"
            ):
                from tcg.core.api._options_wiring import (
                    build_futures_reference_resolver,
                )

                futures_ref_resolver = build_futures_reference_resolver(
                    svc,
                    option_collection=instrument.collection,
                    futures_reference=instrument.futures_reference,
                    prefetch_window=(trade_dates[0], trade_dates[-1]),
                )
            try:
                values, diagnostics, _contracts = await resolve_option_stream(
                    dates=trade_dates,
                    collection=instrument.collection,
                    option_type=instrument.option_type,
                    cycle=_cycle,
                    maturity=instrument.maturity,
                    selection=instrument.selection,
                    stream=instrument.stream,
                    roll_offset=instrument.roll_offset,
                    chain_reader=chain_reader,
                    maturity_resolver=mat_resolver,
                    underlying_price_resolver=ul_resolver,
                    bulk_chain_reader=bulk_reader,
                    available_expirations=all_expirations,
                    available_expirations_by_date=available_by_date,
                    concurrency_gate=gate,
                    hold_between_rolls=instrument.hold_between_rolls,
                    hold_roll_info_out=roll_info_out,
                    futures_reference_resolver=futures_ref_resolver,
                )
            except NotImplementedError as exc:
                # continuous_front is not yet wired — surface a LOUD request-time
                # error rather than mis-size off a missing reference.  (nearest_abs
                # and nearest_on_or_after ARE wired; only continuous_front raises.)
                raise SignalValidationError(str(exc)) from exc

            dates_arr = np.array([date_to_int(d) for d in trade_dates], dtype=np.int64)
            if diag_sink is not None:
                # Per-leg coverage record for the Data-page basket path.  The
                # descriptor names the leg in the UI; error_codes/dates let the
                # caller aggregate a dominant-cause summary + gap date ranges.
                sel = instrument.selection
                descriptor = (
                    f"{instrument.collection} {instrument.option_type} "
                    f"{type(sel).__name__}"
                )
                diag_sink.append(
                    {
                        "descriptor": descriptor,
                        "dates": dates_arr,
                        "error_codes": diagnostics,
                    }
                )
            if roll_info_out is not None:
                key = _hold_key(instrument)
                # 3→4-tuple ripple (Guardrail Sign 4): carry roll_future_ref (NaN
                # array in premium mode where the resolver populated nothing).
                _roll_fref = roll_info_out.get("roll_future_ref")
                _hold_roll_info_cache[key] = (
                    dates_arr,
                    roll_info_out["is_roll"],
                    roll_info_out["roll_premium"],
                    _roll_fref,
                )
                # Companion: stash the per-date diagnostics so the portfolio hold
                # path can name the dominant cause on an all-NaN resolve.
                _hold_diag_cache[key] = diagnostics
                # Companion: stash the close→mid fallback markers so the portfolio
                # trade-log path can flag WHERE a false-zero/NULL settlement was
                # replaced by the row mid.  Defensive zeros if the resolver omitted
                # them (only populated on the hold path).
                _n = len(dates_arr)
                _close_fb = roll_info_out.get("close_mid_fallback")
                _roll_prem_fb = roll_info_out.get("roll_premium_fallback")
                _hold_fallback_cache[key] = (
                    dates_arr,
                    np.asarray(
                        _close_fb
                        if _close_fb is not None
                        else np.zeros(_n, dtype=np.float64),
                        dtype=np.float64,
                    ),
                    np.asarray(
                        _roll_prem_fb
                        if _roll_prem_fb is not None
                        else np.zeros(_n, dtype=np.float64),
                        dtype=np.float64,
                    ),
                )
                # Futures-notional: resolve the per-root multipliers (live-first,
                # config fallback; never a silent 1.0) and cache them for the
                # side-channel.  Both live hints come from the resolver out-dict:
                # ``mult_opt_live`` = the first held OPTION contract's contract_size;
                # ``mult_fut_live`` = the first reference FUTURE's contract_size
                # (fetched together with its price — no extra round-trip).  Live
                # wins; the signed-off config is the fallback for a NULL live value.
                if instrument.sizing_mode == "futures_notional":
                    from tcg.types.multipliers import (
                        resolve_multipliers,
                        root_from_collection,
                    )

                    def _live_hint(arr: Any) -> float | None:
                        if arr is not None and len(arr) and np.isfinite(arr[0]):
                            return float(arr[0])
                        return None

                    _live_opt = _live_hint(roll_info_out.get("mult_opt_live"))
                    _live_fut = _live_hint(roll_info_out.get("mult_fut_live"))
                    _res = resolve_multipliers(
                        root_from_collection(instrument.collection),
                        live_m_fut=_live_fut,
                        live_m_opt=_live_opt,
                    )
                    if _res.diagnostic is not None:
                        logger.info(
                            "futures-notional multipliers for %s: %s",
                            instrument.collection,
                            _res.diagnostic,
                        )
                    _hold_mult_cache[key] = (_res.m_fut, _res.m_opt)
            return dates_arr, values

        if isinstance(instrument, InstrumentBasket):
            # Weighted combination of leg series.  Each leg is a typed
            # leaf instrument paired with its weight; we recurse into
            # ``fetch`` for the leg's instrument so spot / continuous /
            # option_stream legs all reuse the existing per-type
            # resolvers without any duplicated logic.
            #
            # ``basket_desc`` is what appears in error messages — saved
            # baskets identify themselves by their persisted id; inline
            # baskets have no id, so they self-identify by asset class.
            basket_desc = (
                repr(instrument.basket_id)
                if instrument.basket_id is not None
                else f"inline[{instrument.asset_class}]"
            )
            weighted_dates: npt.NDArray[np.int64] | None = None
            weighted_values: npt.NDArray[np.float64] | None = None

            for leg_index, (leg_inst, leg_weight_raw) in enumerate(instrument.legs):
                leg_weight = float(leg_weight_raw)
                try:
                    leg_dates, leg_values = await fetch(leg_inst, field)
                except SignalDataError as exc:
                    # Re-raise with a basket-leg-prefixed message so
                    # downstream error envelopes carry leg context.
                    raise SignalDataError(
                        f"basket {basket_desc} leg {leg_index}: {exc}"
                    ) from exc

                if weighted_dates is None:
                    weighted_dates = leg_dates
                    weighted_values = leg_weight * leg_values
                else:
                    common, idx_a, idx_b = np.intersect1d(
                        weighted_dates,
                        leg_dates,
                        assume_unique=True,
                        return_indices=True,
                    )
                    if common.size == 0:
                        raise SignalDataError(
                            f"basket {basket_desc}: no overlapping dates between legs"
                        )
                    weighted_dates = common
                    assert weighted_values is not None
                    weighted_values = (
                        weighted_values[idx_a] + leg_weight * leg_values[idx_b]
                    )

            if weighted_dates is None or weighted_values is None:
                raise SignalDataError(f"basket {basket_desc} has no legs")
            return weighted_dates, weighted_values

        # continuous
        try:
            roll_config = build_roll_config(
                instrument.adjustment,
                instrument.cycle,
                instrument.roll_offset,
                strategy=instrument.strategy,
            )
        except ValueError as exc:
            raise SignalValidationError(f"continuous input: {exc}") from exc
        try:
            cseries = await svc.get_continuous(
                instrument.collection,
                roll_config,
                start=start,
                end=end,
            )
        except DataNotFoundError as exc:
            raise SignalDataError(f"continuous {instrument.collection}: {exc}") from exc
        if cseries is None:
            raise SignalDataError(
                f"continuous series unavailable for {instrument.collection!r}"
            )
        # Capture the interior roll boundaries (otherwise discarded) so the signal
        # cost overlay can charge a roll round-trip at each — parity with the
        # portfolio engine.  Idempotent across ``field`` re-fetches of the same
        # instrument; does NOT change the return value (byte-neutral).
        _continuous_roll_cache[_continuous_key(instrument)] = tuple(
            int(d) for d in cseries.roll_dates
        )
        values = _pick_field(cseries.prices, field)
        return cseries.prices.dates, values

    async def fetch_hold_roll_info(
        instrument: InstrumentOptionStream,
    ) -> tuple[
        npt.NDArray[np.int64],
        npt.NDArray[np.float64],
        npt.NDArray[np.float64],
        "npt.NDArray[np.float64] | None",
    ]:
        """Return the hold-mode roll structure for ``signal_exec``'s dollar-P&L path.

        ``(dates, is_roll, roll_premium, roll_future_ref)`` — populated during the
        normal ``fetch`` of this hold-mode option input (which runs first, so the
        cache is warm).  ``roll_future_ref`` is a NaN array in premium_notional mode
        (the resolver populates it only in futures_notional mode).  Falls back to a
        fresh resolve if, defensively, the cache is cold.
        """
        key = _hold_key(instrument)
        cached = _hold_roll_info_cache.get(key)
        if cached is not None:
            return cached
        # Cold cache (defensive): resolve once to populate it, then return.  Reuses
        # the same code path as ``fetch`` so the wiring / gate / window match.
        await fetch(instrument, "close")
        cached = _hold_roll_info_cache.get(key)
        if cached is None:  # pragma: no cover (only if instrument is not hold-mode)
            raise SignalDataError(
                "fetch_hold_roll_info called for a non-hold option instrument"
            )
        return cached

    async def fetch_hold_multipliers(
        instrument: InstrumentOptionStream,
    ) -> tuple[float, float]:
        """Return the resolved ``(m_fut, m_opt)`` for a futures-notional hold input.

        Live-first / signed-off-config fallback (:mod:`tcg.types.multipliers`),
        resolved in core (the engine never reads dwh) during the normal ``fetch``.
        A NaN pair (root with neither live nor config) is returned verbatim so the
        engine applies the tail carry-forward — NEVER a silent 1.0.
        """
        key = _hold_key(instrument)
        cached = _hold_mult_cache.get(key)
        if cached is not None:
            return cached
        await fetch(instrument, "close")
        cached = _hold_mult_cache.get(key)
        if cached is None:
            # Defensive: a non-futures-notional / non-hold instrument has no
            # multipliers — return a NaN pair (engine tail carry-forward).
            return (float("nan"), float("nan"))
        return cached

    async def fetch_hold_diagnostics(
        instrument: InstrumentOptionStream,
    ) -> list[str | None] | None:
        """Return the cached per-date resolver diagnostics for a hold-mode option
        input (``error_codes``: ``missing_delta_no_compute`` / ``missing_mid`` /
        ``no_chain_for_date`` / …), or ``None`` if this input was never fetched in
        hold mode.  Populated during the normal ``fetch`` of the hold input (which
        runs first, so the cache is warm).  Purely diagnostic — never resolves."""
        return _hold_diag_cache.get(_hold_key(instrument))

    async def fetch_hold_close_fallback(
        instrument: InstrumentOptionStream,
    ) -> (
        tuple[npt.NDArray[np.int64], npt.NDArray[np.float64], npt.NDArray[np.float64]]
        | None
    ):
        """Return the cached close→mid fallback markers for a hold-mode option
        input, or ``None`` if this input was never fetched in hold mode.

        ``(dates, close_mid_fallback, roll_premium_fallback)`` — 0.0/1.0 float
        arrays aligned to ``dates`` marking where a false-zero/NULL ``close``
        settlement was replaced by the row mid (daily value series / roll-day open
        premium respectively).  Populated during the normal ``fetch`` of the hold
        input (which runs first, so the cache is warm).  Purely diagnostic — never
        resolves."""
        return _hold_fallback_cache.get(_hold_key(instrument))

    async def fetch_continuous_roll_info(
        instrument: InstrumentContinuous,
    ) -> npt.NDArray[np.int64]:
        """Return the interior roll-boundary dates (YYYYMMDD) of a continuous input.

        Populated during the normal ``fetch`` of the continuous input (its close
        operand is resolved first, warming the cache) — so no extra ``get_continuous``
        is issued.  The signal cost overlay reads this to charge a roll round-trip at
        each boundary.  Falls back to a fresh resolve if, defensively, the cache is
        cold.  Mirrors ``fetch_hold_roll_info``.
        """
        key = _continuous_key(instrument)
        cached = _continuous_roll_cache.get(key)
        if cached is None:
            # Cold cache (defensive): resolve once to populate it via the same
            # ``fetch`` path, then read back.
            await fetch(instrument, "close")
            cached = _continuous_roll_cache.get(key)
        if cached is None:  # pragma: no cover (only if instrument is not continuous)
            return np.array([], dtype=np.int64)
        return np.asarray(cached, dtype=np.int64)

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
    fetch.fetch_continuous_roll_info = fetch_continuous_roll_info  # type: ignore[attr-defined]
    fetch.fetch_hold_diagnostics = fetch_hold_diagnostics  # type: ignore[attr-defined]
    fetch.fetch_hold_multipliers = fetch_hold_multipliers  # type: ignore[attr-defined]
    fetch.fetch_hold_close_fallback = fetch_hold_close_fallback  # type: ignore[attr-defined]
    return fetch
