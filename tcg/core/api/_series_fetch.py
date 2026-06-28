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
from tcg.types.signal import (
    InputInstrument,
    InstrumentBasket,
    InstrumentContinuous,
    InstrumentOptionStream,
    InstrumentSpot,
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
        from tcg.core.api.options import (
            _criterion_pydantic_to_dataclass,
            _maturity_pydantic_to_dataclass,
            _roll_schedule_pydantic_to_dataclass,
        )

        maturity = _maturity_pydantic_to_dataclass(instrument_ref.maturity)
        selection = _criterion_pydantic_to_dataclass(instrument_ref.selection)
        return InstrumentOptionStream(
            collection=instrument_ref.collection,
            option_type=instrument_ref.option_type,
            cycle=instrument_ref.cycle,
            maturity=maturity,
            selection=selection,
            stream=instrument_ref.stream,
            roll_offset=int(instrument_ref.roll_offset),
            roll_schedule=_roll_schedule_pydantic_to_dataclass(
                instrument_ref.roll_schedule
            ),
        )
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

        all_expirations = await svc.list_option_expirations_filtered(
            inst.collection,
            option_type=inst.option_type,
            cycle=inst.cycle,
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
) -> Any:
    # Lazy-init cache for option_stream wiring — built once on first
    # option_stream fetch, then reused for all subsequent option_stream
    # inputs within this signal evaluation.
    _os_wiring_cache: dict[str, Any] = {}

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

            # Build wiring once per signal evaluation, capture in closure.
            if "wiring" not in _os_wiring_cache:
                _os_wiring_cache["wiring"] = build_stream_resolver_wiring(svc)
            chain_reader, mat_resolver, ul_resolver, bulk_reader = _os_wiring_cache[
                "wiring"
            ]

            # Pre-fetch available expirations filtered by type + cycle.
            all_expirations = await svc.list_option_expirations_filtered(
                instrument.collection,
                option_type=instrument.option_type,
                cycle=instrument.cycle,
            )

            # Signals path: rolls are not consumed here (PR scope is
            # visualisation only — Phase 1).  Bind contracts to ``_``
            # but do NOT drop the signature change; 3-tuple is canonical.
            values, diagnostics, _contracts = await resolve_option_stream(
                dates=trade_dates,
                collection=instrument.collection,
                option_type=instrument.option_type,
                cycle=instrument.cycle,
                maturity=instrument.maturity,
                selection=instrument.selection,
                stream=instrument.stream,
                roll_offset=instrument.roll_offset,
                roll_schedule=instrument.roll_schedule,
                chain_reader=chain_reader,
                maturity_resolver=mat_resolver,
                underlying_price_resolver=ul_resolver,
                bulk_chain_reader=bulk_reader,
                available_expirations=all_expirations,
            )

            dates_arr = np.array([date_to_int(d) for d in trade_dates], dtype=np.int64)
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
        values = _pick_field(cseries.prices, field)
        return cseries.prices.dates, values

    return fetch
