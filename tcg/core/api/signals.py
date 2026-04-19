"""Signals router — evaluate a user-defined Signal spec against market data.

Exposes:

* ``POST /api/signals/compute`` — evaluate a Signal over its referenced
  Indicators and Instruments and return per-timestep
  ``position``/``long_score``/``short_score`` vectors plus entry/exit
  index lists.

Signals are OR-of-AND rule blocks across four directions. Indicator
specs live in browser localStorage, so the request carries every
referenced indicator spec inline; the backend executes them via
:func:`tcg.engine.indicator_exec.run_indicator`.
"""

from __future__ import annotations

from datetime import date
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
from tcg.types.signal import (
    Block,
    CompareCondition,
    Condition,
    ConstantOperand,
    CrossCondition,
    InRangeCondition,
    IndicatorOperand,
    InstrumentOperand,
    Operand,
    RollingCondition,
    Signal,
    SignalRules,
)

router = APIRouter(prefix="/api/signals", tags=["signals"])


# ---------------------------------------------------------------------------
# Error envelope (matches /api/indicators/compute verbatim)
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
# Pydantic request models (raw JSON shape from the frontend)
# ---------------------------------------------------------------------------


class _OperandIn(BaseModel):
    """Generic operand; discriminated by ``kind`` at parse time."""

    kind: Literal["indicator", "instrument", "constant"]
    # indicator
    indicator_id: str | None = None
    output: str = "default"
    # instrument
    collection: str | None = None
    instrument_id: str | None = None
    field: str = "close"
    # constant
    value: float | None = None


class _ConditionIn(BaseModel):
    op: str
    # Compare / cross
    lhs: _OperandIn | None = None
    rhs: _OperandIn | None = None
    # In-range
    operand: _OperandIn | None = None
    min: _OperandIn | None = None
    max: _OperandIn | None = None
    # Rolling
    lookback: int | None = None


class _BlockIn(BaseModel):
    conditions: list[_ConditionIn] = Field(default_factory=list)


class _SignalRulesIn(BaseModel):
    long_entry: list[_BlockIn] = Field(default_factory=list)
    long_exit: list[_BlockIn] = Field(default_factory=list)
    short_entry: list[_BlockIn] = Field(default_factory=list)
    short_exit: list[_BlockIn] = Field(default_factory=list)


class _SignalIn(BaseModel):
    id: str = ""
    name: str = ""
    rules: _SignalRulesIn = Field(default_factory=_SignalRulesIn)


class _SeriesRefIn(BaseModel):
    collection: str
    instrument_id: str


class _IndicatorSpecIn(BaseModel):
    code: str
    params: dict[str, int | float | bool] = Field(default_factory=dict)
    # Frontend sends camelCase; accept both forms.
    seriesMap: dict[str, _SeriesRefIn] = Field(default_factory=dict)


class SignalComputeRequest(BaseModel):
    spec: _SignalIn
    indicators: dict[str, _IndicatorSpecIn] = Field(default_factory=dict)
    # Reserved for future inline instrument bundles (v1: unused).
    instruments: dict[str, Any] = Field(default_factory=dict)
    start: str | None = None
    end: str | None = None


# ---------------------------------------------------------------------------
# JSON → typed Signal conversion
# ---------------------------------------------------------------------------


_COMPARE_OPS = {"gt", "lt", "ge", "le", "eq"}
_CROSS_OPS = {"cross_above", "cross_below"}
_ROLLING_OPS = {"rolling_gt", "rolling_lt"}


