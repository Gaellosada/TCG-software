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

Where ``Block = {id, name, input_id, weight, conditions,
target_entry_block_names?}``. ``weight`` is a signed percentage in
``[-100, +100]``; sign decides long/short. ``id`` is a stable
frontend-generated UUID. ``name`` is a user-editable string used by
exits to reference entries. ``target_entry_block_names`` (list) holds
one or more entry names and is REQUIRED on exits (≥1) and FORBIDDEN on
entries; every name must reference an existing entry block name within
the same signal (cross-input targets are allowed). The legacy singular
``target_entry_block_name`` (string) is still accepted and normalised to
a one-element list. ``input_id`` is REQUIRED on entries and FORBIDDEN on
exits — an exit's operating inputs are derived from its targeted
entries' ``input_id`` values at validation time.

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
      "realized_pnl": float[][],              // per-input cumulative CONTRIBUTION
                                              //   (fraction of starting capital) to
                                              //   the one compounded account; Σ over
                                              //   inputs == equity_ratio - 1 (to
                                              //   floating-point tolerance)
      "equity_ratio": float[],                // capital-free compounded equity curve,
                                              //   starts 1.0, = Π(1 + Σ_i pos_i·r_i),
                                              //   clamped at 0 on wipeout (one account,
                                              //   net exposure). equity = capital·ratio
      "events": [
        {
          "input_id":     str,
          "block_id":     str,                // the frontend-supplied UUID
          "kind":         "entry"|"exit",
          "fired_indices":   int[],           // bars where AND-condition fired
          "latched_indices": int[],           // "effective" bars (see below)
          "active_indices":  int[],           // entries only: bars with latch open
          "target_entry_block_names": str[]   // exits: targeted entry names; [] otherwise
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

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from tcg.core.api._dates import parse_iso_range
from tcg.core.api._models import (
    BasketRefInline,
    BasketRefSaved,
    OptionStreamRef,
    SeriesRef,
    SpotInstrumentRef,
)
from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.api._series_fetch import (
    _date_array_for_leaf_instrument,
    _has_option_stream_dependency,
    _materialise_leg_instrument,
    _saved_basket_leg_to_typed,
    basket_leg_date_intersection,
    make_signal_fetcher,
)
from tcg.core.api._serializers import nan_safe_floats
from tcg.core.api.common import error_response, get_market_data
from tcg.persistence import WriteRepository
from tcg.data._utils import int_to_iso
from tcg.data.protocols import MarketDataService
from tcg.engine.signal_exec import (
    IndicatorSpecInput,
    SignalDataError,
    SignalRuntimeError,
    SignalValidationError,
    evaluate_signal,
)
from tcg.types.persistence import BasketDoc, DocType
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
    InstrumentBasket,
    InstrumentContinuous,
    InstrumentOperand,
    InstrumentOptionStream,
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
    # Discriminated union shared with the indicators router, routed by
    # the callable discriminator in ``_models.py``.  Tags collapse the
    # outer ``type`` and inner ``kind`` (basket-branch only) into a
    # single flat tag space, which keeps OpenAPI 3.0 emission valid.
    instrument: SeriesRef
    # Optional per-input net-position clamp ``[low, high]`` in FRACTION units
    # (Feature 1: long-or-flat / capped net exposure). Deliberately typed
    # ``Any`` (not ``list[float]``) so a malformed value reaches
    # ``_parse_input``'s validator and yields the uniform HTTP-400 envelope
    # instead of a Pydantic 422. ``None``/absent ⇒ no clamp (byte-identical).
    position_cap: Any = None


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
    # cross_count extension (cross_above / cross_below only). Defaults
    # reproduce today's single-bar crossover byte-identically; both must be
    # integers >= 1 when supplied (validated in ``_parse_condition``).
    # Deliberately typed ``Any`` (not ``int``) so the raw value reaches
    # ``_parse_condition``'s isinstance guards unchanged — Pydantic must NOT
    # coerce ``1.5`` → int (422) or ``true`` → 1 (silent 200) before we
    # can emit the uniform HTTP-400 validation envelope.
    count: Any = None
    window: Any = None
    # Count MODE for ``count`` (cross conditions only): "rolling" (default —
    # trailing-window count, byte-identical) or "since_reset" (cumulative count
    # since the owning block's bound reset fires; impulse on the Nth crossing).
    # Typed ``Any`` so a bad value routes through ``_parse_condition``'s guard to
    # the uniform HTTP-400 envelope. Absent ⇒ "rolling".
    count_mode: Any = None


class _BlockIn(BaseModel):
    """v4 block: stable ``id`` + ``name`` + ``input_id`` + signed ``weight``.

    On exit blocks ``target_entry_block_names`` references the entry
    blocks (by name) whose latches this exit clears — one or more, and
    they may span multiple inputs. Entries must leave it empty.

    Legacy ``target_entry_block_name`` (singular string) is still
    accepted on the wire for backward compatibility. The two encodings
    are reconciled by SILENT normalisation, never by a conflict check: if
    the plural key is present (even as an explicit empty list) it wins and
    the singular is dropped; otherwise a non-empty singular is normalised
    to a one-element list. Sending both keys is therefore accepted — the
    singular is simply ignored whenever the plural is present, with no
    rejection on disagreement (intentional back-compat behaviour).
    """

    id: str = ""
    name: str = ""
    conditions: list[_ConditionIn] = Field(default_factory=list)
    input_id: str = ""
    weight: float = 0.0
    # New canonical exit-target field (one or more entry names).
    target_entry_block_names: list[str] | None = None
    # Legacy singular exit-target field — accepted for backward
    # compatibility and normalised into ``target_entry_block_names`` at
    # parse time (plural wins when both present).
    target_entry_block_name: str | None = None
    enabled: bool = True
    description: str = ""
    # Per-block reset binding (entries/exits only). When non-empty,
    # references a reset block's ``id`` in the same signal's
    # ``rules.resets``. Validated at parse time after resets are parsed.
    requires_reset_block_id: str | None = None
    # Cumulative re-arm count for the binding above (entries/exits only).
    # Validated at parse time (integer >= 1; rejected on reset blocks).
    # Default 1 reproduces the original single-flip re-arm.
    requires_reset_count: int = 1
    # DEPRECATED (v4): kept so Pydantic does not silently drop it; API
    # validation rejects any request that sets this field. Remove once
    # no legacy clients remain (target: v5 or 2026-Q3).
    target_entry_block_id: str | None = None
    # Optional temporal chain (entries/exits only — rejected on resets).
    # Maps SUCCESSOR condition index (as a string key on the wire — JSON object
    # keys are strings — or an int) to the ``within`` window in BARS.
    #
    # The value type is deliberately ``Any`` (not ``int``): a ``dict[str, int]``
    # annotation makes Pydantic the de-facto window validator — it rejects a
    # null/str/float window with a 422 (bypassing our uniform HTTP-400 envelope)
    # AND silently coerces ``true`` → ``1`` (``bool`` subclasses ``int``),
    # accepting a nonsense window. Keeping it permissive routes EVERY malformed
    # window through ``_parse_links`` (the authoritative validator) which raises
    # ``SignalValidationError`` → HTTP 400 ``error_type='validation'``. Validated
    # in ``_parse_blocks``: finite int windows >= 1, single contiguous forward
    # chain over indices 1..len-1, no nesting. ``None``/empty ⇒ zero-link CNF.
    links: dict[str, Any] | None = None


class _SignalRulesIn(BaseModel):
    entries: list[_BlockIn] = Field(default_factory=list)
    exits: list[_BlockIn] = Field(default_factory=list)
    resets: list[_BlockIn] = Field(default_factory=list)


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


@dataclass
class _ResolvedBasketInput:
    """Internal carrier for a basket input that has been pre-resolved.

    Replaces a :class:`_InputIn` whose ``instrument`` is a
    :class:`BasketRef` once it has been turned into a typed
    leaf-instrument leg list.  Each leg is an already-built
    :class:`InstrumentSpot` / :class:`InstrumentContinuous` /
    :class:`InstrumentOptionStream` paired with its signed weight.

    For *saved* baskets the legs come from materialising the persisted
    ``BasketDoc.legs`` polymorphic dicts; for *inline* baskets they
    come from dispatching each :class:`BasketLeg` on the wire.  Both
    shapes converge here so :func:`_parse_input` can build an
    :class:`InstrumentBasket` without any I/O.

    Exactly one of ``basket_id`` / ``asset_class`` is set:

    * ``basket_id is not None and asset_class is None`` — saved basket.
    * ``basket_id is None and asset_class is not None`` — inline basket.
    """

    id: str
    legs: tuple[
        tuple[
            "InstrumentSpot | InstrumentContinuous | InstrumentOptionStream",
            float,
        ],
        ...,
    ]
    basket_id: str | None = None
    asset_class: str | None = None
    # Feature 1 per-input net-position clamp, carried through basket resolution
    # (raw wire value; validated in ``_parse_input`` like the non-basket path).
    position_cap: Any = None


async def _resolve_basket_inputs(
    raw_inputs: list[_InputIn],
    repo: WriteRepository,
    svc: MarketDataService,
) -> list[_InputIn | _ResolvedBasketInput]:
    """Pre-resolve every basket ref into a typed-leg snapshot.

    * Saved basket → fetch ``BasketDoc`` via ``repo``, dispatch each
      persisted leg's ``instrument`` sub-dict through
      :func:`_materialise_leg_instrument`.
    * Inline basket → dispatch each :class:`BasketLeg` directly
      through :func:`_materialise_leg_instrument` (no DB read).
    * Non-basket inputs pass through untouched.

    Short-circuit (Q6, preserved from iter 1): if no input has
    ``kind == "saved"``, the repo is never consulted — inline-only
    compute requests work even when the persistence layer is
    unreachable.

    Raises :class:`SignalValidationError` when a saved basket id is
    unknown, when a saved basket has no legs, or when a leg's
    persisted shape doesn't match the basket's declared
    ``asset_class``.
    """
    # Short-circuit: avoid repo reads when no input is a saved basket.
    any_saved = any(isinstance(inp.instrument, BasketRefSaved) for inp in raw_inputs)
    _ = svc  # not consulted under the polymorphic-leg flow; kept on the
    # signature so the saved-basket short-circuit invariant call site
    # in tests doesn't shift.
    out: list[_InputIn | _ResolvedBasketInput] = []
    for inp in raw_inputs:
        if isinstance(inp.instrument, BasketRefSaved):
            assert any_saved  # repo must be live for at least one read
            basket_id = inp.instrument.basket_id
            doc = await repo.get_by_id(DocType.BASKET.value, basket_id)
            if doc is None or not isinstance(doc, BasketDoc):
                raise SignalValidationError(
                    f"input {inp.id!r}: basket {basket_id!r} not found"
                )
            if not doc.legs:
                raise SignalValidationError(
                    f"input {inp.id!r}: basket {basket_id!r} has no legs"
                )
            typed_legs = tuple(
                _saved_basket_leg_to_typed(
                    leg,
                    basket_id=basket_id,
                    leg_index=i,
                    asset_class=doc.asset_class,
                )
                for i, leg in enumerate(doc.legs)
            )
            out.append(
                _ResolvedBasketInput(
                    id=inp.id,
                    basket_id=basket_id,
                    legs=typed_legs,
                    position_cap=inp.position_cap,
                )
            )
        elif isinstance(inp.instrument, BasketRefInline):
            inline = inp.instrument
            typed_legs = tuple(
                (
                    _materialise_leg_instrument(
                        leg.instrument, input_id=inp.id, leg_index=i
                    ),
                    float(leg.weight),
                )
                for i, leg in enumerate(inline.legs)
            )
            out.append(
                _ResolvedBasketInput(
                    id=inp.id,
                    legs=typed_legs,
                    asset_class=inline.asset_class,
                    position_cap=inp.position_cap,
                )
            )
        else:
            out.append(inp)
    return out


def _parse_position_cap(raw: Any, *, iid: str) -> tuple[float, float] | None:
    """Validate a wire ``position_cap`` → ``(low, high)`` fraction tuple or None.

    Accepts ``None``/absent (no clamp). Otherwise requires a 2-element
    list/tuple of finite real numbers with ``low <= high``. All failures raise
    :class:`SignalValidationError` (uniform HTTP-400 envelope). ``bool`` is
    rejected (it subclasses ``int`` — a ``True`` bound is almost certainly a
    client bug).
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise SignalValidationError(
            f"input {iid!r}: position_cap must be a [low, high] pair (got {raw!r})"
        )
    vals: list[float] = []
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise SignalValidationError(
                f"input {iid!r}: position_cap bounds must be numbers (got {raw!r})"
            )
        xf = float(x)
        if not np.isfinite(xf):
            raise SignalValidationError(
                f"input {iid!r}: position_cap bounds must be finite (got {raw!r})"
            )
        vals.append(xf)
    lo_cap, hi_cap = vals
    if lo_cap > hi_cap:
        raise SignalValidationError(
            f"input {iid!r}: position_cap low ({lo_cap}) must be <= high ({hi_cap})"
        )
    return (lo_cap, hi_cap)


def _parse_input(inp_in: _InputIn | _ResolvedBasketInput) -> Input:
    iid = inp_in.id
    if not iid:
        raise SignalValidationError("input id must be non-empty")
    cap = _parse_position_cap(inp_in.position_cap, iid=iid)
    # Pre-resolved basket — typed legs already materialised by
    # ``_resolve_basket_inputs``.  No I/O performed here.
    if isinstance(inp_in, _ResolvedBasketInput):
        instrument: InputInstrument = InstrumentBasket(
            legs=inp_in.legs,
            basket_id=inp_in.basket_id,
            asset_class=inp_in.asset_class,
        )
        return Input(id=iid, instrument=instrument, position_cap=cap)
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
    elif isinstance(inst_in, OptionStreamRef):
        if not inst_in.collection:
            raise SignalValidationError(
                f"input {iid!r}: option_stream instrument requires collection"
            )
        # Lazy imports to avoid circular deps (same pattern as indicators.py)
        from tcg.core.api.options import (
            _criterion_pydantic_to_dataclass,
            _maturity_pydantic_to_dataclass,
            _roll_offset_pydantic_to_dataclass,
        )

        # Reject tautological: by_delta selection + delta stream
        if (
            hasattr(inst_in.selection, "kind")
            and inst_in.selection.kind == "by_delta"
            and inst_in.stream == "delta"
        ):
            raise SignalValidationError(
                f"input {iid!r}: by_delta selection with delta stream is tautological"
            )

        maturity = _maturity_pydantic_to_dataclass(inst_in.maturity)
        selection = _criterion_pydantic_to_dataclass(inst_in.selection)
        instrument = InstrumentOptionStream(
            collection=inst_in.collection,
            option_type=inst_in.option_type,
            cycle=inst_in.cycle,
            maturity=maturity,
            selection=selection,
            stream=inst_in.stream,
            roll_offset=_roll_offset_pydantic_to_dataclass(inst_in.roll_offset),
            hold_between_rolls=inst_in.hold_between_rolls,
            nav_times=inst_in.nav_times,
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
    return Input(id=iid, instrument=instrument, position_cap=cap)


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
            raise SignalValidationError(f"{path}: constant operand requires 'value'")
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
        # Absent field (not in model_fields_set) → use default 1 (byte-identical
        # single-bar crossover).  Explicitly supplied value (incl. null/None) →
        # must pass the integer >= 1 guard below; explicit null is malformed.
        count = 1 if "count" not in c.model_fields_set else c.count
        window = 1 if "window" not in c.model_fields_set else c.window
        # Reject None (explicit null), bool (subclasses int), non-int, and
        # out-of-range values loudly with the uniform HTTP-400 envelope.
        # Defaults (count=1, window=1) reproduce today's single-bar crossover.
        if (
            count is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise SignalValidationError(
                f"{path}: '{op}' count must be an integer >= 1 (got {c.count!r})"
            )
        if (
            window is None
            or isinstance(window, bool)
            or not isinstance(window, int)
            or window < 1
        ):
            raise SignalValidationError(
                f"{path}: '{op}' window must be an integer >= 1 (got {c.window!r})"
            )
        # count_mode: absent ⇒ "rolling" (byte-identical). Only "rolling" and
        # "since_reset" are valid; anything else is a loud HTTP-400.
        count_mode = (
            "rolling" if "count_mode" not in c.model_fields_set else c.count_mode
        )
        if count_mode not in ("rolling", "since_reset"):
            raise SignalValidationError(
                f"{path}: '{op}' count_mode must be 'rolling' or 'since_reset' "
                f"(got {c.count_mode!r})"
            )
        return CrossCondition(
            op=op,  # type: ignore[arg-type]
            lhs=_parse_operand(c.lhs, path=f"{path}.lhs"),
            rhs=_parse_operand(c.rhs, path=f"{path}.rhs"),
            count=count,
            window=window,
            count_mode=count_mode,  # type: ignore[arg-type]
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


def _parse_links(
    raw_links: dict[str, Any] | None, n_conditions: int, *, path: str
) -> dict[int, int] | None:
    """Validate and normalise a block's temporal ``links`` (HTTP 400 on error).

    Enforces the bounded-state invariants (G3) at the API layer:
      * keys parse as integers in ``1..n_conditions-1`` (no index 0 — the head
        carries no link; no out-of-range index);
      * windows are integers ``>= 1`` (finite, positive — reject None/missing
        and ``<= 0``; a window of 0 would be a non-link, so it is rejected at
        authoring to keep the chain unambiguous);
      * the keys form ONE contiguous forward chain covering EVERY successor —
        exactly ``{1, 2, ..., n_conditions-1}`` (linear-chain-only, no gaps,
        no nesting, spans the whole block).

    Returns the normalised ``dict[int, int]`` (int keys) or ``None`` when no
    links are supplied (empty/absent ⇒ zero-link CNF).
    """
    if not raw_links:
        return None
    out: dict[int, int] = {}
    for raw_key, raw_win in raw_links.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            raise SignalValidationError(
                f"{path}: links key {raw_key!r} is not an integer condition index"
            )
        if isinstance(raw_win, bool) or not isinstance(raw_win, int):
            raise SignalValidationError(
                f"{path}: links window for index {key} must be an integer (got {raw_win!r})"
            )
        if raw_win < 1:
            raise SignalValidationError(
                f"{path}: links window for index {key} must be >= 1 "
                f"(a window of 0 is a non-link; omit it instead)"
            )
        if key < 1 or key >= n_conditions:
            raise SignalValidationError(
                f"{path}: links key {key} out of range; valid successor "
                f"indices are 1..{n_conditions - 1}"
            )
        if key in out:
            raise SignalValidationError(f"{path}: duplicate links key {key}")
        out[key] = raw_win
    expected = set(range(1, n_conditions))
    if set(out) != expected:
        raise SignalValidationError(
            f"{path}: links must form one contiguous forward chain over "
            f"every condition — expected successor indices "
            f"{sorted(expected)!r}, got {sorted(out)!r}"
        )
    return out


def _parse_blocks(
    blocks: list[_BlockIn],
    *,
    section: str,
    is_entry: bool,
    entry_names: set[str] | None = None,
    is_reset: bool = False,
    reset_ids: set[str] | None = None,
) -> tuple[Block, ...]:
    """Parse request-shape blocks into typed :class:`Block` tuples.

    Validates v4 invariants:
      * entries: ``target_entry_block_names`` must be unset; ``weight``
        is a signed percentage in ``[-100, +100]`` and ``!= 0``.
        Non-empty ``name`` values must be unique across entries.
      * exits: ``target_entry_block_names`` is required (≥1 name), every
        name must reference a name in ``entry_names``, and names must be
        unique within the exit. The legacy singular
        ``target_entry_block_name`` is accepted and normalised.
      * both: ``id`` is required (non-empty) on any block that has at
        least one condition. Empty-id + empty-conditions blocks are
        the "placeholder" state from the UI and are passed through as
        sentinels (the engine will skip them).

    Entry ids must be unique within the signal's entries list.
    """
    out: list[Block] = []
    seen_entry_ids: set[str] = set()
    seen_entry_names: set[str] = set()
    for i, blk in enumerate(blocks):
        path = f"rules.{section}[{i}]"
        conds = tuple(
            _parse_condition(c, path=f"{path}.conditions[{j}]")
            for j, c in enumerate(blk.conditions)
        )
        bid = blk.id or ""
        name = blk.name or ""
        iid = blk.input_id or ""
        weight = float(blk.weight)
        # Normalise exit targets: plural key wins when present, else fall
        # back to the legacy singular (one-element list), else empty. The
        # plural may be an explicit empty list — that still counts as
        # "the plural key was supplied" so the singular is NOT consulted.
        if blk.target_entry_block_names is not None:
            tgt_names: tuple[str, ...] = tuple(blk.target_entry_block_names)
        elif blk.target_entry_block_name:
            tgt_names = (blk.target_entry_block_name,)
        else:
            tgt_names = ()
        # ``has_target`` flags whether a NON-EMPTY exit target was supplied
        # (by either key) — used by the placeholder / reset / entry checks
        # that must reject a target on the wrong block kind. It keys off the
        # truthiness of BOTH encodings symmetrically: an explicit empty
        # plural list (``[]``) is treated the SAME as an empty/absent
        # singular (no target supplied), so the two encodings behave
        # identically on entry/reset blocks. (Exits separately require ≥1
        # target via the ``not tgt_names`` check below, so an empty list on
        # an exit is still rejected there, not here.)
        has_target = bool(blk.target_entry_block_names) or bool(
            blk.target_entry_block_name
        )
        legacy_tgt = blk.target_entry_block_id or None
        rrb = blk.requires_reset_block_id or None
        rrc = blk.requires_reset_count
        raw_links = blk.links or None
        # Validated temporal chain (entries/exits only). Stays None for
        # placeholders and resets (resets reject non-empty links above).
        parsed_links: dict[int, int] | None = None

        # Placeholder blocks (no conditions + no input) are accepted
        # as sentinels; they skip evaluation. Otherwise full validation.
        placeholder = (
            not conds and not iid and not bid and weight == 0.0 and not has_target
        )
        if not placeholder:
            if not bid:
                raise SignalValidationError(f"{path}: block id is required")
            if is_reset:
                # Reset blocks are signal-global: they must NOT carry
                # entry/exit-only fields. We reject (rather than silently
                # strip) so malformed payloads surface clearly. Error
                # messages are part of the LOCKED API contract — do not
                # paraphrase.
                if iid:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set input_id"
                    )
                if weight != 0.0:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set weight"
                    )
                if has_target:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set target_entry_block_name"
                    )
                if legacy_tgt is not None:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set target_entry_block_name"
                    )
                # Reset blocks are the binding TARGETS — they cannot
                # themselves bind to another reset. Reject loudly to
                # surface malformed payloads.
                if blk.requires_reset_block_id is not None:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set requires_reset_block_id"
                    )
                # ``requires_reset_count`` is meaningless on a reset block
                # (counting lives on the entry/exit binder). The default 1
                # is tolerated; any explicit non-default is rejected loudly,
                # mirroring the requires_reset_block_id rejection above.
                if rrc != 1:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set requires_reset_count"
                    )
                # Temporal chains are forbidden on reset blocks: a sequence
                # reset would break the per-True-bar re-arm countdown (silent
                # strategy inertness). Mirror the input_id/weight rejection.
                if raw_links:
                    raise SignalValidationError(
                        f"{path}: reset blocks must not set links"
                    )
            elif is_entry:
                if has_target:
                    raise SignalValidationError(
                        f"{path}: entry blocks must not set 'target_entry_block_name'"
                    )
                if legacy_tgt is not None:
                    raise SignalValidationError(
                        f"{path}: entry blocks must not set "
                        f"'target_entry_block_id' (legacy field removed)"
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
                if name:
                    if name in seen_entry_names:
                        raise SignalValidationError(
                            f"{path}: duplicate entry block name {name!r} "
                            f"— entry names must be unique within a signal"
                        )
                    seen_entry_names.add(name)
            else:
                # Exit blocks must NOT carry a block-level input_id: the
                # operating input is derived from the target entry.
                # Rejecting non-empty values enforces the "one source of
                # truth" invariant; silently stripping would hide bugs.
                if iid:
                    raise SignalValidationError(
                        f"{path}: exit blocks must not set 'input_id' — "
                        f"the operating input is derived from the target "
                        f"entry's input_id"
                    )
                if legacy_tgt is not None:
                    raise SignalValidationError(
                        f"{path}: exit blocks must use "
                        f"'target_entry_block_names' (list of strings), not "
                        f"the removed 'target_entry_block_id' (uuid)"
                    )
                if not tgt_names:
                    raise SignalValidationError(
                        f"{path}: exit blocks require at least one "
                        f"'target_entry_block_name' (use "
                        f"'target_entry_block_names')"
                    )
                # Reject duplicate target names within a single exit — an
                # exit clearing the same entry twice is a malformed
                # payload (the second clear is always a no-op).
                seen_targets: set[str] = set()
                for tname in tgt_names:
                    if tname in seen_targets:
                        raise SignalValidationError(
                            f"{path}: duplicate target_entry_block_name "
                            f"{tname!r} in exit block — each target must "
                            f"appear at most once"
                        )
                    seen_targets.add(tname)
                assert entry_names is not None
                # Every target must reference a declared entry name. Reject
                # dangling names loudly (engine tolerates them, API does
                # not) so the user sees the typo immediately.
                for tname in tgt_names:
                    if tname not in entry_names:
                        raise SignalValidationError(
                            f"{path}: target_entry_block_name {tname!r} "
                            f"does not match any entry block name in this "
                            f"signal's rules; declared entry names: "
                            f"{sorted(entry_names)!r}"
                        )
            # Per-block reset binding (entries+exits only). The
            # ``is_reset`` branch already rejects non-None values; here
            # we enforce type + cross-reference against the signal's
            # reset ids (collected by ``parse_signal`` before this call).
            if not is_reset and rrb is not None:
                if not isinstance(rrb, str) or not rrb:
                    raise SignalValidationError(
                        f"{path}: requires_reset_block_id must be a "
                        f"non-empty string or null"
                    )
                if reset_ids is not None and rrb not in reset_ids:
                    raise SignalValidationError(
                        f"{path}: requires_reset_block_id {rrb!r} does "
                        f"not match any reset block id in this signal's "
                        f"rules.resets"
                    )
            # Per-block reset count (entries+exits only). Must be an
            # integer >= 1. The reset branch above already rejects a
            # non-default count; here we enforce the lower bound on the
            # binder. ``bool`` is rejected explicitly (it subclasses int
            # but is never a valid count).
            if not is_reset and (
                isinstance(rrc, bool) or not isinstance(rrc, int) or rrc < 1
            ):
                raise SignalValidationError(
                    f"{path}: requires_reset_count must be an integer "
                    f">= 1 (got {rrc!r})"
                )
            # Temporal chain (entries/exits only — resets reject above). A
            # non-empty links map must form one contiguous forward chain over
            # the block's conditions (validated; HTTP 400 on malformed input).
            if not is_reset:
                parsed_links = _parse_links(raw_links, len(conds), path=path)

        out.append(
            Block(
                id=bid,
                name=name,
                conditions=conds,
                input_id=iid,
                weight=weight,
                target_entry_block_names=tgt_names,
                enabled=bool(blk.enabled),
                description=str(blk.description or ""),
                requires_reset_block_id=rrb,
                requires_reset_count=int(rrc),
                links=parsed_links,
            )
        )
    return tuple(out)


def parse_signal(
    raw: SignalIn,
    *,
    resolved_inputs: list[_InputIn | _ResolvedBasketInput] | None = None,
) -> Signal:
    """Parse a wire-shape :class:`SignalIn` into the dataclass :class:`Signal`.

    When ``resolved_inputs`` is given it replaces ``raw.inputs`` — used by
    :func:`compute_signal` to inject pre-resolved basket inputs.
    """
    input_list = resolved_inputs if resolved_inputs is not None else raw.inputs
    inputs = tuple(_parse_input(i) for i in input_list)
    # Parse resets FIRST so we can collect their ids for cross-validating
    # entries' and exits' requires_reset_block_id bindings.
    resets = _parse_blocks(
        raw.rules.resets,
        section="resets",
        is_entry=False,
        is_reset=True,
    )
    reset_ids: set[str] = {b.id for b in resets if b.id}
    entries = _parse_blocks(
        raw.rules.entries,
        section="entries",
        is_entry=True,
        reset_ids=reset_ids,
    )
    entry_names: set[str] = {b.name for b in entries if b.name}
    exits = _parse_blocks(
        raw.rules.exits,
        section="exits",
        is_entry=False,
        entry_names=entry_names,
        reset_ids=reset_ids,
    )
    rules = SignalRules(entries=entries, exits=exits, resets=resets)
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
        # Preserve the short-circuit for the spot/continuous case
        # (they don't need pre-resolved dates — the leaf resolver
        # borrows the date axis from the price series itself).  But
        # fall through into the loop when the single input has an
        # option-stream dependency, because the option_stream resolver
        # needs an explicit (start, end) window derived from available
        # expirations — and Bug 2 surfaces when the envelope dates are
        # ``None`` (SignalsPage has no date-range UI today).
        if not signal.inputs or not _has_option_stream_dependency(
            signal.inputs[0].instrument
        ):
            return start, end
        # else: fall through to the per-input loop, which clamps via
        # `_date_array_for_leaf_instrument` (option_stream branch) /
        # the basket recursion at lines below.

    date_arrays: list[npt.NDArray[np.int64]] = []
    for inp in signal.inputs:
        inst = inp.instrument
        if isinstance(inst, InstrumentBasket):
            # Intersection of leg date arrays (shared with the standalone
            # basket-series window derivation — same per-leg recursion).
            date_arrays.append(
                await basket_leg_date_intersection(
                    inst,
                    svc,
                    start=start,
                    end=end,
                    err_prefix=f"input {inp.id!r}",
                )
            )
        else:
            date_arrays.append(
                await _date_array_for_leaf_instrument(
                    inst,
                    svc,
                    start=start,
                    end=end,
                    err_prefix=f"input {inp.id!r}",
                )
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
    if isinstance(inst, InstrumentBasket):
        # Kind-discriminated emission so the FE can re-render either
        # shape from the response.  Each leg's ``instrument`` is emitted
        # via the same per-type payload-builder used by the top-level
        # branches above — guarantees the wire round-trip matches the
        # leg's input shape exactly.  Saved baskets carry ``basket_id``;
        # inline baskets carry ``asset_class``.
        legs_payload = [
            {"instrument": _instrument_payload(leg_inst), "weight": float(w)}
            for leg_inst, w in inst.legs
        ]
        if inst.basket_id is not None:
            return {
                "type": "basket",
                "kind": "saved",
                "basket_id": inst.basket_id,
                "legs": legs_payload,
            }
        return {
            "type": "basket",
            "kind": "inline",
            "asset_class": inst.asset_class,
            "legs": legs_payload,
        }
    if isinstance(inst, InstrumentOptionStream):
        from dataclasses import asdict

        return {
            "type": "option_stream",
            "collection": inst.collection,
            "option_type": inst.option_type,
            "cycle": inst.cycle,
            "maturity": asdict(inst.maturity),
            "selection": asdict(inst.selection),
            "stream": inst.stream,
            # Snake_case ``roll_offset`` mirrors the inbound ``OptionStreamRef``
            # wire model so the emitted payload round-trips through
            # ``OptionStreamRef.model_validate``.  Emitted as the unified
            # ``{value, unit}`` object (the wire model also still accepts a bare
            # int for legacy reads).  No ``adjustment``/``roll_schedule`` key:
            # option streams carry no back-adjustment, and "end of month" is the
            # EndOfMonth maturity, not a separate schedule.
            "roll_offset": {
                "value": int(inst.roll_offset.value),
                "unit": inst.roll_offset.unit,
            },
            # Select-and-hold flag round-trips through ``OptionStreamRef`` (default
            # False = current daily-reselect series).
            "hold_between_rolls": bool(inst.hold_between_rolls),
            # Premium-notional multiple for the fixed-contract dollar-P&L sizing
            # (hold mode only); round-trips through ``OptionStreamRef`` (default
            # 1.0).
            "nav_times": float(inst.nav_times),
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
    repo: WriteRepository = Depends(get_write_repository),
) -> dict:
    """Evaluate a v4 Signal and return per-input positions + events."""

    try:
        start_date, end_date = parse_iso_range(body.start, body.end)
    except ValueError as exc:
        return error_response("validation", str(exc))

    try:
        resolved_inputs = await _resolve_basket_inputs(body.spec.inputs, repo, svc)
        signal = parse_signal(body.spec, resolved_inputs=resolved_inputs)
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
            svc,
            signal,
            start_date,
            end_date,
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
        return error_response("runtime", str(exc), traceback=exc.user_traceback or None)

    timestamps = [_int_yyyymmdd_to_unix_ms(int(d)) for d in result.index.tolist()]

    positions_out: list[dict] = []
    realized_pnl_out: list[list[float | None]] = []
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
        # Per-input cumulative contribution to the compounded account
        # (Σ over inputs == equity_ratio - 1). Serialize NaN→null like the
        # rest of the response for consistency/robustness.
        realized_pnl_out.append(nan_safe_floats(p.realized_pnl))

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
                "target_entry_block_names": list(ev.target_entry_block_names),
            }
        )

    indicator_own_panel: dict[str, bool] = {
        spec.id: spec.ownPanel for spec in body.indicators
    }
    indicators_out: list[dict] = []
    for ind in result.indicator_series:
        entry: dict = {
            "input_id": ind.input_id,
            "indicator_id": ind.indicator_id,
            "series": nan_safe_floats(ind.series),
            "ownPanel": indicator_own_panel.get(ind.indicator_id, False),
        }
        if ind.params_override:
            entry["params_override"] = ind.params_override
        indicators_out.append(entry)

    trades_out: list[dict] = [
        {
            "input_id": tr.input_id,
            "entry_block_id": tr.entry_block_id,
            "entry_block_name": tr.entry_block_name,
            "exit_block_id": tr.exit_block_id,
            "exit_block_name": tr.exit_block_name,
            "open_bar": tr.open_bar,
            "close_bar": tr.close_bar,
            "direction": tr.direction,
            "signed_weight": float(tr.signed_weight),
        }
        for tr in result.trades
    ]

    return {
        "timestamps": timestamps,
        "positions": positions_out,
        "realized_pnl": realized_pnl_out,
        "equity_ratio": nan_safe_floats(result.equity_ratio),
        "events": events_out,
        "indicators": indicators_out,
        "trades": trades_out,
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
