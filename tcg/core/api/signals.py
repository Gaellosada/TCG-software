"""Signals router -- evaluate a user-defined Signal spec against market data.

Exposes:

* ``POST /api/signals/compute`` -- evaluate a Signal over its referenced
  Indicators and Instruments and return per-instrument position series
  plus a global clipping flag.

v2 request/response (iter-3) -- see PLAN.md §Authoritative v2 contract:

Request::

    {
      "spec":       Signal,            // { id, name, rules }
      "indicators": IndicatorSpec[]    // each entry has required ``id``
    }

Response::

    {
      "timestamps": number[],          // unix ms, union-aligned
      "positions": [
        {
          "instrument": { "collection", "instrument_id" },
          "values":        float[],
          "clipped_mask":  bool[],
          "price": { "label", "values" } | null
        }
      ],
      "clipped":     bool,
      "diagnostics": { ... }
    }

Error envelope (unchanged):
``{error_type, message, traceback?}``.
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
from tcg.types.signal import (
    Block,
    CompareCondition,
    Condition,
    ConstantOperand,
    CrossCondition,
    InRangeCondition,
    IndicatorOperand,
    InstrumentOperand,
    InstrumentRef,
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
    # v2: indicator operand per-use overrides
    params_override: dict[str, Any] | None = None
    series_override: dict[str, str] | None = None
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


class _InstrumentRefIn(BaseModel):
    collection: str
    instrument_id: str


class _BlockIn(BaseModel):
    """v2 block: top-level ``instrument`` + unsigned ``weight``."""

    conditions: list[_ConditionIn] = Field(default_factory=list)
    instrument: _InstrumentRefIn | None = None
    weight: float = 0.0


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
    # PLAN.md §Authoritative v2 contract pins IndicatorSpec = {id, name, code,
    # params, seriesMap}. ``id`` is required (used as lookup key by the
    # evaluator) and ``name`` is optional metadata shipped by the frontend.
    id: str
    name: str = ""
    code: str
    params: dict[str, int | float | bool] = Field(default_factory=dict)
    # Frontend sends camelCase; accept both forms.
    seriesMap: dict[str, _SeriesRefIn] = Field(default_factory=dict)


class SignalComputeRequest(BaseModel):
    spec: _SignalIn
    # PLAN.md §Request body: indicators is an ARRAY of IndicatorSpec. Each
    # entry's ``id`` is the handler-side lookup key; duplicate ids are
    # rejected as a validation error (see _indicators_by_id below).
    indicators: list[_IndicatorSpecIn] = Field(default_factory=list)
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
            indicator_id=op_in.indicator_id,
            output=op_in.output,
            params_override=(
                dict(op_in.params_override) if op_in.params_override else None
            ),
            series_override=(
                dict(op_in.series_override) if op_in.series_override else None
            ),
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
        instrument: InstrumentRef | None = None
        if blk.instrument is not None:
            instrument = InstrumentRef(
                collection=blk.instrument.collection,
                instrument_id=blk.instrument.instrument_id,
            )
        out.append(
            Block(
                conditions=conds,
                instrument=instrument,
                weight=float(blk.weight),
            )
        )
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


def _int_yyyymmdd_to_unix_ms(d: int) -> int:
    """Convert a YYYYMMDD int to a UTC unix-ms timestamp at 00:00:00."""
    iso = int_to_iso(int(d))  # "YYYY-MM-DD"
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _nan_safe(arr: npt.NDArray[np.float64] | None) -> list[float | None]:
    if arr is None:
        return []
    return [None if (v != v) else float(v) for v in arr.tolist()]


@router.post("/compute")
async def compute_signal(
    body: SignalComputeRequest,
    svc: MarketDataService = Depends(get_market_data),
) -> dict:
    """Evaluate a Signal spec and return per-instrument positions + clip flag."""

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

    # PLAN.md pins the request body's ``indicators`` field as an array of
    # IndicatorSpec objects, each with a required ``id``. The evaluator
    # still wants a lookup table keyed by id, so we fold the list into a
    # dict here and surface duplicate ids as a validation error (rather
    # than letting one entry silently clobber the other).
    indicators: dict[str, IndicatorSpecInput] = {}
    for ind_spec in body.indicators:
        if ind_spec.id in indicators:
            return _error_response(
                "validation",
                f"duplicate indicator id {ind_spec.id!r} in request body",
            )
        indicators[ind_spec.id] = IndicatorSpecInput(
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

    # ── 4. Build v2 response ──
    # timestamps: union-aligned unix-milliseconds. Empty list when the
    # spec references no series (degenerate all-constants case).
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
                "instrument": {
                    "collection": p.instrument.collection,
                    "instrument_id": p.instrument.instrument_id,
                },
                "values": _nan_safe(p.values),
                "clipped_mask": [bool(x) for x in p.clipped_mask.tolist()],
                "price": price_payload,
            }
        )

    return {
        "timestamps": timestamps,
        "positions": positions_out,
        "clipped": bool(result.clipped),
        "diagnostics": dict(result.diagnostics),
    }


__all__ = ["router", "SignalComputeRequest"]
