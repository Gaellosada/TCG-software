"""Signals router -- evaluate a user-defined Signal spec against market data.

v3 (iter-4) -- named inputs

Request::

    {
      "spec": {
        "id", "name",
        "inputs": [
          { "id": "X", "instrument": {
              "type": "spot", "collection", "instrument_id"
          } },
          { "id": "Y", "instrument": {
              "type": "continuous", "collection", "adjustment",
              "cycle", "rollOffset", "strategy"
          } }
        ],
        "rules": { long_entry, long_exit, short_entry, short_exit }
      },
      "indicators": IndicatorSpec[]
    }

Blocks have ``{input_id, weight, conditions}``. Instrument operands have
``{kind:'instrument', input_id, field}``. Indicator operands have
``{kind:'indicator', indicator_id, input_id, params_override,
series_override}`` where ``series_override`` maps ``label -> input_id``.

Response (shape-compatible with iter-3, with per-input entries keyed by
``input_id`` + ``instrument`` discriminated):

    {
      "timestamps": number[],
      "positions": [
        {
          "input_id": str,
          "instrument": {type, collection, instrument_id?|adjustment+cycle+...},
          "values":        float[],
          "clipped_mask":  bool[],
          "price":         {label, values} | null
        }
      ],
      "indicators": [],            // reserved, array for shape stability
      "clipped":     bool,
      "diagnostics": { ... }
    }

Error envelope unchanged: ``{error_type, message, traceback?}``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from tcg.core.api.data import get_market_data
from tcg.data._utils import int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine.signal_exec import (
    IndicatorSpecInput,
    SignalDataError,
    SignalRuntimeError,
    SignalValidationError,
    evaluate_signal,
)
from tcg.types.errors import DataNotFoundError
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy
from tcg.types.signal import (
    Block,
    CompareCondition,
    Condition,
    ConstantOperand,
    CrossCondition,
    IndicatorOperand,
    InRangeCondition,
    Input,
    InputInstrument,
    InstrumentContinuous,
    InstrumentOperand,
    InstrumentSpot,
    Operand,
    RollingCondition,
    Signal,
    SignalRules,
)

router = APIRouter(prefix="/api/signals", tags=["signals"])


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def _error_response(
    error_type: str,
    message: str,
    *,
    status: int = 400,
    traceback: str | None = None,
) -> JSONResponse:
    content: dict = {"error_type": error_type, "message": message}
    if traceback:
        content["traceback"] = traceback
    return JSONResponse(status_code=status, content=content)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class _SpotInstrumentIn(BaseModel):
    type: Literal["spot"]
    collection: str
    instrument_id: str


class _ContinuousInstrumentIn(BaseModel):
    type: Literal["continuous"]
    collection: str
    adjustment: Literal["none", "proportional", "difference"] = "none"
    cycle: str | None = None
    # Accept camelCase from the frontend.
    rollOffset: int = 0
    strategy: Literal["front_month"] = "front_month"


class _InputIn(BaseModel):
    id: str
    # Pydantic v2 discriminated union on ``type``.
    instrument: _SpotInstrumentIn | _ContinuousInstrumentIn = Field(
        discriminator="type"
    )


class _OperandIn(BaseModel):
    kind: Literal["indicator", "instrument", "constant"]
    # indicator
    indicator_id: str | None = None
    output: str = "default"
    params_override: dict[str, Any] | None = None
    # v3: series_override maps label -> input_id (str)
    series_override: dict[str, str] | None = None
    # instrument + indicator
    input_id: str | None = None
    # instrument
    field: str = "close"
    # constant
    value: float | None = None


class _ConditionIn(BaseModel):
    op: str
    lhs: _OperandIn | None = None
    rhs: _OperandIn | None = None
    operand: _OperandIn | None = None
    min: _OperandIn | None = None
    max: _OperandIn | None = None
    lookback: int | None = None


class _BlockIn(BaseModel):
    """v3 block: ``input_id`` + unsigned ``weight``."""

    conditions: list[_ConditionIn] = Field(default_factory=list)
    input_id: str = ""
    weight: float = 0.0


class _SignalRulesIn(BaseModel):
    long_entry: list[_BlockIn] = Field(default_factory=list)
    long_exit: list[_BlockIn] = Field(default_factory=list)
    short_entry: list[_BlockIn] = Field(default_factory=list)
    short_exit: list[_BlockIn] = Field(default_factory=list)


class _SignalIn(BaseModel):
    id: str = ""
    name: str = ""
    inputs: list[_InputIn] = Field(default_factory=list)
    rules: _SignalRulesIn = Field(default_factory=_SignalRulesIn)


class _SeriesRefIn(BaseModel):
    collection: str
    instrument_id: str


class _IndicatorSpecIn(BaseModel):
    id: str
    name: str = ""
    code: str
    params: dict[str, int | float | bool] = Field(default_factory=dict)
    seriesMap: dict[str, _SeriesRefIn] = Field(default_factory=dict)


class SignalComputeRequest(BaseModel):
    spec: _SignalIn
    indicators: list[_IndicatorSpecIn] = Field(default_factory=list)
    instruments: dict[str, Any] = Field(default_factory=dict)
    start: str | None = None
    end: str | None = None


# ---------------------------------------------------------------------------
# JSON → typed Signal conversion
# ---------------------------------------------------------------------------


_COMPARE_OPS = {"gt", "lt", "ge", "le", "eq"}
_CROSS_OPS = {"cross_above", "cross_below"}
_ROLLING_OPS = {"rolling_gt", "rolling_lt"}


def _parse_input(inp_in: _InputIn) -> Input:
    iid = inp_in.id
    if not iid:
        raise SignalValidationError("input id must be non-empty")
    inst_in = inp_in.instrument
    if isinstance(inst_in, _SpotInstrumentIn):
        if not inst_in.collection or not inst_in.instrument_id:
            raise SignalValidationError(
                f"input {iid!r}: spot instrument requires collection + instrument_id"
            )
        instrument: InputInstrument = InstrumentSpot(
            collection=inst_in.collection,
            instrument_id=inst_in.instrument_id,
        )
    else:
        if not inst_in.collection:
            raise SignalValidationError(
                f"input {iid!r}: continuous instrument requires collection"
            )
        instrument = InstrumentContinuous(
            collection=inst_in.collection,
            adjustment=inst_in.adjustment,
            cycle=inst_in.cycle,
            roll_offset=int(inst_in.rollOffset),
            strategy=inst_in.strategy,
        )
    return Input(id=iid, instrument=instrument)


def _parse_operand(op_in: _OperandIn | None, *, path: str) -> Operand:
    if op_in is None:
        raise SignalValidationError(f"{path}: operand required")
    if op_in.kind == "indicator":
        if not op_in.indicator_id:
            raise SignalValidationError(
                f"{path}: indicator operand requires 'indicator_id'"
            )
        if not op_in.input_id:
            raise SignalValidationError(
                f"{path}: indicator operand requires 'input_id'"
            )
        return IndicatorOperand(
            indicator_id=op_in.indicator_id,
            input_id=op_in.input_id,
            output=op_in.output,
            params_override=(
                dict(op_in.params_override) if op_in.params_override else None
            ),
            series_override=(
                dict(op_in.series_override) if op_in.series_override else None
            ),
        )
    if op_in.kind == "instrument":
        if not op_in.input_id:
            raise SignalValidationError(
                f"{path}: instrument operand requires 'input_id'"
            )
        return InstrumentOperand(
            input_id=op_in.input_id,
            field=op_in.field or "close",
        )
    if op_in.kind == "constant":
        if op_in.value is None:
            raise SignalValidationError(
                f"{path}: constant operand requires 'value'"
            )
        return ConstantOperand(value=float(op_in.value))
    raise SignalValidationError(f"{path}: unknown operand kind {op_in.kind!r}")


def _parse_condition(c: _ConditionIn, *, path: str) -> Condition:
    op = c.op
    if op in _COMPARE_OPS:
        return CompareCondition(
            op=op,  # type: ignore[arg-type]
            lhs=_parse_operand(c.lhs, path=f"{path}.lhs"),
            rhs=_parse_operand(c.rhs, path=f"{path}.rhs"),
        )
    if op in _CROSS_OPS:
        return CrossCondition(
            op=op,  # type: ignore[arg-type]
            lhs=_parse_operand(c.lhs, path=f"{path}.lhs"),
            rhs=_parse_operand(c.rhs, path=f"{path}.rhs"),
        )
    if op == "in_range":
        return InRangeCondition(
            op="in_range",
            operand=_parse_operand(c.operand, path=f"{path}.operand"),
            min=_parse_operand(c.min, path=f"{path}.min"),
            max=_parse_operand(c.max, path=f"{path}.max"),
        )
    if op in _ROLLING_OPS:
        if c.lookback is None or c.lookback < 1:
            raise SignalValidationError(
                f"{path}: '{op}' requires integer 'lookback' >= 1"
            )
        return RollingCondition(
            op=op,  # type: ignore[arg-type]
            operand=_parse_operand(c.operand, path=f"{path}.operand"),
            lookback=int(c.lookback),
        )
    raise SignalValidationError(f"{path}: unknown op {op!r}")


def _parse_blocks(
    blocks: list[_BlockIn], *, direction: str
) -> tuple[Block, ...]:
    out: list[Block] = []
    for i, blk in enumerate(blocks):
        conds = tuple(
            _parse_condition(c, path=f"{direction}[{i}].conditions[{j}]")
            for j, c in enumerate(blk.conditions)
        )
        out.append(
            Block(
                conditions=conds,
                input_id=blk.input_id or "",
                weight=float(blk.weight),
            )
        )
    return tuple(out)


def _parse_signal(raw: _SignalIn) -> Signal:
    inputs = tuple(_parse_input(i) for i in raw.inputs)
    rules = SignalRules(
        long_entry=_parse_blocks(raw.rules.long_entry, direction="long_entry"),
        long_exit=_parse_blocks(raw.rules.long_exit, direction="long_exit"),
        short_entry=_parse_blocks(
            raw.rules.short_entry, direction="short_entry"
        ),
        short_exit=_parse_blocks(raw.rules.short_exit, direction="short_exit"),
    )
    return Signal(id=raw.id, name=raw.name, inputs=inputs, rules=rules)


# ---------------------------------------------------------------------------
# Price fetcher adapter — dispatches on InputInstrument kind
# ---------------------------------------------------------------------------


_ADJ_MAP: dict[str, AdjustmentMethod] = {
    "none": AdjustmentMethod.NONE,
    "proportional": AdjustmentMethod.PROPORTIONAL,
    "difference": AdjustmentMethod.DIFFERENCE,
}


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


def _make_fetcher(
    svc: MarketDataService,
    start: date | None,
    end: date | None,
) -> Any:
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

        # continuous
        try:
            strategy = RollStrategy.FRONT_MONTH
        except AttributeError:  # pragma: no cover — RollStrategy shape guard
            raise SignalValidationError(
                "continuous input: RollStrategy.FRONT_MONTH unavailable"
            )
        adj = _ADJ_MAP.get(instrument.adjustment)
        if adj is None:
            raise SignalValidationError(
                f"continuous input: unknown adjustment {instrument.adjustment!r}"
            )
        roll_config = ContinuousRollConfig(
            strategy=strategy,
            adjustment=adj,
            cycle=instrument.cycle or None,
            roll_offset_days=int(instrument.roll_offset),
        )
        try:
            cseries = await svc.get_continuous(
                instrument.collection,
                roll_config,
                start=start,
                end=end,
            )
        except DataNotFoundError as exc:
            raise SignalDataError(
                f"continuous {instrument.collection}: {exc}"
            ) from exc
        if cseries is None:
            raise SignalDataError(
                f"continuous series unavailable for {instrument.collection!r}"
            )
        values = _pick_field(cseries.prices, field)
        return cseries.prices.dates, values

    return fetch


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _int_yyyymmdd_to_unix_ms(d: int) -> int:
    iso = int_to_iso(int(d))
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _nan_safe(arr: npt.NDArray[np.float64] | None) -> list[float | None]:
    if arr is None:
        return []
    return [None if (v != v) else float(v) for v in arr.tolist()]


def _instrument_payload(inst: InputInstrument) -> dict:
    if isinstance(inst, InstrumentSpot):
        return {
            "type": "spot",
            "collection": inst.collection,
            "instrument_id": inst.instrument_id,
        }
    return {
        "type": "continuous",
        "collection": inst.collection,
        "adjustment": inst.adjustment,
        "cycle": inst.cycle,
        "rollOffset": int(inst.roll_offset),
        "strategy": inst.strategy,
    }


@router.post("/compute")
async def compute_signal(
    body: SignalComputeRequest,
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Evaluate a v3 Signal and return per-input positions + clip flag."""

    try:
        start_date = date.fromisoformat(body.start) if body.start else None
        end_date = date.fromisoformat(body.end) if body.end else None
    except ValueError as exc:
        return _error_response("validation", f"Invalid date format: {exc}")

    try:
        signal = _parse_signal(body.spec)
    except SignalValidationError as exc:
        return _error_response("validation", str(exc))

    indicators: dict[str, IndicatorSpecInput] = {}
    for ind_spec in body.indicators:
        if ind_spec.id in indicators:
            return _error_response(
                "validation",
                f"duplicate indicator id {ind_spec.id!r} in request body",
            )
        # Preserve declaration order via seriesMap insertion order.
        series_labels = tuple(ind_spec.seriesMap.keys())
        indicators[ind_spec.id] = IndicatorSpecInput(
            code=ind_spec.code,
            params=dict(ind_spec.params),
            series_labels=series_labels,
            series_map={
                label: (ref.collection, ref.instrument_id)
                for label, ref in ind_spec.seriesMap.items()
            },
        )

    fetcher = _make_fetcher(svc, start_date, end_date)
    try:
        result = await evaluate_signal(signal, indicators, fetcher)
    except SignalValidationError as exc:
        return _error_response("validation", str(exc))
    except SignalDataError as exc:
        return _error_response("data", str(exc))
    except SignalRuntimeError as exc:
        return _error_response(
            "runtime", str(exc), traceback=exc.user_traceback or None
        )

    timestamps = [
        _int_yyyymmdd_to_unix_ms(int(d)) for d in result.index.tolist()
    ]

    positions_out: list[dict] = []
    for p in result.positions:
        if p.price_label is None or p.price_values is None:
            price_payload: dict | None = None
        else:
            price_payload = {
                "label": p.price_label,
                "values": _nan_safe(p.price_values),
            }
        positions_out.append(
            {
                "input_id": p.input_id,
                "instrument": _instrument_payload(p.instrument),
                "values": _nan_safe(p.values),
                "clipped_mask": [bool(x) for x in p.clipped_mask.tolist()],
                "price": price_payload,
            }
        )

    return {
        "timestamps": timestamps,
        "positions": positions_out,
        "indicators": [],
        "clipped": bool(result.clipped),
        "diagnostics": dict(result.diagnostics),
    }


__all__ = ["router", "SignalComputeRequest"]
