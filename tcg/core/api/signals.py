"""Signals router -- evaluate a user-defined Signal spec against market data.

v4 -- unified Entries/Exits, signed weights, per-target-entry clearing.

Request::

    {
      "spec": {
        "id", "name",
        "inputs": [
          { "id": "X", "instrument": {
              "type": "spot", "collection", "instrument_id"
          } }
        ],
        "rules": {
          "entries": [Block],
          "exits":   [Block]
        }
      },
      "indicators": IndicatorSpec[]
    }

Where ``Block = {id, input_id, weight, conditions,
target_entry_block_id?}``. ``weight`` is a signed percentage in
``[-100, +100]``; sign decides long/short. ``id`` is a stable
frontend-generated UUID. ``target_entry_block_id`` is REQUIRED on
exits and FORBIDDEN on entries, and must reference an existing entry
block id within the same signal.

Response — per-input positions + per-block events::

    {
      "timestamps": number[],
      "positions": [
        {
          "input_id": str,
          "instrument": {type, collection, instrument_id?|adjustment+cycle+...},
          "values":        float[],           // signed net position per bar
          "clipped_mask":  bool[],            // always false (leverage allowed)
          "price":         {label, values} | null
        }
      ],
      "realized_pnl": float[][],              // per-input cumulative pct return
      "events": [
        {
          "input_id":     str,
          "block_id":     str,                // the frontend-supplied UUID
          "kind":         "entry"|"exit",
          "fired_indices":   int[],           // bars where AND-condition fired
          "latched_indices": int[],           // "effective" bars (see below)
          "active_indices":  int[],           // entries only: bars with latch open
          "target_entry_block_id": str | null
        }
      ],
      "indicators": [
        {"input_id", "indicator_id", "series": (float|null)[]}
      ],
      "clipped":     bool,                    // always false (leverage allowed)
      "diagnostics": { ... }
    }

``latched_indices`` on an entry block = bars where its latch
transitioned False→True (i.e. the bar the user would label as an
"entry"). ``latched_indices`` on an exit = bars where this exit
actually closed a previously-open entry latch (i.e. "effective exit").
Frontend uses these directly to compute the "don't repeat
entries/exits" filter.

Error envelope unchanged: ``{error_type, message, traceback?}``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from tcg.core.api._adapters import build_roll_config
from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import (
    ContinuousInstrumentRef,
    SpotInstrumentRef,
)
from tcg.core.api._serializers import nan_safe_floats
from tcg.core.api.common import error_response, get_market_data
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


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class _InputIn(BaseModel):
    id: str
    # Pydantic v2 discriminated union on ``type``.
    instrument: SpotInstrumentRef | ContinuousInstrumentRef = Field(
        discriminator="type"
    )


class _OperandIn(BaseModel):
    kind: Literal["indicator", "instrument", "constant"]
    # indicator
    indicator_id: str | None = None
    output: str = "default"
    params_override: dict[str, Any] | None = None
    # series_override maps label -> input_id
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
    """v4 block: stable ``id`` + ``input_id`` + signed ``weight``.

    On exit blocks ``target_entry_block_id`` references the entry
    block whose latch this exit clears. Entries must leave it unset.
    """

    id: str = ""
    conditions: list[_ConditionIn] = Field(default_factory=list)
    input_id: str = ""
    weight: float = 0.0
    target_entry_block_id: str | None = None


class _SignalRulesIn(BaseModel):
    entries: list[_BlockIn] = Field(default_factory=list)
    exits: list[_BlockIn] = Field(default_factory=list)


class SignalIn(BaseModel):
    id: str = ""
    name: str = ""
    inputs: list[_InputIn] = Field(default_factory=list)
    rules: _SignalRulesIn = Field(default_factory=_SignalRulesIn)


class _SeriesRefIn(BaseModel):
    collection: str
    instrument_id: str


class IndicatorSpecIn(BaseModel):
    id: str
    name: str = ""
    code: str
    params: dict[str, int | float | bool] = Field(default_factory=dict)
    seriesMap: dict[str, _SeriesRefIn] = Field(default_factory=dict)
    ownPanel: bool = False


class SignalComputeRequest(BaseModel):
    spec: SignalIn
    indicators: list[IndicatorSpecIn] = Field(default_factory=list)
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
    if isinstance(inst_in, SpotInstrumentRef):
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
    blocks: list[_BlockIn],
    *,
    section: str,
    is_entry: bool,
    entry_ids: set[str] | None = None,
) -> tuple[Block, ...]:
    """Parse request-shape blocks into typed :class:`Block` tuples.

    Validates v4 invariants:
      * entries: ``target_entry_block_id`` must be unset; ``weight`` is
        a signed percentage in ``[-100, +100]`` and ``!= 0``.
      * exits: ``target_entry_block_id`` is required and must reference
        an id in ``entry_ids``.
      * both: ``id`` is required (non-empty) on any block that has at
        least one condition. Empty-id + empty-conditions blocks are
        the "placeholder" state from the UI and are passed through as
        sentinels (the engine will skip them).

    Entry ids must be unique within the signal's entries list.
    """
    out: list[Block] = []
    seen_entry_ids: set[str] = set()
    for i, blk in enumerate(blocks):
        path = f"rules.{section}[{i}]"
        conds = tuple(
            _parse_condition(c, path=f"{path}.conditions[{j}]")
            for j, c in enumerate(blk.conditions)
        )
        bid = blk.id or ""
        iid = blk.input_id or ""
        weight = float(blk.weight)
        tgt = blk.target_entry_block_id or None

        # Placeholder blocks (no conditions + no input) are accepted
        # as sentinels; they skip evaluation. Otherwise full validation.
        placeholder = (
            not conds and not iid and not bid
            and weight == 0.0 and tgt is None
        )
        if not placeholder:
            if not bid:
                raise SignalValidationError(
                    f"{path}: block id is required"
                )
            if is_entry:
                if tgt is not None:
                    raise SignalValidationError(
                        f"{path}: entry blocks must not set "
                        f"'target_entry_block_id'"
                    )
                if weight == 0.0:
                    raise SignalValidationError(
                        f"{path}: entry block weight must be non-zero "
                        f"(signed percentage in [-100, +100])"
                    )
                if abs(weight) > 100.0:
                    raise SignalValidationError(
                        f"{path}: entry block weight {weight!r} out of "
                        f"range; expected signed percentage in [-100, +100]"
                    )
                if bid in seen_entry_ids:
                    raise SignalValidationError(
                        f"{path}: duplicate entry block id {bid!r}"
                    )
                seen_entry_ids.add(bid)
            else:
                if not tgt:
                    raise SignalValidationError(
                        f"{path}: exit blocks require "
                        f"'target_entry_block_id'"
                    )
                assert entry_ids is not None
                if tgt not in entry_ids:
                    raise SignalValidationError(
                        f"{path}: target_entry_block_id {tgt!r} does not "
                        f"reference any entry block id in this signal's "
                        f"rules; declared entry ids: "
                        f"{sorted(entry_ids)!r}"
                    )

        out.append(
            Block(
                id=bid,
                conditions=conds,
                input_id=iid,
                weight=weight,
                target_entry_block_id=tgt,
            )
        )
    return tuple(out)


def parse_signal(raw: SignalIn) -> Signal:
    inputs = tuple(_parse_input(i) for i in raw.inputs)
    entries = _parse_blocks(
        raw.rules.entries, section="entries", is_entry=True,
    )
    entry_ids: set[str] = {b.id for b in entries if b.id}
    exits = _parse_blocks(
        raw.rules.exits,
        section="exits",
        is_entry=False,
        entry_ids=entry_ids,
    )
    rules = SignalRules(entries=entries, exits=exits)
    return Signal(id=raw.id, name=raw.name, inputs=inputs, rules=rules)


# ---------------------------------------------------------------------------
# Input date-range overlap — restrict evaluation to common timeframe
# ---------------------------------------------------------------------------


async def compute_input_overlap(
    svc: MarketDataService,
    signal: Signal,
    start: date | None,
    end: date | None,
) -> tuple[date | None, date | None]:
    """Pre-fetch all input instruments and return the overlapping date range.

    Returns ``(start, end)`` clamped to the intersection of all inputs'
    date ranges so the engine only evaluates bars where every input is
    defined — analogous to the portfolio page's aligned-price logic.
    """
    if len(signal.inputs) <= 1:
        return start, end

    date_arrays: list[npt.NDArray[np.int64]] = []
    for inp in signal.inputs:
        inst = inp.instrument
        match type(inst).__name__:
            case "InstrumentSpot":
                try:
                    series = await svc.get_prices(
                        inst.collection,  # type: ignore[union-attr]
                        inst.instrument_id,  # type: ignore[union-attr]
                        start=start,
                        end=end,
                    )
                except DataNotFoundError as exc:
                    raise SignalDataError(
                        f"input {inp.id!r}: {exc}"
                    ) from exc
                date_arrays.append(series.dates)
            case "InstrumentContinuous":
                try:
                    roll_config = build_roll_config(
                        inst.adjustment,  # type: ignore[union-attr]
                        inst.cycle,  # type: ignore[union-attr]
                        inst.roll_offset,  # type: ignore[union-attr]
                    )
                except ValueError as exc:
                    raise SignalDataError(
                        f"input {inp.id!r}: {exc}"
                    ) from exc
                try:
                    cseries = await svc.get_continuous(
                        inst.collection,  # type: ignore[union-attr]
                        roll_config,
                        start=start,
                        end=end,
                    )
                except DataNotFoundError as exc:
                    raise SignalDataError(
                        f"input {inp.id!r}: {exc}"
                    ) from exc
                date_arrays.append(cseries.prices.dates)
            case _:
                raise SignalDataError(
                    f"input {inp.id!r}: unsupported instrument type"
                )

    # Intersect all date arrays to find the common range.
    common = date_arrays[0]
    for arr in date_arrays[1:]:
        common = np.intersect1d(common, arr, assume_unique=False)

    if common.size == 0:
        raise SignalDataError(
            "no overlapping dates across inputs — "
            "the selected instruments have disjoint date ranges"
        )

    # Convert int dates (YYYYMMDD) to stdlib date for the fetcher bounds.
    lo = int(common[0])
    hi = int(common[-1])
    overlap_start = date(lo // 10000, (lo % 10000) // 100, lo % 100)
    overlap_end = date(hi // 10000, (hi % 10000) // 100, hi % 100)

    return overlap_start, overlap_end


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
            roll_config = build_roll_config(
                instrument.adjustment,
                instrument.cycle,
                instrument.roll_offset,
            )
        except ValueError as exc:
            raise SignalValidationError(
                f"continuous input: {exc}"
            ) from exc
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
    """Evaluate a v4 Signal and return per-input positions + events."""

    try:
        start_date, end_date = parse_iso_range(body.start, body.end)
    except ValueError as exc:
        return error_response("validation", str(exc))

    try:
        signal = parse_signal(body.spec)
    except SignalValidationError as exc:
        return error_response("validation", str(exc))

    indicators: dict[str, IndicatorSpecInput] = {}
    for ind_spec in body.indicators:
        if ind_spec.id in indicators:
            return error_response(
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

    # --- compute the biggest overlap of all input date ranges ----------
    try:
        overlap_start, overlap_end = await compute_input_overlap(
            svc, signal, start_date, end_date,
        )
    except SignalDataError as exc:
        return error_response("data", str(exc))

    fetcher = make_signal_fetcher(svc, overlap_start, overlap_end)
    try:
        result = await evaluate_signal(signal, indicators, fetcher)
    except SignalValidationError as exc:
        return error_response("validation", str(exc))
    except SignalDataError as exc:
        return error_response("data", str(exc))
    except SignalRuntimeError as exc:
        return error_response(
            "runtime", str(exc), traceback=exc.user_traceback or None
        )

    timestamps = [
        _int_yyyymmdd_to_unix_ms(int(d)) for d in result.index.tolist()
    ]

    positions_out: list[dict] = []
    realized_pnl_out: list[list[float]] = []
    for p in result.positions:
        if p.price_label is None or p.price_values is None:
            price_payload: dict | None = None
        else:
            price_payload = {
                "label": p.price_label,
                "values": nan_safe_floats(p.price_values),
            }
        positions_out.append(
            {
                "input_id": p.input_id,
                "instrument": _instrument_payload(p.instrument),
                "values": nan_safe_floats(p.values),
                "clipped_mask": [bool(x) for x in p.clipped_mask.tolist()],
                "price": price_payload,
            }
        )
        # realized_pnl is nan-safe by construction (0 on nan steps).
        realized_pnl_out.append([float(v) for v in p.realized_pnl.tolist()])

    events_out: list[dict] = []
    for ev in result.events:
        events_out.append(
            {
                "input_id": ev.input_id,
                "block_id": ev.block_id,
                "kind": ev.kind,
                "fired_indices": [int(i) for i in ev.fired_indices],
                "latched_indices": [int(i) for i in ev.latched_indices],
                "active_indices": [int(i) for i in ev.active_indices],
                "target_entry_block_id": ev.target_entry_block_id,
            }
        )

    indicator_own_panel: dict[str, bool] = {
        spec.id: spec.ownPanel for spec in body.indicators
    }
    indicators_out: list[dict] = []
    for ind in result.indicator_series:
        indicators_out.append(
            {
                "input_id": ind.input_id,
                "indicator_id": ind.indicator_id,
                "series": nan_safe_floats(ind.series),
                "ownPanel": indicator_own_panel.get(ind.indicator_id, False),
            }
        )

    return {
        "timestamps": timestamps,
        "positions": positions_out,
        "realized_pnl": realized_pnl_out,
        "events": events_out,
        "indicators": indicators_out,
        "clipped": bool(result.clipped),
        "diagnostics": dict(result.diagnostics),
    }


__all__ = [
    "router",
    "SignalComputeRequest",
    "SignalIn",
    "IndicatorSpecIn",
    "parse_signal",
    "make_signal_fetcher",
    "compute_input_overlap",
]
