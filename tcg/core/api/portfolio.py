"""Portfolio router -- weighted portfolio computation endpoint."""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Callable, Literal

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import (
    OptionStreamLabel,
    OptionStreamRef,
    _validate_nav_times,
)
from tcg.core.api._models_options import MaturityRule, RollOffset, SelectionCriterion
from tcg.core.api._options_materialise import materialise_option_streams
from tcg.core.api._serializers import nan_safe_floats, sanitize_json_floats
from tcg.core.api.common import get_market_data
from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.api.signals import (
    IndicatorSpecIn,
    SignalIn,
    _resolve_basket_inputs,
    compute_input_overlap,
    make_signal_fetcher,
    parse_signal,
)
from tcg.data._utils import date_to_int, int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.persistence import WriteRepository
from tcg.engine import (
    aggregate_returns,
    compute_metrics,
    compute_weighted_portfolio,
)
from tcg.engine.hold_pnl import _HoldPnLSpec, _compound_with_hold
from tcg.engine.signal_exec import (
    IndicatorSpecInput,
    SignalDataError,
    SignalRuntimeError,
    SignalValidationError,
    evaluate_signal,
)
from tcg.types.errors import ValidationError
from tcg.types.options import expand_cycle
from tcg.types.market import (
    AdjustmentMethod,
    AssetClass,
    ContinuousLegSpec,
    ContinuousRollConfig,
    InstrumentId,
    RollStrategy,
)
from tcg.types.multipliers import resolve_multipliers, root_from_collection
from tcg.types.portfolio import RebalanceFreq
from tcg.types.signal import (
    InstrumentContinuous,
    InstrumentOptionStream,
    InstrumentSpot,
    Trade,
)


logger = logging.getLogger(__name__)


def _signal_input_underlying_id(instrument: object) -> str | None:
    """Resolve a signal Input's bound instrument to the underlying instrument
    identifier used elsewhere in the portfolio response.

    Mirrors the direct-leg conventions:
      * spot       → ``instrument_id`` (matches ``LegSpec.symbol``)
      * continuous → ``collection``    (matches ``LegSpec.collection``)
      * option_stream → ``collection``

    Returns ``None`` for unknown instrument variants so the caller can fall
    back to the signal-local input id rather than crash.
    """
    if isinstance(instrument, InstrumentSpot):
        return instrument.instrument_id
    if isinstance(instrument, InstrumentContinuous):
        return instrument.collection
    if isinstance(instrument, InstrumentOptionStream):
        return instrument.collection
    return None


def _signal_input_collection(instrument: object) -> str | None:
    """Resolve a signal Input's bound instrument to its dwh COLLECTION.

    Unlike ``_signal_input_underlying_id`` (which returns the SPOT
    ``instrument_id``), this always returns the ``collection`` so the
    trade-log sizing can key the FUT_/OPT_ multiplier rule off the
    collection PREFIX regardless of the instrument variant.  A spot input's
    ``input_id`` is its symbol (no FUT_/OPT_ prefix) — reading the prefix off
    the ``input_id`` alone would silently mis-classify a mis-mapped futures
    leg as ``shares``; keying off the true collection avoids that (Sign: NEVER
    a silent shares/1.0 for a FUT/OPT leg).  Returns ``None`` for unknown
    variants so the caller nulls the quantity.
    """
    if isinstance(
        instrument, (InstrumentSpot, InstrumentContinuous, InstrumentOptionStream)
    ):
        return instrument.collection
    return None


def _leg_multiplier_and_unit(collection: str | None) -> tuple[float | None, str]:
    """Resolve ``(M, quantity_unit)`` for a trade's collection.

    - ``FUT_*`` → ``m_fut``; ``OPT_*`` → ``m_opt`` (via the PR#77
      ``resolve_multipliers`` machinery). CONFIG-ONLY at this seam: no live
      ``dim_instrument.contract_size`` read is issued here (it would be a new
      per-trade DB call on the hot compute path); the signed-off fallback table
      is authoritative and an unknown root yields ``NaN`` → ``M = None`` so the
      quantity is nulled — NEVER a silent ``1.0`` for a FUT/OPT leg.
    - anything else (spot / equity / index, or an unknown/None collection) →
      ``M = 1.0``, unit ``"shares"``.  Unit is still reported for a FUT/OPT leg
      with an unresolved multiplier (``M = None``) so the FE can label it.
    """
    if collection is not None and collection.startswith(("FUT_", "OPT_")):
        res = resolve_multipliers(root_from_collection(collection))
        m = res.m_fut if collection.startswith("FUT_") else res.m_opt
        usable = float(m) if math.isfinite(m) and m > 0.0 else None
        return usable, "contracts"
    return 1.0, "shares"


def _roll_row_quantity(
    leg_fraction: float,
    equity: npt.NDArray[np.float64],
    price_series: npt.NDArray[np.float64] | None,
    open_bar: int,
    m: float | None,
    n_bars: int,
) -> float | None:
    """Fractional CONTRACT count for a roll row's OPEN bar.

    REUSES the §10.5 formula VERBATIM (``|signed_weight|·NAV_open/(price_open·M)``)
    and its NaN/null guards so a roll row is sized exactly like the direct-leg row
    it replaces — a missing/≤0 price, an unresolved FUT/OPT ``M``, or a non-finite
    NAV yields ``None`` (the FE falls back to the % display; NEVER a silent 1.0).
    ``price_series`` is the leg's OWN price series aligned to ``common_dates`` (the
    continuous adjusted close, or the option premium the hold accumulator was fed)
    — NOT ``price_by_input`` (which is the option leg's synthetic equity, wrong for
    an option quantity; this is why direct-option rows are sized here, not in §10.5).
    """
    price_open = (
        price_series[open_bar]
        if price_series is not None and 0 <= open_bar < len(price_series)
        else None
    )
    nav_open = float(equity[open_bar]) if 0 <= open_bar < n_bars else math.nan
    if (
        m is None
        or price_open is None
        or not math.isfinite(float(price_open))
        or float(price_open) <= 0.0
        or not math.isfinite(nav_open)
    ):
        return None
    return abs(leg_fraction) * nav_open / (float(price_open) * m)


def _roll_row_pnl(
    quantity: float | None,
    leg_fraction: float,
    price_series: npt.NDArray[np.float64] | None,
    open_bar: int,
    close_bar: int,
    m: float | None,
) -> float | None:
    """DISPLAY-ONLY realised P&L for a roll segment = ``sign·qty·Δprice·M``.

    ``Δprice`` = price at ``close_bar`` − price at ``open_bar`` on the leg's own
    price series; ``sign`` is the leg direction (long +, short −).  Returns ``None``
    when the quantity/multiplier is unusable or either bar's price is non-finite —
    ``sanitize_json_floats`` nulls any residual.  NEVER feeds equity or metrics.
    """
    if quantity is None or m is None or price_series is None:
        return None
    n = len(price_series)
    if not (0 <= open_bar < n and 0 <= close_bar < n):
        return None
    p_open = float(price_series[open_bar])
    p_close = float(price_series[close_bar])
    if not (math.isfinite(p_open) and math.isfinite(p_close)):
        return None
    dir_sign = 1.0 if leg_fraction >= 0 else -1.0
    return dir_sign * quantity * (p_close - p_open) * m


def _synthetic_segment_pnl(
    synthetic: npt.NDArray[np.float64] | None,
    equity: npt.NDArray[np.float64],
    leg_fraction: float,
    open_bar: int,
    close_boundary: int,
    n_bars: int,
) -> float | None:
    """DISPLAY-ONLY realised P&L for an OPTION hold segment, in the SAME weight/NAV-
    scaled dollar unit as the continuous-futures rows in the same column.

        segment_pnl = |leg_fraction| · NAV_open · (synthetic[close_boundary] /
                                                   synthetic[open_bar] − 1)

    * ``NAV_open`` = the PORTFOLIO equity at the segment open (``equity[open_bar]``),
      the SAME NAV the continuous path / :func:`_roll_row_quantity` deploy — NOT the
      leg synthetic — so an option leg's P&L respects its portfolio weight and is
      comparable to the continuous rows.
    * ``synthetic`` = the leg's aligned synthetic (``100·equity_ratio``); its ratio
      ``synthetic[close_boundary]/synthetic[open_bar]`` is the leg's segment RETURN,
      which already bakes in DIRECTION (so ``|leg_fraction|`` is used, not the signed
      weight — multiplying by the signed weight would re-introduce the inversion).
    * ``close_boundary`` is the NEXT segment's open bar (the roll bar, whose step
      books the OLD contract's final move) — or the last bar for the final segment —
      so, for a SINGLE full-weight leg (``|w|=1``, ``NAV_open == synthetic[open]``),
      this COLLAPSES to ``synthetic[close_boundary] − synthetic[open_bar]`` and the
      segments TELESCOPE to the leg's total equity change.

    Mirrors the continuous ``leg_fraction·NAV_open·(price return)``.  NaN-safe (the
    accumulator books 0 where the premium is missing).  A leg WIPED to ``synthetic
    ≤ 0`` at the segment open makes the return undefined → ``None`` (the FE shows
    em-dash — an honest "no return to show" rather than a fake 0).
    """
    if synthetic is None:
        return None
    n = len(synthetic)
    if not (0 <= open_bar < n and 0 <= close_boundary < n):
        return None
    s_open = float(synthetic[open_bar])
    s_close = float(synthetic[close_boundary])
    nav_open = float(equity[open_bar]) if 0 <= open_bar < n_bars else math.nan
    if not (
        math.isfinite(s_open) and math.isfinite(s_close) and math.isfinite(nav_open)
    ):
        return None
    if s_open <= 0.0:
        return None
    result = abs(leg_fraction) * nav_open * (s_close / s_open - 1.0)
    # Belt-and-suspenders: a denormal-but-positive s_open could overflow the ratio
    # to ±inf (unserializable). Unreachable today (the accumulator books an exact 0
    # on wipe, caught by the s_open<=0 guard), but null any non-finite result so a
    # future accumulator change can never surface an inf P&L.
    return result if math.isfinite(result) else None