def _parse_operand(op_in: _OperandIn | None, *, path: str) -> Operand:
    if op_in is None:
        raise SignalValidationError(f"{path}: operand required")
    if op_in.kind == "indicator":
        if not op_in.indicator_id:
            raise SignalValidationError(
                f"{path}: indicator operand requires 'indicator_id'"
            )
        return IndicatorOperand(
            indicator_id=op_in.indicator_id, output=op_in.output
        )
    if op_in.kind == "instrument":
        if not op_in.collection or not op_in.instrument_id:
            raise SignalValidationError(
                f"{path}: instrument operand requires 'collection' and "
                f"'instrument_id'"
            )
        return InstrumentOperand(
            collection=op_in.collection,
            instrument_id=op_in.instrument_id,
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
        out.append(Block(conditions=conds))
    return tuple(out)


def _parse_signal(raw: _SignalIn) -> Signal:
    rules = SignalRules(
        long_entry=_parse_blocks(raw.rules.long_entry, direction="long_entry"),
        long_exit=_parse_blocks(raw.rules.long_exit, direction="long_exit"),
        short_entry=_parse_blocks(
            raw.rules.short_entry, direction="short_entry"
        ),
        short_exit=_parse_blocks(raw.rules.short_exit, direction="short_exit"),
    )
    return Signal(id=raw.id, name=raw.name, rules=rules)


# ---------------------------------------------------------------------------
# Price fetcher adapter
# ---------------------------------------------------------------------------


def _make_fetcher(
    svc: MarketDataService,
    start: date | None,
    end: date | None,
) -> Any:
    """Adapt MarketDataService.get_prices to the (coll, id, field) → (dates, values) shape."""

    async def fetch(
        collection: str, instrument_id: str, field: str
    ) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
        try:
            series = await svc.get_prices(
                collection, instrument_id, start=start, end=end
            )
        except DataNotFoundError as exc:
            raise SignalDataError(
                f"instrument {collection}/{instrument_id}: {exc}"
            ) from exc
        if series is None:
            raise SignalDataError(
                f"instrument '{instrument_id}' not found in collection "
                f"'{collection}'"
            )
        if field == "close":
            values = series.close
        elif field == "open":
            values = series.open
        elif field == "high":
            values = series.high
        elif field == "low":
            values = series.low
        elif field == "volume":
            values = series.volume
        else:
            raise SignalValidationError(
                f"instrument field {field!r} is not supported; "
                f"expected one of close/open/high/low/volume"
            )
        return series.dates, values.astype(np.float64, copy=False)

    return fetch


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/compute")
async def compute_signal(
    body: SignalComputeRequest,
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Evaluate a Signal spec and return per-timestep scores + positions."""

    # ── 1. Parse dates ──
    try:
        start_date = date.fromisoformat(body.start) if body.start else None
        end_date = date.fromisoformat(body.end) if body.end else None
    except ValueError as exc:
        return _error_response("validation", f"Invalid date format: {exc}")

    # ── 2. Parse / translate spec ──
    try:
        signal = _parse_signal(body.spec)
    except SignalValidationError as exc:
        return _error_response("validation", str(exc))

    indicators: dict[str, IndicatorSpecInput] = {}
    for ind_id, ind_spec in body.indicators.items():
        indicators[ind_id] = IndicatorSpecInput(
            code=ind_spec.code,
            params=dict(ind_spec.params),
            series_map={
                label: (ref.collection, ref.instrument_id)
                for label, ref in ind_spec.seriesMap.items()
            },
        )

    # ── 3. Evaluate ──
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

    # ── 4. Build response ──
    # Render NaN as JSON null, matching /api/indicators/compute.
    def _nan_safe(arr: npt.NDArray[np.float64]) -> list[float | None]:
        return [None if (v != v) else float(v) for v in arr.tolist()]

    index_iso = [
        f"{int_to_iso(int(d))}T00:00:00Z" for d in result.index.tolist()
    ]

    # ``price`` — first instrument operand's resolved series (NaN→null).
    # ``None`` when the signal references no instrument operand; the
    # frontend falls back to a position-only chart in that case.
    if result.price_label is None or result.price_values is None:
        price_payload: dict | None = None
    else:
        price_payload = {
            "label": result.price_label,
            "values": _nan_safe(result.price_values),
        }

    return {
        "index": index_iso,
        "position": _nan_safe(result.position),
        "long_score": _nan_safe(result.long_score),
        "short_score": _nan_safe(result.short_score),
        "entries_long": result.entries_long,
        "exits_long": result.exits_long,
        "entries_short": result.entries_short,
        "exits_short": result.exits_short,
        "price": price_payload,
    }


__all__ = ["router", "SignalComputeRequest"]