def _build_roll_rows(
    *,
    label: str,
    input_id: str,
    collection: str | None,
    leg_fraction: float,
    direction: str,
    interior_roll_dates: list[int],
    price_series: npt.NDArray[np.float64] | None,
    equity: npt.NDArray[np.float64],
    cd_index: dict[int, int],
    n_bars: int,
    sizing_price_series: npt.NDArray[np.float64] | None = None,
    sizing_multiplier: float | None = None,
    use_futures_notional: bool = False,
    pnl_series: npt.NDArray[np.float64] | None = None,
) -> list[dict]:
    """One display-only trade row per HELD CONTRACT of a rolling direct leg.

    Applies to continuous-futures legs and hold-mode option-stream legs only.
    ``interior_roll_dates`` are the YYYYMMDD roll BOUNDARIES (the initial open is
    NOT a boundary — continuous ``ContinuousSeries.roll_dates`` already excludes it
    and the option path drops the first ``is_roll`` date).  Each boundary is mapped
    to a ``common_dates`` bar via ``cd_index`` and DROPPED if outside the window
    (same remap-drop semantics as signal trades).  A leg with N held contracts
    (N−1 in-window boundaries) yields N rows: row0 entry ``open``; the last row exit
    ``end``; every other boundary ``rolling`` (decision 1).  The rows are PURELY
    INFORMATIONAL — they are appended to ``aggregated_trades`` AFTER equity/metrics
    are computed and never feed back into them.

    TWO segment-P&L bases, selected by ``pnl_series``:

    * CONTINUOUS legs (``pnl_series is None``) — the count is the own-price notional
      ``|w|·NAV_open/(price_open·M)`` and the segment P&L is ``sign·qty·Δclose·M`` off
      the leg's adjusted close (finite), exactly as before (unchanged).
    * OPTION hold legs (``pnl_series`` = the leg's aligned synthetic equity) — the
      daily held premium is mostly NaN for a far-OTM option, so BOTH bases move off
      it: the segment P&L is the accumulator-derived, weight/NAV-scaled dollar amount
      ``|w|·NAV_open·(synthetic return)`` (:func:`_synthetic_segment_pnl`; NaN-safe,
      correctly signed, SAME unit + weight scaling as the continuous rows, and for a
      single full-weight leg it collapses to the leg equity change) and the COUNT is
      sized off ``sizing_price_series`` — the
      FUTURES notional ``|w|·NAV/(F_ref·m_fut)`` (``sizing_multiplier`` = m_fut) when
      ``use_futures_notional``, else the roll-day PREMIUM notional
      ``|w|·NAV/(roll_premium·M)`` (both finite at the segment opens, which are roll
      bars).  All the same NaN/≤0 guards apply, so an unrecoverable price/multiplier
      nulls the count (the FE shows em-dash) — NEVER a silent daily-premium fallback.

    This is DISPLAY-ONLY and never perturbs equity/metrics.
    """
    option_mode = pnl_series is not None
    m, unit = _leg_multiplier_and_unit(collection)
    # Roll boundaries → distinct in-window bars in (0, n_bars-1]; a boundary at
    # bar 0 (== window start) or out of range collapses into the first segment.
    roll_bars = sorted(
        {
            b
            for d in interior_roll_dates
            if (b := cd_index.get(int(d))) is not None and 1 <= b <= n_bars - 1
        }
    )
    opens = [0, *roll_bars]
    closes = [*(b - 1 for b in roll_bars), n_bars - 1]
    n_seg = len(opens)
    hover = f"rolling {collection or label}"
    rows: list[dict] = []
    for k in range(n_seg):
        open_bar = opens[k]
        close_bar = closes[k]
        entry_name = "open" if k == 0 else "rolling"
        exit_name = "end" if k == n_seg - 1 else "rolling"
        if option_mode:
            # OPTION hold leg: count off the sizing series (futures-notional F_ref·m_fut
            # when requested, else the roll-day premium notional roll_premium·M — both
            # finite at the segment opens, which are roll bars); the daily held premium
            # is NaN there for a far-OTM option.  ``sizing_multiplier`` carries m_fut in
            # futures mode; premium mode reuses the leg's own M.
            count_mult = sizing_multiplier if use_futures_notional else m
            quantity = _roll_row_quantity(
                leg_fraction, equity, sizing_price_series, open_bar, count_mult, n_bars
            )
            # Segment P&L from the accumulator equity (NaN-safe + correctly signed):
            # synthetic[close_boundary] − synthetic[open_bar], where the close boundary
            # is the NEXT segment's open bar (the roll bar whose step books the OLD
            # contract's final move) so segments telescope to the leg equity change.
            close_boundary = opens[k + 1] if k < n_seg - 1 else n_bars - 1
            segment_pnl = _synthetic_segment_pnl(
                pnl_series, equity, leg_fraction, open_bar, close_boundary, n_bars
            )
        else:
            # CONTINUOUS leg (unchanged): count off the leg's own adjusted close, and
            # the segment P&L is sign·qty·Δclose·M on that (finite) close series.
            quantity = _roll_row_quantity(
                leg_fraction, equity, price_series, open_bar, m, n_bars
            )
            segment_pnl = _roll_row_pnl(
                quantity, leg_fraction, price_series, open_bar, close_bar, m
            )
        rows.append(
            {
                "input_id": input_id,
                "entry_block_id": f"roll:{label}",
                "entry_block_name": entry_name,
                "exit_block_id": f"roll:{label}",
                "exit_block_name": exit_name,
                "open_bar": open_bar,
                "close_bar": close_bar,
                "direction": direction,
                "signed_weight": leg_fraction,
                "holding_id": label,
                "holding_name": label,
                "quantity_unit": unit,
                "multiplier": m,
                "quantity": quantity,
                "segment_pnl": segment_pnl,
                "roll_hover": hover,
                "_roll_row": True,
            }
        )
    return rows


@dataclass(frozen=True)
class _SignalLegEvalResult:
    """Internal aggregate of what a signal leg produces for the portfolio.

    ``index`` and ``synthetic`` keep the existing aggregation contract;
    ``trades`` and ``positions_payload`` are bubbled up for the trade log.
    Each entry in ``positions_payload`` mirrors the signals-API positions
    shape: ``{input_id, price: {label, values} | None}``.
    """

    index: npt.NDArray[np.int64]
    synthetic: npt.NDArray[np.float64]
    trades: tuple[Trade, ...] = ()
    positions_payload: tuple[dict, ...] = ()
    # Trade ``input_id`` (already remapped to the underlying) → its dwh
    # collection, so the portfolio trade-log sizing can resolve the FUT/OPT
    # multiplier off the collection PREFIX (``input_id`` alone is a bare symbol
    # for spot inputs). Empty for a leg whose inputs have no known collection.
    collection_by_input: dict[str, str] = field(default_factory=dict)


router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_collection_classifier(request: Request) -> Callable[[str], AssetClass | None]:
    """Dependency: a function mapping a collection name → its ``AssetClass``.

    Replaces the old ``CollectionRegistry`` injection. The dwh-backed service
    exposes the same prefix-based classification via
    ``DefaultMarketDataService.asset_class_for`` (pure, no DB hit); we hand the
    bound method out so ``_parse_legs`` stays storage-agnostic.
    """
    return request.app.state.market_data.asset_class_for


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class SignalLegSpec(BaseModel):
    """Full signal definition embedded in a portfolio leg."""

    spec: SignalIn
    indicators: list[IndicatorSpecIn] = Field(default_factory=list)


class LegSpec(BaseModel):
    type: str  # "instrument", "continuous", "signal", or "option_stream"
    collection: str | None = (
        None  # Required for "instrument"/"continuous"/"option_stream"
    )
    symbol: str | None = None  # Required for "instrument"
    strategy: str | None = None  # Required for "continuous"
    # Roll back-adjustment of the rolled series — "continuous" (futures) ONLY;
    # default "none".  Option streams carry no back-adjustment (ratio/difference
    # are ill-posed for option premia), so this is ignored for "option_stream";
    # a legacy value on a persisted option leg is accepted and has no effect.
    adjustment: str | None = None
    cycle: str | None = None  # Optional for "continuous" and "option_stream"
    # Roll-early offset.  "continuous" (futures) uses a bare int = DAYS (0..365).
    # "option_stream" uses the unified ``RollOffset`` ``{value, unit:days|months}``
    # — though a bare int is still accepted for it and read as days (legacy
    # shim).  None = no shift.  ("Roll at end of month" for options is the
    # EndOfMonth maturity, not a roll value — the former ``roll_schedule`` field
    # was removed.)
    roll_offset: int | RollOffset | None = None
    signal_spec: SignalLegSpec | None = None  # Required for "signal"
    # Option-stream fields (required when type == "option_stream")
    option_type: Literal["C", "P"] | None = None
    maturity: MaturityRule | None = None
    selection: SelectionCriterion | None = None
    stream: OptionStreamLabel | None = None
    # SELECT-AND-HOLD (fixed-contract dollar-P&L) for an option_stream leg.
    # Mirrors ``InstrumentOptionStream`` / ``OptionStreamRef`` semantics: when
    # True AND the stream is a PREMIUM (mid/bs_mid), the leg books fixed-contract
    # dollar P&L (a quantity sized once per roll off the compounding NAV,
    # qty·Δpremium daily) via the SHARED accumulator instead of a daily-reselect
    # %-return — so a short 10Δ-put leg reproduces the validated S1 signal curve.
    # DIRECTION (long/short) is the leg WEIGHT SIGN; ``nav_times`` is the
    # premium-notional size.  Ignored for level streams (iv/greeks) and for
    # non-option legs.  Default False = byte-identical to the daily-reselect path.
    hold_between_rolls: bool = False
    nav_times: float = 1.0
    # Futures-notional sizing for a hold-mode option PRICE leg (mirrors
    # ``OptionStreamRef`` / ``InstrumentOptionStream``).  ``premium_notional``
    # (DEFAULT, byte-identical): qty = nav_times·NAV_roll/premium_roll.
    # ``futures_notional``: qty = nav_times·NAV_roll/(F_ref·M_fut), daily $ =
    # qty·Δpremium·M_opt.  ``futures_reference`` picks the reference future (only
    # meaningful in futures_notional mode).  Ignored for non-hold / level legs.
    sizing_mode: Literal["premium_notional", "futures_notional"] = "premium_notional"
    futures_reference: Literal[
        "nearest_on_or_after", "continuous_front", "nearest_abs"
    ] = "nearest_on_or_after"

    @field_validator("nav_times")
    @classmethod
    def _check_nav_times(cls, v: float) -> float:
        # Delegate to the ONE shared validator in ``_models`` so this leg field
        # and ``OptionStreamRef.nav_times`` can never drift.
        return _validate_nav_times(v)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("instrument", "continuous", "signal", "option_stream"):
            raise ValueError(
                f"leg type must be 'instrument', 'continuous', 'signal', "
                f"or 'option_stream', got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def validate_signal_has_spec(self) -> LegSpec:
        if self.type == "signal" and self.signal_spec is None:
            raise ValueError("signal legs require 'signal_spec'")
        return self

    @model_validator(mode="after")
    def validate_option_price_leg_requires_hold(self) -> LegSpec:
        """An option PRICE leg (mid/bs_mid) MUST use hold-mode fixed-contract P&L.

        A rolled option's daily-reselect %-return is not a valid equity series:
        the resolver picks a DIFFERENT contract each day (delta/moneyness drift +
        the roll itself), so its day-over-day %-change mixes the real premium move
        with contract-switch jumps (e.g. a near-expiry ~$5 premium → a fresh ~$50
        contract reads as a +900% "return") → nonsensical, even NEGATIVE, equity.
        Hold-mode ``qty·Δpremium`` is the only sound accounting. Option LEVEL
        streams (iv/greeks/volume/oi) are display-only overlays, not equity, so
        they are exempt. Non-option legs are unaffected.
        """
        # ``_HOLD_PREMIUM_STREAMS`` (defined below at module scope) is the SINGLE
        # source of truth for which streams are premia — the same set the hold
        # resolver keys off — so this requirement can never drift from the set of
        # streams the hold path actually accepts.  Raise the codebase
        # ``ValidationError`` (the dominant idiom in this module): it surfaces the
        # message verbatim through the 400 ``validation_error`` envelope the
        # frontend reads, both at request parse and on direct construction.
        if (
            self.type == "option_stream"
            and self.stream in _HOLD_PREMIUM_STREAMS
            and not self.hold_between_rolls
        ):
            raise ValidationError(
                "option price legs (mid/bs_mid) require hold-mode fixed-contract "
                "P&L — enable 'Hold contract between rolls'; a rolled option's "
                "daily-reselect %-return is not a valid equity series"
            )
        return self

    @model_validator(mode="after")
    def validate_option_stream_has_fields(self) -> LegSpec:
        """Ensure option_stream legs carry all required option fields."""
        if self.type != "option_stream":
            return self
        missing: list[str] = []
        if self.collection is None:
            missing.append("collection")
        if self.option_type is None:
            missing.append("option_type")
        if self.maturity is None:
            missing.append("maturity")
        if self.selection is None:
            missing.append("selection")
        if self.stream is None:
            missing.append("stream")
        if missing:
            raise ValueError(f"option_stream legs require: {', '.join(missing)}")
        return self


class PortfolioRequest(BaseModel):
    legs: dict[str, LegSpec]
    weights: dict[str, float]
    rebalance: str = "none"
    return_type: str = "normal"
    start: str | None = None
    end: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_legs(
    legs: dict[str, LegSpec],
    classify: Callable[[str], AssetClass | None],
) -> dict[str, InstrumentId | ContinuousLegSpec]:
    """Convert request leg specs to service-layer types with validation.

    Only processes instrument/continuous legs; signal and option_stream
    legs are skipped (handled separately).
    """
    legs_spec: dict[str, InstrumentId | ContinuousLegSpec] = {}

    for label, leg in legs.items():
        if leg.type in ("signal", "option_stream"):
            continue

        if leg.type == "instrument":
            if not leg.collection:
                raise ValidationError(
                    f"Leg '{label}': 'collection' is required for instrument legs"
                )
            if not leg.symbol:
                raise ValidationError(
                    f"Leg '{label}': 'symbol' is required for instrument legs"
                )
            asset_class = classify(leg.collection)
            if asset_class is None:
                raise ValidationError(
                    f"Leg '{label}': cannot determine asset class for "
                    f"collection '{leg.collection}'"
                )
            legs_spec[label] = InstrumentId(
                symbol=leg.symbol,
                asset_class=asset_class,
                collection=leg.collection,
            )

        else:  # "continuous"
            if not leg.collection:
                raise ValidationError(
                    f"Leg '{label}': 'collection' is required for continuous legs"
                )
            if not leg.strategy:
                raise ValidationError(
                    f"Leg '{label}': 'strategy' is required for continuous legs"
                )
            try:
                roll_strategy = RollStrategy(leg.strategy)
            except ValueError:
                raise ValidationError(
                    f"Leg '{label}': invalid strategy '{leg.strategy}'. "
                    f"Must be one of: {', '.join(e.value for e in RollStrategy)}"
                )

            adj_method = AdjustmentMethod.NONE
            if leg.adjustment:
                try:
                    adj_method = AdjustmentMethod(leg.adjustment)
                except ValueError:
                    raise ValidationError(
                        f"Leg '{label}': invalid adjustment '{leg.adjustment}'. "
                        f"Must be one of: {', '.join(e.value for e in AdjustmentMethod)}"
                    )

            # Continuous (futures) legs roll in DAYS only. Accept a bare int or a
            # RollOffset with unit='days'; reject months (futures EOM is the
            # separate RollStrategy.END_OF_MONTH, not a roll-offset unit).
            roll_offset_days = 0
            if leg.roll_offset is not None:
                if isinstance(leg.roll_offset, RollOffset):
                    if leg.roll_offset.unit != "days":
                        raise ValidationError(
                            f"Leg '{label}': continuous legs only support a "
                            f"roll_offset in days, got unit "
                            f"{leg.roll_offset.unit!r}"
                        )
                    raw_days = leg.roll_offset.value
                else:
                    raw_days = leg.roll_offset
                if not (0 <= raw_days <= 365):
                    raise ValidationError(
                        f"Leg '{label}': roll_offset must be between 0 and 365"
                    )
                roll_offset_days = raw_days

            legs_spec[label] = ContinuousLegSpec(
                collection=leg.collection,
                roll_config=ContinuousRollConfig(
                    strategy=roll_strategy,
                    adjustment=adj_method,
                    cycle=leg.cycle,
                    roll_offset_days=roll_offset_days,
                ),
            )

    return legs_spec


async def _evaluate_signal_leg(
    label: str,
    leg: LegSpec,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
    repo: WriteRepository,
) -> _SignalLegEvalResult:
    """Evaluate a signal leg and bubble up everything the portfolio path needs.

    The synthetic price series starts at 100 and accumulates the sum of
    all per-input realized_pnl arrays from the signal evaluation:

        synthetic = 100.0 * (1.0 + aggregated_pnl)

    Basket inputs (inline OR saved) on the signal's spec are pre-resolved
    via :func:`_resolve_basket_inputs` and threaded into ``parse_signal``
    as ``resolved_inputs=`` — mirrors :func:`compute_signal`'s pattern so
    that a portfolio signal leg whose input is a basket doesn't crash
    inside ``_parse_input`` on the continuous-branch fallback.

    Returns:
        ``_SignalLegEvalResult`` carrying the YYYYMMDD int date index, the
        synthetic price series, the raw per-signal ``Trade`` tuple (bar
        indices in the signal's own index space, NOT the portfolio's
        common_dates — caller is responsible for re-mapping), and the
        per-input price payloads matching the signals-API positions shape.
    """
    if leg.signal_spec is None:
        raise ValidationError(f"Leg '{label}': signal legs require 'signal_spec'")

    # 1. Pre-resolve basket refs (inline + saved) and parse the signal
    #    spec into engine types. Mirrors ``compute_signal`` so that
    #    BasketRefInline / BasketRefSaved inputs are materialised into
    #    typed-leg snapshots before ``_parse_input`` runs.
    try:
        resolved_inputs = await _resolve_basket_inputs(
            leg.signal_spec.spec.inputs, repo, svc
        )
        signal = parse_signal(leg.signal_spec.spec, resolved_inputs=resolved_inputs)
    except SignalValidationError as exc:
        raise ValidationError(f"Leg '{label}': signal validation error: {exc}") from exc

    if len(signal.inputs) == 0:
        raise ValidationError(f"Leg '{label}': signal has no inputs")

    # 2. Parse indicators into IndicatorSpecInput dict
    indicators: dict[str, IndicatorSpecInput] = {}
    for ind_spec in leg.signal_spec.indicators:
        if ind_spec.id in indicators:
            raise ValidationError(
                f"Leg '{label}': duplicate indicator id {ind_spec.id!r}"
            )
        series_labels = tuple(ind_spec.seriesMap.keys())
        indicators[ind_spec.id] = IndicatorSpecInput(
            code=ind_spec.code,
            params=dict(ind_spec.params),
            series_labels=series_labels,
            series_map={
                lbl: (ref.collection, ref.instrument_id)
                for lbl, ref in ind_spec.seriesMap.items()
            },
        )

    # 3. Compute input overlap dates
    try:
        overlap_start, overlap_end = await compute_input_overlap(
            svc,
            signal,
            start_date,
            end_date,
        )
    except SignalDataError as exc:
        raise ValidationError(f"Leg '{label}': signal data error: {exc}") from exc

    # 4. Create fetcher and evaluate
    fetcher = make_signal_fetcher(svc, overlap_start, overlap_end)
    try:
        result = await evaluate_signal(signal, indicators, fetcher)
    except SignalValidationError as exc:
        raise ValidationError(f"Leg '{label}': signal validation error: {exc}") from exc
    except SignalDataError as exc:
        raise ValidationError(f"Leg '{label}': signal data error: {exc}") from exc
    except SignalRuntimeError as exc:
        raise ValidationError(f"Leg '{label}': signal runtime error: {exc}") from exc

    # 5. Aggregate realized_pnl across all inputs
    T = len(result.index)
    aggregated_pnl = np.zeros(T, dtype=np.float64)
    for pos in result.positions:
        aggregated_pnl += pos.realized_pnl

    # 6. Convert to synthetic prices (starting at 100)
    synthetic = 100.0 * (1.0 + aggregated_pnl)

    # 7. Build the signal-local → underlying instrument id remap. Trades
    #    and per-input positions are keyed by the signal-LOCAL input name
    #    (e.g. "index"); at the portfolio layer we want the actual
    #    underlying instrument id (e.g. "SPX") so signal-leg trades line
    #    up with direct-leg trades in the TradeLog. Missing entries fall
    #    back to the signal-local id with a warning (would indicate a
    #    bug or stale data).
    underlying_by_local: dict[str, str] = {}
    for inp in signal.inputs:
        underlying = _signal_input_underlying_id(inp.instrument)
        if underlying is None:
            logger.warning(
                "portfolio: signal %r input %r has unrecognised instrument "
                "variant %r — keeping signal-local id for trade/position "
                "remap",
                label,
                inp.id,
                type(inp.instrument).__name__,
            )
            continue
        underlying_by_local[inp.id] = underlying

    def _remap_id(local_id: str) -> str:
        mapped = underlying_by_local.get(local_id)
        if mapped is None:
            logger.warning(
                "portfolio: signal %r emitted input_id %r with no matching "
                "Input — keeping original id",
                label,
                local_id,
            )
            return local_id
        return mapped

    remapped_trades = tuple(
        replace(tr, input_id=_remap_id(tr.input_id)) for tr in result.trades
    )

    # Trade-log sizing needs each input's COLLECTION (FUT_/OPT_/spot) to pick
    # the contract multiplier. Key by the SAME remapped id the trades carry so
    # the downstream lookup is by ``trade.input_id`` (first input wins on a
    # collision — distinct inputs sharing an underlying with different
    # collections is not expected).
    collection_by_input: dict[str, str] = {}
    for inp in signal.inputs:
        coll = _signal_input_collection(inp.instrument)
        if coll is None:
            continue
        collection_by_input.setdefault(_remap_id(inp.id), coll)

    # 8. Build per-input price payloads in the signals-API shape so the
    #    portfolio TradeLog can look up open/close prices by input_id.
    positions_payload: list[dict] = []
    for pos in result.positions:
        if pos.price_label is None or pos.price_values is None:
            price_payload: dict | None = None
        else:
            price_payload = {
                "label": pos.price_label,
                "values": nan_safe_floats(pos.price_values),
            }
        positions_payload.append(
            {"input_id": _remap_id(pos.input_id), "price": price_payload}
        )

    return _SignalLegEvalResult(
        index=result.index,
        synthetic=synthetic,
        trades=remapped_trades,
        positions_payload=tuple(positions_payload),
        collection_by_input=collection_by_input,
    )


def _compute_level_metrics(values: npt.NDArray[np.float64]) -> dict:
    """Compute summary metrics for a level (non-price) series."""
    valid = values[~np.isnan(values)]
    if len(valid) == 0:
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "first": None,
            "last": None,
            "change": None,
        }
    return {
        "mean": float(np.mean(valid)),
        "std": float(np.std(valid)),
        "min": float(np.min(valid)),
        "max": float(np.max(valid)),
        "first": float(valid[0]),
        "last": float(valid[-1]),
        "change": float(valid[-1] - valid[0]),
    }


# Actionable hint per dominant per-date diagnostic code, appended to the
# all-NaN option-leg error so the user learns WHY and what to change.
_DIAGNOSTIC_HINTS: dict[str, str] = {
    "missing_delta_no_compute": (
        "no stored greeks/deltas over this range — By Delta needs stored deltas; "
        "use By Moneyness or By Strike, or pick a date range that has greeks"
    ),
    "missing_mid": (
        "no valid bid/ask quotes (mid needs both bid and ask > 0) on these dates — "
        "quotes may be too sparse for this contract"
    ),
    "no_chain_for_date": (
        "the targeted expiration is not listed for this root on these dates"
    ),
    "maturity_resolution_failed": (
        "the maturity rule could not be resolved on these dates "
        "(check the rule's parameters)"
    ),
    "no_match_within_tolerance": (
        "no contract within the delta tolerance — widen the tolerance or "
        "disable strict matching"
    ),
    "past_last_trade_date": (
        "the requested dates are past this root's last trade date"
    ),
    "missing_underlying_price": (
        "no underlying price available to evaluate moneyness on these dates"
    ),
}


def _diagnostic_hint(diagnostics: list[str | None] | None) -> str:
    """Summarise the per-date ``error_codes`` into an actionable suffix.

    Returns a string beginning with ``"; "`` (so it appends cleanly to the
    base all-NaN message) naming the dominant failure code and an actionable
    hint, or ``""`` when there is nothing useful to add.  ``snapped_to:*``
    notes are informational (a successful substitution), not failures, so they
    are excluded from the cause tally.
    """
    if not diagnostics:
        return ""
    causes = Counter(
        c for c in diagnostics if c is not None and not c.startswith("snapped_to:")
    )
    if not causes:
        return ""
    dominant, count = causes.most_common(1)[0]
    total = sum(causes.values())
    hint = _DIAGNOSTIC_HINTS.get(dominant)
    detail = f" — {hint}" if hint else ""
    return f"; dominant cause: {dominant} ({count}/{total} dates){detail}"


async def _empty_cycle_hint(svc: MarketDataService, leg: LegSpec) -> str | None:
    """Return a targeted "no contracts match this cycle" message, or ``None``.

    Called ONLY on the all-NaN error path (so it never costs the happy path).
    When the leg's requested ``cycle`` is non-null and — after ``expand_cycle``
    broadening — matches NONE of the root's real ``expiration_cycle`` tags, the
    empty chain is explained by an inapplicable cycle (e.g. ``"Q"`` for
    OPT_SP_500).  Names the requested cycle and the root's available cycles.

    Returns ``None`` (fall back to the generic diagnostic hint) when the cycle is
    ``None`` (no filter), when the cycle IS available for the root (the empty
    chain has some other cause — data gap, delta miss, …), or when the root's
    cycle list can't be fetched (a bare test double / transient reader error) —
    a hint is best-effort and must never mask the real error.
    """
    requested = leg.cycle
    if requested is None:
        return None
    try:
        available = await svc.get_available_cycles(leg.collection)
    except Exception:  # noqa: BLE001 — best-effort hint; degrade to generic path
        return None
    if not available:
        return None
    expanded = expand_cycle(requested)
    expanded_tags = (expanded,) if isinstance(expanded, str) else tuple(expanded)
    if any(tag in available for tag in expanded_tags):
        return None  # the cycle exists for this root — some other cause
    return (
        f"no contracts match cycle {requested!r} for {leg.collection} — "
        f"available cycles: {', '.join(available)}"
    )


# A hold-mode option leg books fixed-contract dollar P&L only for a PREMIUM
# stream.  Both ``mid`` and ``bs_mid`` are premia (bs_mid is the Black-76
# theoretical premium — the S1 oracle's price basis) and the resolver's hold
# path supports both.  A premium leg WITHOUT hold is rejected at construction
# (``validate_option_price_leg_requires_hold``), so a premium always takes the
# hold path.  Levels (iv/greeks/volume/oi) are NOT premia — hold does not apply,
# they keep the display-only (tracking-overlay) path.
_HOLD_PREMIUM_STREAMS: frozenset[str] = frozenset({"mid", "bs_mid"})


def _is_hold_mode_price_leg(leg: LegSpec) -> bool:
    """True iff ``leg`` is a hold-mode option PRICE leg (a mid/bs_mid premium with
    ``hold_between_rolls``), i.e. one whose equity is the fixed-contract $-P&L
    synthetic — which can wipe to an absorbing 0 (a fully-decayed / blown-up
    short) and then emit NaN returns.  Level streams (iv/greeks/volume/oi) and
    non-option legs are never hold-mode price legs.  Static (reads only the leg
    spec), so ``compute_portfolio`` can gate incompatible rebalance/return knobs
    BEFORE evaluating any leg.
    """
    return (
        leg.type == "option_stream"
        and leg.hold_between_rolls
        and leg.stream in _HOLD_PREMIUM_STREAMS
    )


async def _evaluate_option_stream_leg(
    label: str,
    leg: LegSpec,
    weight: float,
    svc: MarketDataService,
    start_date: date | None,
    end_date: date | None,
) -> tuple[
    npt.NDArray[np.int64],
    npt.NDArray[np.float64],
    str,
    list[int],
    npt.NDArray[np.float64] | None,
    npt.NDArray[np.float64] | None,
    float,
    npt.NDArray[np.float64] | None,
]:
    """Resolve an option_stream leg and return
    (dates, values, stream_mode, roll_dates, premium, future_ref, m_fut,
    roll_premium).

    ``roll_dates`` are the DISPLAY-ONLY interior roll BOUNDARIES (YYYYMMDD ints,
    the initial open excluded) for the trade-log roll rows, and ``premium`` is the
    raw held-premium series the hold accumulator was fed (same axis as ``dates``)
    — both populated only on the "price_hold" path, ``[]`` / ``None`` otherwise.
    ``roll_premium`` (the resolver's roll-day OPEN premium, same axis as ``dates``,
    finite only at roll bars) is the DISPLAY-ONLY basis for a premium-notional roll
    row's contract COUNT: the daily held premium is NaN at a far-OTM option's later
    segment opens, but the roll-day premium the accumulator sized against is finite.
    ``None`` off the "price_hold" path.
    ``future_ref`` (the resolver's ``roll_future_ref``, same axis as ``dates``,
    finite only at roll bars) and ``m_fut`` (the futures multiplier the engine
    sized off) are the DISPLAY-ONLY side-channels that let the caller size a
    ``sizing_mode == "futures_notional"`` leg's roll-row COUNT off the FUTURES
    notional (``|w|·NAV/(F_ref·m_fut)``) instead of the premium notional; both are
    ``None`` / ``NaN`` for a premium-sized (default) or non-hold leg.  None of
    these side-channels affect ``values`` (the equity synthetic); they feed only
    the informational per-roll trade rows.

    ``stream_mode`` is either "price_hold" (the hold-mode synthetic $-P&L equity
    leg; caller must apply |weight| — direction is already baked in) or "level" (a
    greeks/IV/volume/oi display overlay, NOT part of the equity curve).  A non-hold
    premium ("price") leg is impossible — mid/bs_mid REQUIRE hold-mode
    (``validate_option_price_leg_requires_hold``) — so only those two modes occur.

    SELECT-AND-HOLD: when ``leg.hold_between_rolls`` is True AND the stream is a
    PREMIUM (mid/bs_mid), the leg is resolved through the SAME hold-mode resolver
    the signal path uses (``make_signal_fetcher`` → ``resolve_option_stream``) and
    its fixed-contract dollar P&L is booked via the SHARED accumulator
    (:func:`tcg.engine.hold_pnl._compound_with_hold`).  ``values`` is then the
    leg's SYNTHETIC equity curve ``100·equity_ratio`` with DIRECTION (the sign of
    ``weight``) and ``nav_times`` already baked in — a "price_hold" leg, exactly
    like a signal leg's synthetic.  The caller must therefore feed the leg's
    |weight| (not the signed weight) to ``compute_weighted_portfolio`` so the short
    is applied ONCE.  ``weight`` is consulted only on this hold path.

    Returns:
        Tuple of (YYYYMMDD int dates, values array, stream_mode, interior roll
        dates, raw premium, future_ref series, futures multiplier).
    """
    # 1. Build an OptionStreamRef from the leg's fields.  ``roll_offset``
    #    mirrors the continuous-leg precedent in ``_parse_legs``: validate the
    #    range here so the error carries leg context (the OptionStreamRef Field
    #    bound would otherwise raise a bare 422), and default a missing value to
    #    the no-op (0).
    #
    #    Option streams carry NO back-adjustment (ratio/difference are ill-posed
    #    for option premia), so ``leg.adjustment`` is ignored on this path — the
    #    shared ``LegSpec.adjustment`` field applies only to continuous legs.
    # ``roll_offset`` is the unified {value, unit} (a bare int reads as days).
    # OptionStreamRef's RollOffset model validates the per-unit range and raises
    # a structured error; default a missing value to the no-op.  ("end of month"
    # is the EndOfMonth maturity, not a roll value — no roll_schedule here.)
    roll_offset = RollOffset() if leg.roll_offset is None else leg.roll_offset
    # A hold-mode PREMIUM leg (mid/bs_mid + hold flag) books fixed-contract dollar
    # P&L; every other case (non-hold, or a level stream) keeps the display path
    # with hold OFF on the ref → byte-identical to today.
    is_hold_premium = leg.hold_between_rolls and leg.stream in _HOLD_PREMIUM_STREAMS
    try:
        ref = OptionStreamRef(
            type="option_stream",
            collection=leg.collection,
            option_type=leg.option_type,
            cycle=leg.cycle,
            maturity=leg.maturity,
            selection=leg.selection,
            stream=leg.stream,
            roll_offset=roll_offset,
            hold_between_rolls=is_hold_premium,
            nav_times=leg.nav_times,
            sizing_mode=leg.sizing_mode,
            futures_reference=leg.futures_reference,
        )
    except PydanticValidationError as exc:
        raise ValidationError(f"Leg '{label}': {exc}") from exc

    # 1b. SELECT-AND-HOLD price leg → fixed-contract dollar-P&L equity curve, via
    #     the SAME resolver + SHARED accumulator the signal path uses (no new
    #     rolling code).  Direction (sign of ``weight``) + ``nav_times`` are baked
    #     into the returned synthetic ``100·equity_ratio``.
    if is_hold_premium:
        # Convert the validated ref → engine InstrumentOptionStream via the ONE
        # shared converter (also used by signals._parse_input and _series_fetch),
        # so the ref→dataclass field mapping can't drift.  ``ref`` was built with
        # ``hold_between_rolls=is_hold_premium`` (True on this branch) and
        # ``nav_times=leg.nav_times``, so the converter yields hold ON with the
        # same nav_times.  The heavy option rolling/selection wiring is then
        # reused verbatim via make_signal_fetcher.
        from tcg.core.api.options import option_stream_ref_to_instrument

        instrument = option_stream_ref_to_instrument(ref)
        fetcher = make_signal_fetcher(svc, start_date, end_date)
        try:
            dates_arr, premium = await fetcher(instrument, "close")
            # 3→4-tuple ripple (Guardrail Sign 4): the production fetcher carries
            # roll_future_ref for futures-notional sizing; a legacy 3-tuple double
            # (premium_notional only) still works.
            _rres = await fetcher.fetch_hold_roll_info(instrument)
            if len(_rres) == 4:
                _d, is_roll_f, roll_premium, roll_fref = _rres
            else:
                _d, is_roll_f, roll_premium = _rres
                roll_fref = None
        except (SignalDataError, SignalValidationError) as exc:
            raise ValidationError(f"Leg '{label}': {exc}") from exc

        premium = np.asarray(premium, dtype=np.float64)
        if not np.any(np.isfinite(premium)):
            # An empty resolve fails LOUDLY instead of returning a misleading
            # flat-100 leg.  Thread the resolver's per-date diagnostics (surfaced
            # by the fetcher's optional ``fetch_hold_diagnostics`` side-channel —
            # the same additive pattern as ``fetch_hold_roll_info``) into the
            # message via ``_diagnostic_hint``, so it names the dominant cause
            # (missing_delta / missing_mid / no_chain / …) and steers the user
            # (ByDelta→ByMoneyness), exactly like the display path did.  A fetcher
            # without the accessor (e.g. a bare test double) degrades cleanly to
            # the base message.
            hold_diagnostics: list[str | None] | None = None
            diag_fn = getattr(fetcher, "fetch_hold_diagnostics", None)
            if diag_fn is not None:
                hold_diagnostics = await diag_fn(instrument)
            # Cycle-specific hint: the dominant empty-chain cause is often a
            # cycle tag that simply doesn't exist for this root (e.g. "Q" for
            # OPT_SP_500).  Only on this ERROR path (never the happy path) probe
            # the root's real cycles; if the requested cycle — after expansion —
            # matches NONE of them, say so and list what IS available.  This is
            # cheap here and turns a generic no_chain hint into an actionable one.
            cycle_hint = await _empty_cycle_hint(svc, leg)
            if cycle_hint is not None:
                raise ValidationError(f"Leg '{label}': {cycle_hint}")
            raise ValidationError(
                f"Leg '{label}': all option stream values are NaN"
                f"{_diagnostic_hint(hold_diagnostics)}"
            )
        T = int(premium.shape[0])
        # Futures-notional sizing: resolve the per-root multipliers via the fetcher
        # side-channel (live-first / config fallback in the core layer); a NaN pair
        # triggers the engine tail carry-forward.  premium_notional legs leave the
        # multipliers/roll_future_ref inert.
        mult_fut = float("nan")
        mult_opt = float("nan")
        roll_fref_arr: "npt.NDArray[np.float64] | None" = None
        if leg.sizing_mode == "futures_notional":
            if roll_fref is not None:
                roll_fref_arr = np.asarray(roll_fref, dtype=np.float64)
            _mult_fn = getattr(fetcher, "fetch_hold_multipliers", None)
            if _mult_fn is not None:
                mult_fut, mult_opt = await _mult_fn(instrument)
        # DIRECTION is the leg weight SIGN (a portfolio leg is always held, so
        # ``pos_active`` is all True); ``nav_times`` is the premium-notional SIZE.
        # This is exactly the spec signal_exec builds for a hold-mode option
        # input, so a single short hold-put leg reproduces the S1 signal curve.
        spec = _HoldPnLSpec(
            ref_id="_leg",
            sign=float(np.sign(weight)),
            nav_times=float(leg.nav_times),
            premium=premium,
            is_roll=np.asarray(is_roll_f, dtype=np.float64) > 0.5,
            roll_premium=np.asarray(roll_premium, dtype=np.float64),
            pos_active=np.ones(T, dtype=np.bool_),
            sizing_mode=leg.sizing_mode,
            roll_future_ref=roll_fref_arr,
            mult_fut=float(mult_fut),
            mult_opt=float(mult_opt),
        )
        equity_ratio, _step_scale, _hold_contrib = _compound_with_hold(
            np.zeros(max(T - 1, 0), dtype=np.float64), [spec]
        )
        synthetic = 100.0 * equity_ratio
        # DISPLAY-ONLY roll boundaries for the trade-log roll rows: the ``is_roll``
        # dates EXCLUDING the initial open (``is_roll[0]`` marks the first held
        # contract, which is the natural segment-0 open, not a boundary).  Keyed
        # off ``_d`` (the roll-info date axis ``is_roll`` aligns to).  ``premium``
        # is threaded back so the caller sizes the option roll rows off the true
        # premium (``price_by_input`` holds the synthetic equity, not a premium).
        _d_arr = np.asarray(_d, dtype=np.int64)
        _roll_mask = np.asarray(is_roll_f, dtype=np.float64) > 0.5
        _roll_all = [int(x) for x in _d_arr[_roll_mask].tolist()]
        roll_dates_interior = _roll_all[1:]
        # ``roll_fref`` (the resolver's ``roll_future_ref``, same axis as ``dates``)
        # and the resolved ``mult_fut`` are threaded out for the DISPLAY-ONLY
        # futures-notional roll-row sizing (see §10).  Both are inert for a
        # premium-notional leg (``roll_fref`` is None, ``mult_fut`` is NaN).
        return (
            dates_arr,
            synthetic,
            "price_hold",
            roll_dates_interior,
            premium,
            roll_fref,
            float(mult_fut),
            np.asarray(roll_premium, dtype=np.float64),
        )

    # 2. Materialise via shared infrastructure
    result = await materialise_option_streams(
        [("_leg", ref)],
        svc=svc,
        start_date=start_date,
        end_date=end_date,
    )
    if isinstance(result, str):
        raise ValidationError(f"Leg '{label}': {result}")

    dates_arr, values, _diagnostics, _contracts = result["_leg"]

    # 3. Only display-only LEVEL streams reach this point.  A PREMIUM leg
    #    (mid/bs_mid) either took the hold branch above (early return
    #    "price_hold") or was rejected at LegSpec construction
    #    (``validate_option_price_leg_requires_hold``), and ``PRICE_LIKE_STREAMS``
    #    ({"mid"}) ⊆ the premium set — so no %-return "price" leg can reach here.
    #    A level leg (iv/greeks/volume/oi) is a tracking overlay, NOT part of the
    #    equity curve, so it needs no forward-fill or all-NaN guard here (an
    #    all-NaN level leg is surfaced downstream as an empty tracking series).
    return dates_arr, values, "level", [], None, None, float("nan"), None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/compute")
async def compute_portfolio(
    body: PortfolioRequest,
    svc: MarketDataService = Depends(get_market_data),
    classify: Callable[[str], AssetClass | None] = Depends(get_collection_classifier),
    repo: WriteRepository = Depends(get_write_repository),
) -> dict:
    """Compute a weighted portfolio with rebalancing and return full analytics."""

    # ── 1. Validate inputs ──

    if not body.legs:
        raise ValidationError("legs must not be empty")

    # Weights must cover every leg label
    missing_weights = set(body.legs.keys()) - set(body.weights.keys())
    if missing_weights:
        raise ValidationError(
            f"weights missing for legs: {', '.join(sorted(missing_weights))}"
        )

    # Rebalance frequency
    try:
        rebalance_freq = RebalanceFreq(body.rebalance)
    except ValueError:
        raise ValidationError(
            f"Invalid rebalance '{body.rebalance}'. "
            f"Must be one of: {', '.join(e.value for e in RebalanceFreq)}"
        )

    # Return type
    if body.return_type not in ("normal", "log"):
        raise ValidationError(
            f"return_type must be 'normal' or 'log', got {body.return_type!r}"
        )

    # A hold-mode option PRICE leg's synthetic can hit an absorbing 0 (a wiped
    # short) and then emit NaN returns.  Two SHARED, pre-existing engine behaviours
    # silently corrupt such a leg, so reject the incompatible knobs at the boundary
    # rather than emit a misleading curve:
    #   * rebalance != 'none' re-funds a wiped (0-valued) leg back to its target
    #     share at each boundary (``metrics._compute_periodic_rebalance``) →
    #     idle capital drains the surviving legs;
    #   * return_type='log' maps a finite→0 transition to ln(0) = -inf → the leg
    #     is held FLAT (``metrics._compute_buy_and_hold``) instead of going to 0,
    #     overstating equity.
    # Both are correct for ordinary price legs; only a hold-mode option leg (meant
    # to be held to expiry — its direction + nav_times live in the synthetic)
    # breaks them.  Guard here (contained) rather than editing the shared engine.
    has_hold_option_leg = any(
        _is_hold_mode_price_leg(leg) for leg in body.legs.values()
    )
    if has_hold_option_leg and rebalance_freq != RebalanceFreq.NONE:
        raise ValidationError(
            "hold-mode option price legs require rebalance='none'; a wiped leg "
            "would be silently re-funded to its target weight at each rebalance "
            "boundary, draining the surviving legs"
        )
    if has_hold_option_leg and body.return_type == "log":
        raise ValidationError(
            "hold-mode option price legs require return_type='normal'; under log "
            "returns a leg wiped to zero (ln(0) = -inf) is held flat instead of "
            "going to zero, overstating the equity"
        )

    try:
        start_date, end_date = parse_iso_range(body.start, body.end)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    # ── 2. Separate legs by type ──

    instrument_legs = {
        label: leg
        for label, leg in body.legs.items()
        if leg.type in ("instrument", "continuous")
    }
    signal_legs = {
        label: leg for label, leg in body.legs.items() if leg.type == "signal"
    }
    option_stream_legs = {
        label: leg for label, leg in body.legs.items() if leg.type == "option_stream"
    }

    # ── 3. Fetch instrument prices (if any) ──

    # Will hold YYYYMMDD int dates for each source.
    all_date_grids: list[npt.NDArray[np.int64]] = []
    # Will hold (full_dates, full_closes) per label for instrument legs.
    instrument_full_dates: npt.NDArray[np.int64] | None = None
    instrument_dates: npt.NDArray[np.int64] | None = None
    instrument_closes: dict[str, npt.NDArray[np.float64]] = {}
    # Bound unconditionally so §9.5 (continuous roll-boundary re-surfacing) can
    # look up a continuous leg's ContinuousLegSpec even in code paths where the
    # instrument block did not run (it always runs when a continuous leg exists,
    # but an empty default keeps the name safe).
    legs_spec: dict[str, InstrumentId | ContinuousLegSpec] = {}

    if instrument_legs:
        legs_spec = _parse_legs(body.legs, classify)

        # Fetch full overlapping date range for instrument legs.
        full_common_dates, full_aligned_series = await svc.get_aligned_prices(
            legs_spec,
        )
        instrument_full_dates = full_common_dates

        # Apply optional date filter
        if start_date or end_date:
            lo = date_to_int(start_date) if start_date else 0
            hi = date_to_int(end_date) if end_date else 99999999
            mask = (full_common_dates >= lo) & (full_common_dates <= hi)
            common_dates = full_common_dates[mask]
            aligned_series = {
                label: type(series)(
                    dates=series.dates[mask],
                    open=series.open[mask],
                    high=series.high[mask],
                    low=series.low[mask],
                    close=series.close[mask],
                    volume=series.volume[mask],
                )
                for label, series in full_aligned_series.items()
            }
            if len(common_dates) == 0:
                raise ValidationError("No data in the selected date range")
        else:
            common_dates = full_common_dates
            aligned_series = full_aligned_series

        instrument_dates = common_dates
        instrument_closes = {
            label: series.close for label, series in aligned_series.items()
        }
        all_date_grids.append(instrument_dates)

    # ── 4. Evaluate signal legs (if any) ──

    # signal_dates[label] = YYYYMMDD array, signal_closes[label] = synthetic prices
    signal_dates_map: dict[str, npt.NDArray[np.int64]] = {}
    signal_closes: dict[str, npt.NDArray[np.float64]] = {}
    # Per-leg trade + positions payloads bubbled up from _evaluate_signal_leg
    # for portfolio-level trade log aggregation (see §10 below).
    signal_trades_map: dict[str, tuple[Trade, ...]] = {}
    signal_positions_map: dict[str, tuple[dict, ...]] = {}
    # label -> {remapped input_id -> collection} for trade-log contract sizing.
    signal_collections_map: dict[str, dict[str, str]] = {}

    for label, leg in signal_legs.items():
        leg_result = await _evaluate_signal_leg(
            label,
            leg,
            svc,
            start_date,
            end_date,
            repo,
        )
        signal_dates_map[label] = leg_result.index
        signal_closes[label] = leg_result.synthetic
        signal_trades_map[label] = leg_result.trades
        signal_positions_map[label] = leg_result.positions_payload
        signal_collections_map[label] = leg_result.collection_by_input
        all_date_grids.append(leg_result.index)

    # ── 4.5. Evaluate option_stream legs (if any) ──

    option_stream_dates_map: dict[str, npt.NDArray[np.int64]] = {}
    option_stream_closes: dict[str, npt.NDArray[np.float64]] = {}
    tracking_series: dict[str, dict] = {}  # level legs -> separate response section
    # Hold-mode option price legs carry DIRECTION inside their synthetic equity
    # curve (like signal legs), so their portfolio share below is |weight| — the
    # signed-weight short must NOT be re-applied by the weight normalization.
    hold_option_labels: set[str] = set()
    # DISPLAY-ONLY per-leg roll boundaries + raw premium for the trade-log roll
    # rows of a hold-mode option leg (see §10).  Never touch the equity synthetic.
    option_roll_dates_interior: dict[str, list[int]] = {}
    option_premium_raw: dict[str, npt.NDArray[np.float64] | None] = {}
    # DISPLAY-ONLY futures-notional sizing side-channels for a hold-mode option leg
    # in ``sizing_mode == "futures_notional"``: the reference-future price series
    # (aligned below like the premium) + the resolved futures multiplier, so the
    # roll-row COUNT follows the leg's sizing_mode (see §10).  Never touch equity.
    option_future_ref_raw: dict[str, npt.NDArray[np.float64] | None] = {}
    option_mult_fut: dict[str, float] = {}
    # DISPLAY-ONLY roll-day OPEN premium (finite at roll bars) for a premium-notional
    # hold leg's roll-row COUNT: the daily held premium is NaN at a far-OTM option's
    # later segment opens, so the count must be sized off this (what the accumulator
    # sized against) instead.  Never touches equity.
    option_roll_premium_raw: dict[str, npt.NDArray[np.float64] | None] = {}

    for label, leg in option_stream_legs.items():
        (
            os_dates,
            os_values,
            stream_mode,
            os_roll_interior,
            os_premium,
            os_future_ref,
            os_mult_fut,
            os_roll_premium,
        ) = await _evaluate_option_stream_leg(
            label,
            leg,
            body.weights[label],
            svc,
            start_date,
            end_date,
        )

        if stream_mode in ("price", "price_hold"):
            # Price leg -- joins the main portfolio equity curve
            option_stream_dates_map[label] = os_dates
            option_stream_closes[label] = os_values
            all_date_grids.append(os_dates)
            # Flag a hold leg OFF THE ACTUAL PATH TAKEN ("price_hold"), NOT the
            # raw leg.hold_between_rolls flag: the hold path is gated on
            # (flag AND stream in _HOLD_PREMIUM_STREAMS), so re-deriving from the
            # flag alone would use |weight| for a leg that took the display
            # (%-return) path — a silent sign-drop if a price-like non-premium
            # stream is ever added. Keying off the returned mode can't drift.
            if stream_mode == "price_hold":
                hold_option_labels.add(label)
                option_roll_dates_interior[label] = os_roll_interior
                option_premium_raw[label] = os_premium
                option_future_ref_raw[label] = os_future_ref
                option_mult_fut[label] = os_mult_fut
                option_roll_premium_raw[label] = os_roll_premium
        else:
            # Level leg -- tracking overlay only (not in equity curve)
            tracking_series[label] = {
                "dates": [int_to_iso(int(d)) for d in os_dates],
                "values": nan_safe_floats(os_values),
                "stream": leg.stream,
                "stream_mode": "level",
                "metrics": _compute_level_metrics(os_values),
            }

    # ── 5. Align all series to common dates ──

    if not all_date_grids:
        raise ValidationError(
            "No price-like legs to compute portfolio equity curve. "
            "Use 'mid' stream for option legs that should participate "
            "in the portfolio."
        )

    # Find intersection of all date grids
    common_dates = all_date_grids[0]
    for grid in all_date_grids[1:]:
        common_dates = np.intersect1d(common_dates, grid, assume_unique=False)

    if len(common_dates) == 0:
        raise ValidationError(
            "No overlapping dates across all legs — the instrument, signal, "
            "and option date ranges are disjoint (an option leg's available "
            "dates often differ from the spot/continuous legs')"
        )

    # Slice instrument closes to common dates
    aligned_closes: dict[str, npt.NDArray[np.float64]] = {}
    if instrument_dates is not None:
        inst_mask = np.isin(instrument_dates, common_dates, assume_unique=True)
        for label, closes in instrument_closes.items():
            aligned_closes[label] = closes[inst_mask]

    # Slice signal closes to common dates
    for label in signal_closes:
        sig_mask = np.isin(
            signal_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        aligned_closes[label] = signal_closes[label][sig_mask]

    # Slice option_stream price closes to common dates
    for label in option_stream_closes:
        os_mask = np.isin(
            option_stream_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        aligned_closes[label] = option_stream_closes[label][os_mask]

    # Align each hold-mode option leg's RAW premium to common_dates (same mask as
    # its synthetic close) so the trade-log roll rows are sized/priced off the true
    # premium — DISPLAY-ONLY, never part of the equity curve.
    option_premium_aligned: dict[str, npt.NDArray[np.float64]] = {}
    for label, prem in option_premium_raw.items():
        if prem is None or label not in option_stream_dates_map:
            continue
        os_mask = np.isin(
            option_stream_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        option_premium_aligned[label] = np.asarray(prem, dtype=np.float64)[os_mask]

    # Align each futures_notional hold leg's reference-future price series to
    # common_dates with the SAME os_mask as the premium (it shares the option
    # leg's date axis).  DISPLAY-ONLY: it sizes the roll-row COUNT off the futures
    # notional; ``roll_future_ref`` is finite only at roll bars (NaN elsewhere),
    # which is exactly where each roll-row segment opens.
    option_future_ref_aligned: dict[str, npt.NDArray[np.float64]] = {}
    for label, fref in option_future_ref_raw.items():
        if fref is None or label not in option_stream_dates_map:
            continue
        os_mask = np.isin(
            option_stream_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        option_future_ref_aligned[label] = np.asarray(fref, dtype=np.float64)[os_mask]

    # Align each hold leg's roll-day OPEN premium to common_dates (same os_mask): a
    # premium-notional roll row's COUNT is sized off it (finite at roll bars, which
    # are exactly where each segment opens) rather than the daily held premium (NaN
    # at a far-OTM option's later segment opens).  DISPLAY-ONLY.
    option_roll_premium_aligned: dict[str, npt.NDArray[np.float64]] = {}
    for label, rprem in option_roll_premium_raw.items():
        if rprem is None or label not in option_stream_dates_map:
            continue
        os_mask = np.isin(
            option_stream_dates_map[label],
            common_dates,
            assume_unique=True,
        )
        option_roll_premium_aligned[label] = np.asarray(rprem, dtype=np.float64)[
            os_mask
        ]

    # ── 6. Compute full date range for the slider ──
    #
    # For instrument-only portfolios the full_date_range is the full
    # (unfiltered) instrument overlap.  For mixed or signal-only, we
    # combine all full-extent date arrays.

    full_date_grids: list[npt.NDArray[np.int64]] = []
    if instrument_full_dates is not None:
        full_date_grids.append(instrument_full_dates)
    for label in signal_dates_map:
        # Signal dates are already the full evaluation range (overlap_start
        # to overlap_end within each signal). Use them as-is.
        full_date_grids.append(signal_dates_map[label])
    for label in option_stream_dates_map:
        full_date_grids.append(option_stream_dates_map[label])

    full_common_all = full_date_grids[0]
    for grid in full_date_grids[1:]:
        full_common_all = np.intersect1d(
            full_common_all,
            grid,
            assume_unique=False,
        )
    full_start_iso = int_to_iso(int(full_common_all[0]))
    full_end_iso = int_to_iso(int(full_common_all[-1]))

    # ── 7. Compute portfolio ──

    # Filter weights to only include legs present in aligned_closes.
    # Level-mode option_stream legs are in tracking_series, not
    # aligned_closes, so they are naturally excluded.
    #
    # A hold-mode option price leg's synthetic already bakes in its direction
    # (sign of weight) and nav_times, exactly like a signal leg's synthetic — so
    # it enters the weighted portfolio with its |weight| as the SHARE.  Passing
    # the signed (negative) weight would let compute_weighted_portfolio re-short
    # an already-short curve (double-short); |weight| applies direction ONCE.
    portfolio_weights = {
        label: (
            abs(body.weights[label])
            if label in hold_option_labels
            else body.weights[label]
        )
        for label in aligned_closes
    }
    result = compute_weighted_portfolio(
        aligned_closes,
        portfolio_weights,
        rebalance_freq.value,
        body.return_type,
        common_dates,
    )

    # ── 8. Compute metrics ──

    # Risk stats must use the same return basis the equity curve was built
    # with (HIGH#3): a log-built curve's vol/Sharpe/Sortino are otherwise
    # computed on the wrong (simple-return) basis.
    metrics = compute_metrics(result.portfolio_equity, return_type=body.return_type)
    leg_metrics = {
        label: compute_metrics(eq, return_type=body.return_type)
        for label, eq in result.per_leg_equities.items()
    }

    # ── 9. Aggregate returns ──

    monthly = aggregate_returns(
        common_dates,
        result.portfolio_returns,
        result.per_leg_returns,
        body.return_type,
        "monthly",
    )
    yearly = aggregate_returns(
        common_dates,
        result.portfolio_returns,
        result.per_leg_returns,
        body.return_type,
        "yearly",
    )

    # ── 9.5. Re-surface roll boundaries for rolling direct legs (DISPLAY-ONLY) ──
    #
    # ``get_aligned_prices`` discards a continuous leg's roll_dates (keeps only the
    # stitched prices), so re-fetch each continuous leg via ``get_continuous`` to
    # recover the exact roll BOUNDARIES the chart markers use.  NOTE this is NOT a
    # free cache hit under a date filter: the equity path reaches ``get_continuous``
    # via ``get_aligned_prices`` → ``get_continuous(None, None)`` (unfiltered), but
    # here we pass ``start=start_date, end=end_date``; a non-None window is a
    # DIFFERENT cache key, so under a date filter this is a cache MISS — a full
    # contract re-roll per continuous leg (only the unfiltered case reuses the warm
    # entry).  It stays bounded (one extra roll per continuous leg) and DISPLAY-ONLY.
    # This is purely informational: a failure here must NEVER 500 the compute —
    # the equity/metrics are already built — so a data error degrades to "no roll
    # boundaries" (the leg then shows a single open→end row).
    continuous_roll_dates_interior: dict[str, list[int]] = {}
    for label, leg in body.legs.items():
        if leg.type != "continuous":
            continue
        spec = legs_spec.get(label)
        if not isinstance(spec, ContinuousLegSpec):
            continue
        try:
            cseries = await svc.get_continuous(
                spec.collection,
                spec.roll_config,
                start=start_date,
                end=end_date,
            )
        except Exception as exc:  # noqa: BLE001 — display-only; log, never fail compute
            logger.warning(
                "roll-boundary re-fetch failed for continuous leg %r (%s): %s",
                label,
                spec.collection,
                exc,
            )
            cseries = None
        if cseries is not None:
            continuous_roll_dates_interior[label] = [int(d) for d in cseries.roll_dates]

    # ── 10. Aggregate trades + per-input positions across signal legs ──
    #
    # Each signal leg evaluates against its own date overlap (per-signal
    # ``result.index``). Trade bar indices and positions price arrays are
    # therefore in that per-signal axis, NOT the portfolio's common_dates.
    # We re-map every trade endpoint onto common_dates via a date→index
    # dict; trades whose endpoints fall outside common_dates are DROPPED
    # (not clamped) — they refer to bars the user can't see in the
    # portfolio chart, so they'd index out of bounds on the frontend.

    cd_index: dict[int, int] = {int(d): i for i, d in enumerate(common_dates)}

    aggregated_trades: list[dict] = []
    for label, trades in signal_trades_map.items():
        sig_idx = signal_dates_map[label]
        # ``body.weights[label]`` is the user-facing PERCENT allocation
        # (frontend default 100). For trade-size scaling we need the
        # FRACTION form (0.0 … 1.0+) so ``signed_weight`` stays in
        # fraction units across direct + signal legs.
        leg_fraction = float(body.weights[label]) / 100.0
        for tr in trades:
            # Re-map the open bar (signal-axis index → common_dates index).
            # If the trade's open date isn't part of common_dates, DROP —
            # the trade can't be placed on the portfolio's date axis.
            sig_open_date = int(sig_idx[tr.open_bar])
            new_open = cd_index.get(sig_open_date)
            if new_open is None:
                continue
            if tr.close_bar is None:
                # Open trade: open date is in common_dates → keep with
                # close_bar=None. The frontend renders an effective close
                # price using the last finite value from positions[].
                # ``open_bar`` is NOT restricted to the signal's last bar;
                # the engine emits open trades wherever an entry block
                # latched and never closed (see engine
                # test_trades_open_at_end).
                new_close: int | None = None
            else:
                sig_close_date = int(sig_idx[tr.close_bar])
                mapped_close = cd_index.get(sig_close_date)
                if mapped_close is None:
                    continue
                new_close = mapped_close
            aggregated_trades.append(
                {
                    "input_id": tr.input_id,
                    "entry_block_id": tr.entry_block_id,
                    "entry_block_name": tr.entry_block_name,
                    "exit_block_id": tr.exit_block_id,
                    "exit_block_name": tr.exit_block_name,
                    "open_bar": new_open,
                    "close_bar": new_close,
                    "direction": tr.direction,
                    "signed_weight": tr.signed_weight * leg_fraction,
                    "holding_id": label,
                    "holding_name": label,
                }
            )

    # Direct legs have no engine trades; surface them in the trade log alongside
    # signal-leg trades.  ROLLING direct legs (continuous futures + hold-mode
    # option premia) emit ONE DISPLAY-ONLY row PER HELD CONTRACT (open / rolling…
    # / end — see ``_build_roll_rows``); NON-rolling legs (spot/index/ETF and
    # option LEVEL overlays) keep the single open "Holding" row exactly as before.
    # Both are built AFTER equity/metrics (§7-9), so they are purely informational.
    equity_arr = result.portfolio_equity
    n_common = int(len(common_dates))
    for label, leg in body.legs.items():
        if leg.type == "signal":
            continue
        # See note above: convert PERCENT allocation → FRACTION for the
        # trade's signed_weight (trades use fraction units uniformly).
        leg_fraction = float(body.weights[label]) / 100.0
        direction = "long" if leg_fraction >= 0 else "short"

        if leg.type == "continuous":
            aggregated_trades.extend(
                _build_roll_rows(
                    label=label,
                    input_id=leg.collection or label,
                    collection=leg.collection,
                    leg_fraction=leg_fraction,
                    direction=direction,
                    interior_roll_dates=continuous_roll_dates_interior.get(label, []),
                    price_series=aligned_closes.get(label),
                    equity=equity_arr,
                    cd_index=cd_index,
                    n_bars=n_common,
                )
            )
            continue

        if leg.type == "option_stream" and label in hold_option_labels:
            # The displayed contract COUNT follows the leg's sizing_mode: a
            # futures_notional leg is sized off the reference-future notional
            # (F_ref·m_fut), a premium leg off the premium notional.  Normalise
            # m_fut to a usable positive-finite float here (else None → the count
            # nulls via the shared guards, exactly like an unresolved OPT M).
            use_fn = leg.sizing_mode == "futures_notional"
            raw_mfut = option_mult_fut.get(label, float("nan"))
            usable_mfut = (
                float(raw_mfut) if math.isfinite(raw_mfut) and raw_mfut > 0.0 else None
            )
            aggregated_trades.extend(
                _build_roll_rows(
                    label=label,
                    input_id=label,
                    collection=leg.collection,
                    leg_fraction=leg_fraction,
                    direction=direction,
                    interior_roll_dates=option_roll_dates_interior.get(label, []),
                    price_series=option_premium_aligned.get(label),
                    equity=equity_arr,
                    cd_index=cd_index,
                    n_bars=n_common,
                    # Count basis: futures notional (F_ref·m_fut) when the leg is
                    # futures_notional, else the roll-day PREMIUM notional
                    # (roll_premium·M) — both finite at the segment opens; the daily
                    # held premium is NaN there for a far-OTM option.
                    sizing_price_series=(
                        option_future_ref_aligned.get(label)
                        if use_fn
                        else option_roll_premium_aligned.get(label)
                    ),
                    sizing_multiplier=usable_mfut if use_fn else None,
                    use_futures_notional=use_fn,
                    # Segment P&L basis: the leg's aligned synthetic equity, so the
                    # per-segment realised P&L is accumulator-derived (NaN-safe +
                    # correctly signed), NOT qty·Δpremium off the NaN daily premium.
                    pnl_series=aligned_closes.get(label),
                )
            )
            continue

        # Non-rolling direct leg (instrument / option LEVEL overlay): single row.
        direct_input_id = leg.symbol or label if leg.type == "instrument" else label
        aggregated_trades.append(
            {
                "input_id": direct_input_id,
                "entry_block_id": "holding",
                "entry_block_name": "Holding",
                "exit_block_id": None,
                "exit_block_name": None,
                "open_bar": 0,
                "close_bar": None,
                "direction": direction,
                "signed_weight": leg_fraction,
                "holding_id": label,
                "holding_name": label,
            }
        )

    aggregated_trades.sort(key=lambda t: (t["open_bar"], t["entry_block_id"]))

    # Build top-level positions payload (matches signals response shape).
    # First leg that references a given input_id wins; downstream conflicts
    # (same input_id, different prices across legs) are not expected and
    # would surface here.
    aggregated_positions: list[dict] = []
    seen_inputs: set[str] = set()
    for label, pos_list in signal_positions_map.items():
        sig_idx = signal_dates_map[label]
        # Projection from common_dates onto signal-bar indices: -1 marks
        # portfolio bars where the signal has no data (rendered as null).
        sig_index_of_date: dict[int, int] = {int(d): j for j, d in enumerate(sig_idx)}
        proj = [sig_index_of_date.get(int(d), -1) for d in common_dates]
        for pos in pos_list:
            iid = pos["input_id"]
            if iid in seen_inputs:
                continue
            seen_inputs.add(iid)
            price = pos.get("price")
            if price is None:
                aggregated_positions.append({"input_id": iid, "price": None})
                continue
            src_values = price["values"]
            remapped: list[float | None] = [
                (src_values[j] if j >= 0 else None) for j in proj
            ]
            aggregated_positions.append(
                {
                    "input_id": iid,
                    "price": {"label": price["label"], "values": remapped},
                }
            )

    # Direct (non-signal) leg price series → positions[]. Reuse the already-
    # aligned closes (length == len(common_dates)); first-leg-wins dedup.
    for label, leg in body.legs.items():
        if leg.type == "signal":
            continue
        if label not in aligned_closes:
            continue
        if leg.type == "instrument":
            direct_input_id = leg.symbol or label
            price_label = f"{leg.symbol}.close" if leg.symbol else f"{label}.close"
        elif leg.type == "continuous":
            direct_input_id = leg.collection or label
            price_label = (
                f"{leg.collection}.close" if leg.collection else f"{label}.close"
            )
        else:
            direct_input_id = label
            price_label = f"{label}.close"
        if direct_input_id in seen_inputs:
            continue
        seen_inputs.add(direct_input_id)
        aggregated_positions.append(
            {
                "input_id": direct_input_id,
                "price": {
                    "label": price_label,
                    "values": nan_safe_floats(aligned_closes[label]),
                },
            }
        )

    # ── 10.5. Per-trade sizing: fractional CONTRACT / ASSET count ──
    #
    # The trade "size" the FE shows is no longer the constant target % — it is
    # HOW MANY contracts (FUT/OPT) or shares (spot/equity) the |signed_weight|
    # allocation buys at the trade's open, off the 100-based equity index
    # treated as a $100 NAV (no initial_capital):
    #     quantity = |signed_weight| * NAV_open / (price_open * M)
    # NAV_open = portfolio_equity[open_bar]; price_open = the input's aligned
    # close at open_bar; M = the contract multiplier (FUT_→m_fut, OPT_→m_opt,
    # else 1.0). NaN-safe: a missing/≤0 price, an unresolved FUT/OPT M, or a
    # non-finite NAV → quantity=None (the FE falls back to the % display); the
    # terminal ``sanitize_json_floats`` pass nulls any residual non-finite.
    # Collection is resolved PER TRADE: direct legs carry it on the LegSpec;
    # signal-leg trades use the per-input collection threaded from signal eval
    # (``input_id`` is the remapped underlying — a bare symbol for spot inputs,
    # not always a collection). Direct OPTION legs are nulled here: their positions
    # "price" is the synthetic 100-based equity, not a tradeable premium, so a
    # contract count off it would mislead.  ROLL rows (continuous + hold-option)
    # are already sized in ``_build_roll_rows`` off the leg's OWN price/premium
    # series — SKIP them here so their quantity/multiplier are not overwritten.
    equity = result.portfolio_equity
    n_bars = int(len(equity))
    price_by_input: dict[str, list | None] = {
        p["input_id"]: (p["price"]["values"] if p.get("price") else None)
        for p in aggregated_positions
    }
    for tr in aggregated_trades:
        if tr.get("_roll_row"):
            continue
        leg = body.legs.get(tr["holding_id"])
        if leg is not None and leg.type == "signal":
            collection = signal_collections_map.get(tr["holding_id"], {}).get(
                tr["input_id"]
            )
        elif leg is not None:
            collection = leg.collection
        else:
            collection = None

        m, unit = _leg_multiplier_and_unit(collection)
        tr["quantity_unit"] = unit
        tr["multiplier"] = m

        open_bar = tr["open_bar"]
        values = price_by_input.get(tr["input_id"])
        price_open = (
            values[open_bar]
            if values is not None and 0 <= open_bar < len(values)
            else None
        )
        nav_open = float(equity[open_bar]) if 0 <= open_bar < n_bars else math.nan
        is_direct_option = leg is not None and leg.type == "option_stream"
        if (
            is_direct_option
            or m is None
            or price_open is None
            or not math.isfinite(float(price_open))
            or float(price_open) <= 0.0
            or not math.isfinite(nav_open)
        ):
            tr["quantity"] = None
        else:
            tr["quantity"] = (
                abs(tr["signed_weight"]) * nav_open / (float(price_open) * m)
            )

    # Drop the internal roll-row marker; it is a build-time flag, not response data.
    for tr in aggregated_trades:
        tr.pop("_roll_row", None)

    # ── 11. Build response ──

    dates_iso = [int_to_iso(int(d)) for d in common_dates]

    response = {
        "dates": dates_iso,
        "portfolio_equity": result.portfolio_equity.tolist(),
        "leg_equities": {
            label: eq.tolist() for label, eq in result.per_leg_equities.items()
        },
        "raw_leg_equities": {
            label: eq.tolist() for label, eq in result.raw_leg_equities.items()
        },
        "rebalance_dates": [int_to_iso(int(d)) for d in result.rebalance_dates],
        "metrics": asdict(metrics),
        "leg_metrics": {label: asdict(m) for label, m in leg_metrics.items()},
        "monthly_returns": monthly,
        "yearly_returns": yearly,
        "date_range": {"start": dates_iso[0], "end": dates_iso[-1]},
        "full_date_range": {"start": full_start_iso, "end": full_end_iso},
        "rebalance": rebalance_freq.value,
        "return_type": body.return_type,
        "tracking_series": tracking_series,
        "trades": aggregated_trades,
        "positions": aggregated_positions,
    }

    # RFC-8259 finite-JSON invariant: NaN / +inf / -inf are NOT valid JSON, so
    # the WHOLE payload is passed through ``sanitize_json_floats`` (every
    # non-finite float -> null) in one recursive pass. Degenerate inputs can
    # poison many blocks at once — an all-NaN leg or a zero-price bar reaches
    # ``portfolio_equity`` / ``leg_equities`` / ``raw_leg_equities``, and the
    # ``nan_safe_floats`` price/tracking blocks let ``inf`` through by design —
    # so sanitizing block-by-block is leak-prone. A single terminal pass is the
    # backstop regardless of how each block was built or what the response
    # renderer's NaN policy is. The engine ALSO holds non-finite bars flat at
    # the source (so curves are correct, not merely nulled), but this is the
    # last line that makes the invariant total. (#6)
    return sanitize_json_floats(response)
