"""Signal evaluator -- pure NumPy + per-bar stateful latching (v4).

v4 — unified Entries/Exits with signed weights
----------------------------------------------
The v3 four-direction model is gone. Each signal declares two block
lists:

  * ``rules.entries`` -- blocks with stable ``id``, user-editable
    ``name``, and signed ``weight`` in ``[-100, +100]``;
    ``sign(weight)`` decides long/short.
  * ``rules.exits`` -- blocks that each target *one or more* entries via
    ``target_entry_block_names``. When an exit's AND-condition fires at
    bar ``t``, the latch of every targeted entry that is currently open
    is cleared; no other latches are touched (no "same-side-under-input"
    blanket clear). Targeted entries may live on different inputs.

Per-bar execution (declaration order within each list):

  1. **Clear pass.** For every usable exit block whose condition fires
     at ``t``: for each targeted entry name, look up its latch by
     name → id, and if True set False.
  2. **Entry pass.** For every usable entry block whose condition
     fires at ``t`` AND whose latch is currently False: set latch True.
     Leverage is allowed — no budget cap, no same-bar conflict logic
     beyond per-block latch state.

After both passes at ``t``::

    position_I(t) = sum over entry-blocks B with input_id == I and B
                    currently latched of (sign(B.weight) * |B.weight|/100)
    position_I(t) = 0 if any-nan-poison at t (preserved from v3).

Block usability
---------------
A block is "usable" iff
  * it has ≥1 condition;
  * ``id`` is non-empty (required for stable tracking / exit targeting);
  * entry blocks require ``input_id`` resolves to a declared Input,
    ``weight != 0`` AND ``|weight| <= 100``;
  * exit blocks require ``target_entry_block_names`` with at least one
    name referencing a usable entry block's name in the same signal's
    rules (any name not resolving to a usable entry is ignored; an exit
    with no resolvable target is a no-op — the engine tolerates this so
    latent bad state degrades gracefully; the API layer rejects dangling
    targets with HTTP 400). Exit blocks do NOT carry their own
    ``input_id``; the operating inputs are always derived from the
    targeted entries' ``input_id`` values (possibly several).
  * every operand's ``input_id`` resolves;
  * the bound Input's instrument is fully configured.

Indicator operand resolution is unchanged from v3 (input-bound primary
label, optional label → input_id overrides, params_override merge).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

import numpy as np
import numpy.typing as npt

from tcg.engine.costs import (
    CostConfig,
    cumulative_cost_pct,
    establish_turnover,
    roll_turnover_from_flags,
    split_drag,
)
from tcg.engine.hold_pnl import _HoldPnLSpec, _compound_with_hold
from tcg.engine.indicator_exec import (
    IndicatorRuntimeError,
    IndicatorValidationError,
    run_indicator,
)
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
    Trade,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SignalValidationError(ValueError):
    """Signal spec or referenced inputs failed static validation."""


class SignalRuntimeError(RuntimeError):
    """Signal evaluation failed at runtime (e.g. indicator raised)."""

    def __init__(self, message: str, user_traceback: str = "") -> None:
        super().__init__(message)
        self.user_traceback: str = user_traceback


class SignalDataError(RuntimeError):
    """A referenced instrument or indicator input was unavailable."""


# ---------------------------------------------------------------------------
# Indicator-spec input (mirrors /api/indicators/compute body shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorSpecInput:
    """Inline indicator spec shipped in a signals compute request."""

    code: str
    params: dict[str, float | int | bool]
    # Preserves declaration order — the first label is the "primary" one
    # that binds to the operand's input_id.
    series_labels: tuple[str, ...]
    # Base per-label instrument id string; v3 uses these only as a
    # fallback when no override is supplied AND the label is not the
    # primary (which is always bound via operand.input_id).
    series_map: dict[str, tuple[str, str]]  # label → (collection, instrument_id)


# ---------------------------------------------------------------------------
# Price fetcher protocol
# ---------------------------------------------------------------------------


# Fetch returns (dates int64 YYYYMMDD, values float64) for a concrete
# :class:`InputInstrument` + field. Field is "close"/"open"/... for OHLCV.
PriceFetcher = Callable[
    [InputInstrument, str],
    Awaitable[tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]],
]


# ---------------------------------------------------------------------------
# Input table helpers
# ---------------------------------------------------------------------------


def _input_table(signal: Signal) -> dict[str, Input]:
    """Build ``input_id -> Input`` map, enforcing unique ids."""
    out: dict[str, Input] = {}
    for inp in signal.inputs:
        if not inp.id:
            raise SignalValidationError("input id must be a non-empty string")
        if inp.id in out:
            raise SignalValidationError(f"duplicate input id: {inp.id!r}")
        out[inp.id] = inp
    return out


def _require_input(input_id: str, inputs: dict[str, Input], *, ctx: str) -> Input:
    if not input_id:
        raise SignalValidationError(f"{ctx}: input_id is empty")
    inp = inputs.get(input_id)
    if inp is None:
        raise SignalValidationError(
            f"{ctx}: unknown input_id {input_id!r}; declared inputs: "
            f"{sorted(inputs.keys())!r}"
        )
    return inp


def _instrument_identity(inst: InputInstrument) -> tuple:
    """Hashable identity for an :class:`InputInstrument` (per-kind)."""
    if isinstance(inst, InstrumentSpot):
        return ("spot", inst.collection, inst.instrument_id)
    if isinstance(inst, InstrumentContinuous):
        return (
            "continuous",
            inst.collection,
            inst.adjustment,
            inst.cycle or "",
            int(inst.roll_offset),
            inst.strategy,
        )
    if isinstance(inst, InstrumentOptionStream):
        return (
            "option_stream",
            inst.collection,
            inst.option_type,
            inst.cycle or "",
            repr(inst.maturity),
            repr(inst.selection),
            inst.stream,
            # Unified roll offset (value, unit) — both parts distinguish identity.
            (int(inst.roll_offset.value), inst.roll_offset.unit),
            # Select-and-hold vs daily-reselect are DIFFERENT series (different
            # P&L) → distinct identity so they never share a fetch-cache slot.
            bool(inst.hold_between_rolls),
        )
    if isinstance(inst, InstrumentBasket):
        # Kind-prefixed identities so a user-chosen basket_id of "inline"
        # cannot collide with a structural-identity inline basket (Q2 of
        # Wave-P decisions).
        if inst.basket_id is not None:
            return ("basket", "saved", inst.basket_id)
        # Inline basket: structural identity built from asset_class +
        # canonically-sorted per-leg (instrument-identity, weight) pairs.
        # Recursing into ``_instrument_identity`` for each leg's
        # instrument means two inline baskets with the same legs in any
        # order share an identity, AND two baskets with the same legs
        # but different adjustment / cycle / option-stream selection
        # produce *different* identities (iter-3 requirement: full
        # instrument spec hashed, not just instrument_id).
        legs_key = tuple(
            sorted(
                (_instrument_identity(leg_inst), float(leg_weight))
                for leg_inst, leg_weight in inst.legs
            )
        )
        return ("basket", "inline", inst.asset_class, legs_key)
    raise SignalValidationError(f"unknown instrument kind: {inst!r}")


# ---------------------------------------------------------------------------
# Indicator override merge (unchanged from v3)
# ---------------------------------------------------------------------------


def _merge_indicator_effective(
    op: IndicatorOperand,
    base: IndicatorSpecInput,
    inputs: dict[str, Input],
) -> tuple[
    dict[str, float | int | bool],
    dict[str, InputInstrument],
]:
    eff_params: dict[str, float | int | bool] = dict(base.params)
    if op.params_override:
        for k, v in op.params_override.items():
            eff_params[k] = v  # type: ignore[assignment]

    primary = _require_input(
        op.input_id, inputs, ctx=f"indicator {op.indicator_id!r} operand"
    )
    overrides = op.series_override or {}

    if not base.series_labels:
        raise SignalValidationError(
            f"indicator {op.indicator_id!r}: series_map must be non-empty"
        )

    eff_label_inst: dict[str, InputInstrument] = {}
    for idx, label in enumerate(base.series_labels):
        if label in overrides:
            other = _require_input(
                overrides[label],
                inputs,
                ctx=(f"indicator {op.indicator_id!r} series_override[{label!r}]"),
            )
            eff_label_inst[label] = other.instrument
        elif idx == 0:
            eff_label_inst[label] = primary.instrument
        else:
            base_entry = base.series_map.get(label)
            if base_entry is None:
                raise SignalValidationError(
                    f"indicator {op.indicator_id!r}: label {label!r} has no "
                    f"base series_map entry and is not overridden"
                )
            coll, iid = base_entry
            eff_label_inst[label] = InstrumentSpot(collection=coll, instrument_id=iid)

    return eff_params, eff_label_inst


def _freeze_params(p: dict[str, float | int | bool]) -> tuple:
    return tuple(sorted((k, v) for k, v in p.items()))


def _freeze_label_inst(m: dict[str, InputInstrument]) -> tuple:
    return tuple(
        sorted((label, _instrument_identity(inst)) for label, inst in m.items())
    )


# ---------------------------------------------------------------------------
# Operand cache key
# ---------------------------------------------------------------------------


def _operand_key(
    operand: Operand,
    indicators: dict[str, IndicatorSpecInput],
    inputs: dict[str, Input],
) -> tuple:
    """Cache key identifying an operand's resolved series."""
    if isinstance(operand, IndicatorOperand):
        base = indicators.get(operand.indicator_id)
        if base is None:
            return (
                "indicator",
                operand.indicator_id,
                operand.output,
                "MISSING",
                operand.input_id,
            )
        eff_p, eff_i = _merge_indicator_effective(operand, base, inputs)
        return (
            "indicator",
            operand.indicator_id,
            operand.output,
            _freeze_params(eff_p),
            _freeze_label_inst(eff_i),
        )
    if isinstance(operand, InstrumentOperand):
        inp = _require_input(operand.input_id, inputs, ctx="instrument operand")
        return (
            "instrument",
            _instrument_identity(inp.instrument),
            operand.field,
        )
    if isinstance(operand, ConstantOperand):
        return ("constant", float(operand.value))
    raise SignalValidationError(f"unknown operand kind: {operand!r}")


# ---------------------------------------------------------------------------
# Walks
# ---------------------------------------------------------------------------


def _walk_operands(
    signal: Signal,
    inputs: dict[str, Input],
    entry_names: set[str],
) -> list[Operand]:
    # Skip blocks that the engine drops from its block lists so their
    # operands are never fetched — a dropped block contributes nothing to
    # positions/events, so walking its operands is at best wasteful and at
    # worst harmful (it can fetch/raise on data no usable block needs).
    #
    # Gating differs by block kind, on purpose:
    #   * EXITS use the full ``_usable_exit`` predicate. This is the S1
    #     fix: an ENABLED exit whose targets ALL dangle is usable()==False
    #     and is dropped from ``exit_blocks`` (becomes a no-op), so its
    #     operands must NOT be walked — otherwise a no-op exit could fetch
    #     or raise. Exits carry no input_id of their own and surface no
    #     input-resolution errors, so skipping them loses no validation.
    #   * ENTRIES and RESETS gate on ``enabled`` ONLY (NOT full usability).
    #     An enabled entry referencing an UNDECLARED input_id is dropped
    #     from ``entry_blocks`` by ``_usable_entry``, but walking its
    #     operand is exactly how that user error surfaces as a
    #     SignalValidationError (the API has no separate declared-input
    #     check; see test_unknown_input_id_validation). Gating entries on
    #     full usability would silently swallow that error — a regression.
    #     Disabled entries/resets are still skipped (the ``enabled`` gate),
    #     preserving the "disabled ≡ deleted" no-fetch behaviour.
    out: list[Operand] = []
    for block in signal.rules.exits:
        if not _usable_exit(block, inputs, entry_names):
            continue
        out.extend(_block_operands(block))
    for block in (*signal.rules.entries, *signal.rules.resets):
        if not block.enabled:
            continue
        out.extend(_block_operands(block))
    return out


def _block_operands(block: Block) -> list[Operand]:
    """Return every operand referenced by a block's conditions."""
    out: list[Operand] = []
    for cond in block.conditions:
        if isinstance(cond, (CompareCondition, CrossCondition)):
            out.append(cond.lhs)
            out.append(cond.rhs)
        elif isinstance(cond, InRangeCondition):
            out.append(cond.operand)
            out.append(cond.min)
            out.append(cond.max)
        elif isinstance(cond, RollingCondition):
            out.append(cond.operand)
        else:
            raise SignalValidationError(
                f"unknown condition type: {type(cond).__name__}"
            )
    return out


def _block_uses_since_reset(block: Block) -> bool:
    """True iff any condition in the block is a ``since_reset`` cross count.

    Used to gate the OPTIONAL reset-fire threading in ``evaluate_signal``: only
    such a block needs its bound reset's firing array supplied to
    :func:`_eval_block_activity`. Every default (rolling) block returns False, so
    the reset-fire precompute never runs on the historical path.
    """
    return any(
        isinstance(c, CrossCondition)
        and getattr(c, "count_mode", "rolling") == "since_reset"
        for c in block.conditions
    )


# ---------------------------------------------------------------------------
# Operand resolution
# ---------------------------------------------------------------------------


async def _resolve_indicator_operand(
    op: IndicatorOperand,
    indicators: dict[str, IndicatorSpecInput],
    inputs: dict[str, Input],
    fetcher: PriceFetcher,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Run a referenced indicator and return its ``(dates, values)`` series."""
    base = indicators.get(op.indicator_id)
    if base is None:
        raise SignalDataError(
            f"indicator spec not provided for indicator_id={op.indicator_id!r}"
        )

    eff_params, eff_label_inst = _merge_indicator_effective(op, base, inputs)

    fetched: list[tuple[str, npt.NDArray[np.int64], npt.NDArray[np.float64]]] = []
    for label in base.series_labels:
        inst = eff_label_inst[label]
        dates, values = await fetcher(inst, "close")
        if dates.size >= 2 and not bool(np.all(np.diff(dates) > 0)):
            raise SignalValidationError(
                f"indicator {op.indicator_id!r} series {label!r}: "
                f"non-monotonic or duplicate dates"
            )
        fetched.append((label, dates, values))

    common = fetched[0][1]
    for _label, dates, _vals in fetched[1:]:
        common = np.intersect1d(common, dates, assume_unique=False)
    if common.size == 0:
        raise SignalValidationError(
            f"indicator {op.indicator_id!r}: no overlapping dates across "
            f"its referenced series"
        )
    common = np.sort(common)

    aligned: dict[str, npt.NDArray[np.float64]] = {}
    for label, dates, values in fetched:
        mask = np.isin(dates, common)
        aligned_dates = dates[mask]
        vals = values[mask].astype(np.float64, copy=False)
        order = np.argsort(aligned_dates)
        aligned[label] = vals[order]

    try:
        result = run_indicator(base.code, dict(eff_params), aligned)
    except IndicatorValidationError as exc:
        raise SignalValidationError(f"indicator {op.indicator_id!r}: {exc}") from exc
    except IndicatorRuntimeError as exc:
        raise SignalRuntimeError(
            f"indicator {op.indicator_id!r}: {exc}",
            user_traceback=exc.user_traceback,
        ) from exc

    return common.astype(np.int64, copy=False), result.astype(np.float64, copy=False)


async def _resolve_operand(
    operand: Operand,
    indicators: dict[str, IndicatorSpecInput],
    inputs: dict[str, Input],
    fetcher: PriceFetcher,
) -> tuple[
    npt.NDArray[np.int64] | None,
    npt.NDArray[np.float64] | None,
    float | None,
]:
    if isinstance(operand, ConstantOperand):
        return None, None, float(operand.value)
    if isinstance(operand, InstrumentOperand):
        inp = _require_input(operand.input_id, inputs, ctx="instrument operand")
        dates, values = await fetcher(inp.instrument, operand.field)
        if dates.size >= 2 and not bool(np.all(np.diff(dates) > 0)):
            raise SignalValidationError(
                f"instrument operand on input {operand.input_id!r}: "
                f"non-monotonic or duplicate dates"
            )
        return (
            dates.astype(np.int64, copy=False),
            values.astype(np.float64, copy=False),
            None,
        )
    if isinstance(operand, IndicatorOperand):
        dates, values = await _resolve_indicator_operand(
            operand, indicators, inputs, fetcher
        )
        return dates, values, None
    raise SignalValidationError(f"unknown operand kind: {operand!r}")


# ---------------------------------------------------------------------------
# Union alignment
# ---------------------------------------------------------------------------


def _union_align(
    resolved: dict[
        tuple,
        tuple[
            npt.NDArray[np.int64] | None,
            npt.NDArray[np.float64] | None,
            float | None,
        ],
    ],
) -> tuple[npt.NDArray[np.int64], dict[tuple, npt.NDArray[np.float64]]]:
    all_dates: list[npt.NDArray[np.int64]] = []
    for dates, _vals, _scalar in resolved.values():
        if dates is not None:
            all_dates.append(dates)

    if not all_dates:
        index = np.array([], dtype=np.int64)
    else:
        index = np.unique(np.concatenate(all_dates))

    values_by_key: dict[tuple, npt.NDArray[np.float64]] = {}
    for key, (dates, vals, scalar) in resolved.items():
        if scalar is not None:
            values_by_key[key] = np.full(index.size, scalar, dtype=np.float64)
            continue
        assert dates is not None and vals is not None
        if dates.size == 0:
            values_by_key[key] = np.full(index.size, np.nan, dtype=np.float64)
            continue
        pos = np.searchsorted(dates, index)
        safe_pos = np.clip(pos, 0, dates.size - 1)
        match = dates[safe_pos] == index
        out = np.full(index.size, np.nan, dtype=np.float64)
        out[match] = vals[safe_pos[match]]
        values_by_key[key] = out

    return index, values_by_key


def _align_series_to_index(
    dates: npt.NDArray[np.int64],
    values: npt.NDArray[np.float64],
    index: npt.NDArray[np.int64],
    *,
    fill: float,
) -> npt.NDArray[np.float64]:
    """Re-index ``(dates, values)`` onto ``index`` (same rule as ``_union_align``).

    ``dates`` must be sorted ascending.  Dates in ``index`` absent from ``dates``
    get ``fill``.  Used to bring a hold-mode option's ``is_roll`` / ``roll_premium``
    side-channel arrays onto the signal's union axis so they line up with the
    input's premium series.
    """
    out = np.full(index.size, fill, dtype=np.float64)
    if dates.size == 0 or index.size == 0:
        return out
    pos = np.searchsorted(dates, index)
    safe_pos = np.clip(pos, 0, dates.size - 1)
    match = dates[safe_pos] == index
    out[match] = values[safe_pos[match]]
    return out


# ---------------------------------------------------------------------------
# Condition evaluation (vectorised) -- unchanged from v3
# ---------------------------------------------------------------------------


_COMPARE_OPS: dict[str, Callable[[np.ndarray, np.ndarray], np.ndarray]] = {
    "gt": np.greater,
    "lt": np.less,
    "ge": np.greater_equal,
    "le": np.less_equal,
    "eq": np.equal,
}


def _eval_condition(
    cond: Condition,
    indicators: dict[str, IndicatorSpecInput],
    inputs: dict[str, Input],
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
    reset_fire: npt.NDArray[np.bool_] | None = None,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """Evaluate one condition to ``(truth, nan_at_t)`` boolean arrays.

    ``reset_fire`` is an OPTIONAL per-bar boolean of the owning block's bound
    reset-block firing bars, consumed ONLY by a ``CrossCondition`` whose
    ``count_mode == "since_reset"`` (to reset the crossing counter). It is
    ``None`` in every default (rolling) path, so the historical code paths are
    untouched (byte-identical).
    """

    def k(o: Operand) -> tuple:
        return _operand_key(o, indicators, inputs)

    if isinstance(cond, CompareCondition):
        a = values_by_key[k(cond.lhs)]
        b = values_by_key[k(cond.rhs)]
        nan_at_t = np.isnan(a) | np.isnan(b)
        with np.errstate(invalid="ignore"):
            truth = _COMPARE_OPS[cond.op](a, b)
        truth = truth & ~nan_at_t
        return truth.astype(np.bool_, copy=False), nan_at_t

    if isinstance(cond, CrossCondition):
        a = values_by_key[k(cond.lhs)]
        b = values_by_key[k(cond.rhs)]
        truth = np.zeros(T, dtype=np.bool_)
        nan_at_t = np.isnan(a) | np.isnan(b)
        if T >= 2:
            a_prev = a[:-1]
            b_prev = b[:-1]
            a_cur = a[1:]
            b_cur = b[1:]
            prev_nan = np.isnan(a_prev) | np.isnan(b_prev)
            cur_nan = np.isnan(a_cur) | np.isnan(b_cur)
            with np.errstate(invalid="ignore"):
                if cond.op == "cross_above":
                    fired = (a_prev <= b_prev) & (a_cur > b_cur)
                else:
                    fired = (a_prev >= b_prev) & (a_cur < b_cur)
            fired = fired & ~prev_nan & ~cur_nan
            truth[1:] = fired
        count = int(getattr(cond, "count", 1) or 1)
        window = int(getattr(cond, "window", 1) or 1)
        count_mode = getattr(cond, "count_mode", "rolling")
        if count_mode == "since_reset":
            # ABSOLUTE reset-on-exit ladder: cumulative crossing count SINCE the
            # last reset, firing an IMPULSE on the count-th crossing then
            # re-arming.  ``truth`` (the single-bar cross pulses) feeds the
            # stateful O(T) accumulator.  A missing ``reset_fire`` (block carries
            # no bound reset, or a directly-constructed signal) means "never
            # reset" — cumulative from bar 0.  ``window`` is deliberately unused.
            rf = reset_fire if reset_fire is not None else np.zeros(T, dtype=np.bool_)
            truth = _cross_since_reset(truth, rf, count)
            return truth.astype(np.bool_, copy=False), nan_at_t
        if count == 1 and window == 1:
            # Default single-bar crossover: byte-identical to the historical
            # code path (a trailing window of one bar holds only bar t's
            # crossing, so count>=1 is exactly the pulse computed above).
            return truth, nan_at_t
        # cross_count: True iff >= ``count`` SAME-DIRECTION crossings occurred
        # in the trailing ``window`` bars (inclusive of bar t). Vectorized
        # trailing sum over the cross-edge pulses (``truth``); O(T). A NaN bar
        # cannot produce a pulse (guarded above) so it contributes 0 — no
        # warm-up poison is added (the count is simply lower early on),
        # matching the locked decision "NaN -> 0".
        pulses = truth.astype(np.int64)
        prefix = np.concatenate(([0], np.cumsum(pulses)))  # len T+1
        idx = np.arange(T)
        lo = np.maximum(0, idx - window + 1)
        windowed = prefix[idx + 1] - prefix[lo]
        truth = windowed >= count
        return truth.astype(np.bool_, copy=False), nan_at_t

    if isinstance(cond, InRangeCondition):
        x = values_by_key[k(cond.operand)]
        lo = values_by_key[k(cond.min)]
        hi = values_by_key[k(cond.max)]
        nan_at_t = np.isnan(x) | np.isnan(lo) | np.isnan(hi)
        with np.errstate(invalid="ignore"):
            truth = (x >= lo) & (x <= hi)
        truth = truth & ~nan_at_t
        return truth.astype(np.bool_, copy=False), nan_at_t

    if isinstance(cond, RollingCondition):
        x = values_by_key[k(cond.operand)]
        kk = int(cond.lookback)
        if kk < 1:
            raise SignalValidationError(f"rolling lookback must be >= 1, got {kk}")
        truth = np.zeros(T, dtype=np.bool_)
        nan_at_t = np.isnan(x).copy()
        nan_at_t[: min(kk, T)] = True
        if T > kk:
            cur = x[kk:]
            prev = x[:-kk]
            prev_or_cur_nan = np.isnan(cur) | np.isnan(prev)
            with np.errstate(invalid="ignore"):
                if cond.op == "rolling_gt":
                    fired = cur > prev
                else:
                    fired = cur < prev
            fired = fired & ~prev_or_cur_nan
            truth[kk:] = fired
            lookback_nan = np.zeros(T, dtype=np.bool_)
            lookback_nan[kk:] = np.isnan(prev)
            nan_at_t = nan_at_t | lookback_nan
        return truth, nan_at_t

    raise SignalValidationError(f"unknown condition type: {type(cond).__name__}")


def _cross_since_reset(
    pulses: npt.NDArray[np.bool_],
    reset_fire: npt.NDArray[np.bool_],
    count: int,
) -> npt.NDArray[np.bool_]:
    """Impulse on the ``count``-th crossing SINCE the last reset (Feature 2).

    ``pulses[t]`` is a same-direction crossing at bar ``t`` (already NaN-guarded
    by the caller — a NaN bar produces no pulse). ``reset_fire[t]`` is a bar on
    which the owning block's bound reset FIRED. Semantics per bar, in order:

      1. **reset**: if ``reset_fire[t]``, zero the running crossing counter
         (the ladder restarts; any partial progress is lost).
      2. **count + fire**: if ``pulses[t]``, increment the counter; when it
         reaches ``count`` emit an IMPULSE True at ``t`` and reset the counter to
         0 (consume, re-arm so the NEXT ``count`` crossings fire again).

    Reset-before-count on a coincident bar means a reset that lands on the same
    bar as a crossing wipes the counter first, so that crossing is the 1st of the
    new ladder. ``count`` is clamped to ``>= 1`` defensively. O(T), O(1) state.
    """
    T = pulses.size
    out = np.zeros(T, dtype=np.bool_)
    n = max(1, int(count))
    seen = 0
    for t in range(T):
        if reset_fire[t]:
            seen = 0
        if pulses[t]:
            seen += 1
            if seen >= n:
                out[t] = True
                seen = 0
    return out


# ---------------------------------------------------------------------------
# Block activity
# ---------------------------------------------------------------------------


def _usable_entry(block: Block, inputs: dict[str, Input]) -> bool:
    """Entry block is usable iff it has id + input_id + conditions +
    signed weight in (−100..0) ∪ (0..100] and is enabled."""
    if not block.enabled:
        return False
    if not block.id:
        return False
    if not block.conditions:
        return False
    if not block.input_id or block.input_id not in inputs:
        return False
    w = float(block.weight)
    if w == 0.0:
        return False
    if abs(w) > 100.0:
        return False
    return True


def _usable_reset(block: Block) -> bool:
    """Reset block is usable iff it has id + ≥1 condition AND is enabled.

    Reset blocks are signal-global: they carry no ``input_id``, no
    ``weight``, no ``target_entry_block_names``. Those fields, if
    present, are ignored at the engine layer (API layer rejects them
    at parse time).
    """
    if not block.enabled:
        return False
    if not block.id:
        return False
    if not block.conditions:
        return False
    return True


def _usable_exit(block: Block, inputs: dict[str, Input], entry_names: set[str]) -> bool:
    """Exit block is usable iff it is enabled, has an id, has conditions,
    AND at least one name in ``target_entry_block_names`` references a
    usable entry's name.

    Names that do not resolve to a usable entry are tolerated (ignored at
    the clear pass); an exit whose targets ALL fail to resolve is not
    usable. Exit blocks do not carry their own ``input_id``; the
    operating inputs are derived from the targeted entries at execution
    time (and may span multiple inputs).
    """
    if not block.enabled:
        return False
    if not block.id:
        return False
    if not block.conditions:
        return False
    # At least one target must resolve to a usable entry name.
    return any(name in entry_names for name in block.target_entry_block_names)


def _exit_input_ids(exit_block: Block, entries_by_name: dict[str, Block]) -> list[str]:
    """Return the operating input ids for an exit block, derived from its
    targeted entries (one per resolvable target, de-duplicated, order
    preserved).

    Callers must only pass exit blocks that are already known to be
    usable (i.e. at least one name resolves in ``entries_by_name``);
    targets that do not resolve are skipped. An exit targeting entries on
    multiple inputs yields multiple input ids.
    """
    out: list[str] = []
    seen: set[str] = set()
    for name in exit_block.target_entry_block_names:
        entry = entries_by_name.get(name)
        if entry is None:
            continue
        if entry.input_id in seen:
            continue
        seen.add(entry.input_id)
        out.append(entry.input_id)
    return out


def _link_groups(
    block: Block,
) -> tuple[list[tuple[int, ...]], list[int]] | None:
    """Partition a block's conditions into conjunction GROUPS separated by THEN
    boundaries, or ``None`` for a zero-link / single-group (pure-CNF) block.

    ``block.links`` maps a SUCCESSOR condition index (``1..len-1``) → ``within``
    window in bars. Under the GROUP semantics (v5) a gap PRESENT in ``links``
    (with a positive window) is a THEN boundary — it starts a new conjunction
    group and records its window; a gap ABSENT is AND (same group). A window of
    ``0`` folds to a non-link (plain AND), matching the pre-existing W=0 rule.
    The API layer validates ``links`` (HTTP 400); here we defensively ignore
    out-of-range keys and non-positive windows so a directly-constructed Signal
    degrades cleanly.

    Returns ``(groups, windows)`` where ``groups[r]`` is the tuple of condition
    indices in group ``r`` (in order) and ``windows`` has length
    ``len(groups) - 1`` (``windows[r]`` = window from group ``r`` to group
    ``r+1``). Returns ``None`` when there is NO THEN boundary — one group / pure
    CNF — including the degenerate ``m < 2`` case; the caller then takes the
    literal historical CNF path (byte-identical).
    """
    links = block.links
    if not links:
        return None
    m = len(block.conditions)
    if m < 2:
        return None
    # THEN boundaries = successor indices with a positive window, in range.
    # W<=0 or out-of-range keys fold to AND (no boundary). Anything left is a
    # boundary between conjunction groups; zero boundaries ⇒ CNF (None).
    boundaries = {
        int(kk): int(vv)
        for kk, vv in links.items()
        if int(vv) >= 1 and 1 <= int(kk) < m
    }
    if not boundaries:
        return None
    groups: list[tuple[int, ...]] = []
    windows: list[int] = []
    cur: list[int] = [0]
    for i in range(1, m):
        if i in boundaries:
            groups.append(tuple(cur))
            windows.append(boundaries[i])  # window from prev group to this one
            cur = [i]
        else:
            cur.append(i)
    groups.append(tuple(cur))
    return groups, windows


def _to_pulse(
    active: npt.NDArray[np.bool_],
    nan_mask: npt.NDArray[np.bool_] | None = None,
) -> npt.NDArray[np.bool_]:
    """Rising-edge conversion of a level array (Item 3a ``fire_mode="pulse"``).

    ``pulsed[t] = active[t] & ~prev_level`` with ``pulsed[0] = active[0]`` — the
    block fires only on the bar its level first goes true, then must drop false
    before it can fire again ("re-arm").

    ``prev_level`` is the level of the most recent NON-GAP bar, not literally
    ``active[t-1]``. This matters because ``active`` is NaN-poisoned: a bar with
    missing data (``nan_mask[t]`` True) has ``active`` forced to False, which is
    indistinguishable from the condition genuinely going false. The engine grid
    is a UNION of every input's calendar, so an interior data hole (e.g. a
    VIX-holiday bar that quotes for one input and is NaN for another) is common.
    Reading such a hole as a level drop manufactured a PHANTOM re-arm on the
    first bar after the gap (sweep-2 MINOR-1: ``[T,T,T,T,gap,T]`` wrongly fired
    at the trailing bar). So a gap bar neither fires nor updates ``prev_level`` —
    the last valid level is carried across the hole. A genuine present-data
    False->True transition still fires. The very first bar and a LEADING gap keep
    the historical behavior (no prior valid level == not armed, so the first real
    True fires).

    NOTE (accepted approximation): the mask is the BLOCK-level ``any_nan`` (OR
    over conditions). On a bar where one operand is NaN while another present
    operand already makes the AND-conjunction genuinely False, the drop is
    treated as a gap (carried) rather than a confirmed drop. This is the
    option-(a) design: prefer suppressing phantom re-arms over registering a drop
    that cannot be fully confirmed under missing data.

    When ``nan_mask`` is ``None`` the historical ``active[t-1]`` semantics are
    used verbatim (callers that never poison, and the direct golden/unit tests).

    Applied ONLY to the LEVEL-shaped ``active`` of the CNF / cross_count path
    (:func:`_eval_block_activity`). The THEN-chain path is NOT run through this:
    ``_sequence_active`` already emits one impulse per completion, and collapsing
    ADJACENT completions would silently drop a real fire — so pulse is a no-op
    (idempotent by construction) on a chain.
    """
    pulsed = np.zeros_like(active)
    if active.size == 0:
        return pulsed
    if nan_mask is None:
        pulsed[0] = active[0]
        if active.size > 1:
            pulsed[1:] = active[1:] & ~active[:-1]
        return pulsed
    # Carry the last valid (non-gap) level across NaN holes. ``prev_level`` starts
    # False (== "not armed"), so the first real True fires (matches leading-gap /
    # first-bar historical behavior).
    prev_level = False
    for t in range(active.size):
        if bool(nan_mask[t]):
            # Data gap: never a fire, and does not disturb the carried level.
            continue
        cur = bool(active[t])
        pulsed[t] = cur and not prev_level
        prev_level = cur
    return pulsed


def _sequence_active(
    stage_truth: list[npt.NDArray[np.bool_]],
    stage_nan: list[npt.NDArray[np.bool_]],
    windows: list[int],
    T: int,
    chain_reset: npt.NDArray[np.bool_] | None = None,
) -> npt.NDArray[np.bool_]:
    """Single forward-only candidate automaton for one linear chain of STAGES.

    Each stage is a conjunction GROUP: ``stage_truth[r][t]`` = every condition in
    group ``r`` matched at bar ``t`` (already NaN-poisoned: a NaN bar is never a
    match). A group of one condition reduces to that condition, so a full chain
    (every gap a THEN) is the special case of one condition per stage.
    ``windows[r]`` = bars allowed from stage ``r``'s match to stage ``r+1``'s
    match (inclusive, strictly after — the successor must land on a LATER bar).
    Returns ``active[T]`` with an IMPULSE True only on the bar the final stage
    completes.

    ``chain_reset`` (Item 3b) is an optional always-on exit-reset array; when
    supplied, a True bar aborts the in-flight candidate BEFORE it can advance at
    that bar (step 2b below). ``None`` on every historical path.

    Per-bar STRICT ORDER (load-bearing — see redteam Findings 1 & 6):
      1. **expire**: if a candidate is in flight and ``t - tau > windows[r]``
         (the window to the next stage elapsed), reset to idle.
      2. **NaN-abort**: a NaN on the awaited stage's operands while in flight
         aborts the candidate (location-independent; does not let the deadline
         tick through a data gap).
      3. **advance**: if in flight and the awaited next stage matches at ``t``
         with ``1 <= t - tau <= windows[r]`` (tested against the PRE-ARM tau),
         advance; on reaching the last stage, FIRE at ``t`` and consume the
         candidate (single in-flight).
      4. **arm**: if the head condition matches at ``t``, (re)arm a candidate at
         stage 0 with ``tau = t`` (latest-start). Arm runs AFTER advance so a
         coincident head+completion advances the OLDER candidate rather than
         silently dropping it.

    Single candidate, forward-only (``tau`` only advances). This is a
    RESTRICTION of the maximal multi-candidate semantics, not an equivalent of
    it: every fire it produces is also a multi-candidate fire (it never fires
    spuriously), but it MAY MISS a completion on 3+-stage chains when the head
    group re-matches mid-chain — the in-flight candidate has already advanced
    past stage 0, so a fresh head arm overwrites it and the older partial
    progress that a multi-candidate scan would have completed is lost. 2-stage
    chains coincide with multi-candidate (only one link, so nothing to drop).
    See ``tests/property/test_temporal_automaton.py`` (subset + 2-stage
    equality). State is O(1): ``(stage, tau)``.
    """
    m = len(stage_truth)
    active = np.zeros(T, dtype=np.bool_)
    if m == 0:
        return active
    if m == 1:
        # Degenerate: a one-condition "chain" is just the condition (impulse ==
        # level for a single stage). Should not happen (windows would be empty)
        # but keep it exact.
        return stage_truth[0].astype(np.bool_, copy=False)

    head = stage_truth[0]
    stage = -1  # -1 == idle (no candidate). 0..m-1 == highest stage reached.
    tau = -1  # bar of the stage-``stage`` match (valid when stage >= 0).
    for t in range(T):
        # 1. expire: window to the NEXT stage (stage+1) has elapsed.
        if stage >= 0 and stage < m - 1:
            if t - tau > windows[stage]:
                stage = -1
                tau = -1
        # 2. NaN-abort: a NaN on the awaited next stage's operands aborts.
        if stage >= 0 and stage < m - 1:
            if bool(stage_nan[stage + 1][t]):
                stage = -1
                tau = -1
        # 2b. exit-reset abort (Item 3b): a targeting exit firing at ``t``
        # aborts any in-flight candidate BEFORE it can advance/complete at this
        # bar. ``chain_reset`` is None on every historical path. A fresh head at
        # the same bar may still re-arm below (step 4) — that is a NEW candidate.
        if chain_reset is not None and bool(chain_reset[t]):
            stage = -1
            tau = -1
        # 3. advance against the PRE-ARM tau (strictly-after >= 1 bar).
        if stage >= 0 and stage < m - 1:
            nxt = stage + 1
            if bool(stage_truth[nxt][t]) and 1 <= (t - tau) <= windows[stage]:
                stage = nxt
                tau = t
                if stage == m - 1:
                    active[t] = True
                    stage = -1  # impulse: consume, ready to re-detect.
                    tau = -1
        # 4. arm head (latest-start) — AFTER advance.
        if bool(head[t]):
            stage = 0
            tau = t
    return active


def _eval_block_activity(
    block: Block,
    indicators: dict[str, IndicatorSpecInput],
    inputs: dict[str, Input],
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
    reset_fire: npt.NDArray[np.bool_] | None = None,
    chain_reset: npt.NDArray[np.bool_] | None = None,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """Evaluate a block to ``(active, any_nan)``.

    ``reset_fire`` (the block's bound reset-block firing bars) is threaded to
    each condition's :func:`_eval_condition` and consumed ONLY by a
    ``CrossCondition`` in ``count_mode="since_reset"``. It is ``None`` in every
    default path (the caller supplies it only for a block that actually uses
    ``since_reset``), so the historical CNF / chain code paths are unchanged.

    ``chain_reset`` (Item 3b, always-on exit reset) is the OR of the firing bars
    of the exits that TARGET this entry. It is ``None`` on every historical path
    (no exits, or exits not targeting this block). When supplied it (a) aborts
    any in-flight temporal candidate at that bar via :func:`_sequence_active`,
    and (b) is OR-ed into the reset threaded to ``since_reset`` cross counters so
    a targeting exit zeroes the tap ladder — independently of the block's own
    ``requires_reset_block_id``. Combining it at this level (rather than adding a
    parameter to :func:`_cross_since_reset`) is behaviourally identical:
    ``_cross_since_reset`` already zeroes ``seen`` on any ``reset_fire[t]`` bar.
    """
    # Combined per-condition reset for ``since_reset`` counters: the block's
    # bound reset OR the always-on exit reset. None only when BOTH are None
    # (the historical default), so ``_eval_condition`` sees an unchanged input.
    if chain_reset is None:
        cond_reset = reset_fire
    elif reset_fire is None:
        cond_reset = chain_reset
    else:
        cond_reset = reset_fire | chain_reset

    groups_windows = _link_groups(block)
    if groups_windows is None:
        # Zero-link / single-group CNF — the LITERAL historical path. Do not
        # refactor. (``cond_reset`` equals ``reset_fire`` unless an exit targets
        # this block; non-``since_reset`` conditions ignore it either way.)
        active = np.ones(T, dtype=np.bool_)
        any_nan = np.zeros(T, dtype=np.bool_)
        for cond in block.conditions:
            c_truth, c_nan = _eval_condition(
                cond, indicators, inputs, values_by_key, T, cond_reset
            )
            active &= c_truth
            any_nan |= c_nan
        if block.fire_mode == "pulse":
            # Thread the block's per-bar NaN mask so an interior data gap is not
            # read as a level drop / phantom re-arm (sweep-2 MINOR-1).
            active = _to_pulse(active, any_nan)
        return active, any_nan
    # Temporal grouping: per-condition truths are AND-reduced within each
    # conjunction group, and the GROUP arrays feed the single-candidate
    # automaton (one stage per group). ``any_nan`` stays the OR over ALL
    # conditions (G2: NaN-poison preserved — the downstream nan_poison mask and
    # the per-input position zeroing are unchanged).
    groups, windows = groups_windows
    cond_truth: list[npt.NDArray[np.bool_]] = []
    cond_nan: list[npt.NDArray[np.bool_]] = []
    any_nan = np.zeros(T, dtype=np.bool_)
    for cond in block.conditions:
        c_truth, c_nan = _eval_condition(
            cond, indicators, inputs, values_by_key, T, cond_reset
        )
        cond_truth.append(c_truth)
        cond_nan.append(c_nan)
        any_nan |= c_nan
    stage_truth: list[npt.NDArray[np.bool_]] = []
    stage_nan: list[npt.NDArray[np.bool_]] = []
    for grp in groups:
        gt = np.ones(T, dtype=np.bool_)
        gn = np.zeros(T, dtype=np.bool_)
        for i in grp:
            gt = gt & cond_truth[i]
            gn = gn | cond_nan[i]
        stage_truth.append(gt)
        stage_nan.append(gn)
    active = _sequence_active(stage_truth, stage_nan, windows, T, chain_reset)
    # No ``_to_pulse`` on the chain path. ``_sequence_active`` already emits an
    # IMPULSE (one True bar per completion) — which IS what ``fire_mode="pulse"``
    # means for a THEN-chain — so a rising-edge pass would be redundant AND
    # wrong: two completions on ADJACENT bars (a coincident head re-arms and the
    # next stage matches the very next bar) form ``[..,1,1,..]``, and
    # ``_to_pulse`` would collapse the second, silently dropping a real
    # completion. Pulse is therefore idempotent on a chain BY CONSTRUCTION, and
    # pulse/sustained coincide here (a discrete completion has no LEVEL to
    # sustain). ``_to_pulse`` is applied ONLY on the level-shaped CNF /
    # cross_count path above, where rising-edge of a sustained level is correct.
    return active, any_nan


def _compound_clamped(
    net_step: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Compound a per-step net return into a wipeout-clamped equity ratio.

    Given ``net_step`` of length ``T-1`` (the netted per-bar return over each
    step), return ``(equity_ratio, step_scale)`` where:

    * ``equity_ratio`` (length ``T``) starts at 1.0 and multiplies by
      ``1 + net_step[s]`` each step. Ruin is ABSORBING: as soon as a step's
      growth factor is ``<= 0`` (a leveraged/short bar that would take equity
      to zero or below), the ratio is pinned to ``0.0`` for that bar and every
      bar after — it never goes negative and never recovers. NaN/inf factors
      (should not occur — steps are guarded upstream) are treated the same as
      ``<= 0`` so the curve stays finite.
    * ``step_scale`` (length ``T-1``) is the per-step weight in ``[0, 1]`` that
      caps the loss on the wiping bar so per-input cumulative CONTRIBUTIONS
      (built as ``Σ step_scale[s]·equity_ratio[s]·contrib_step_i[s]``)
      reconcile to ``equity_ratio - 1`` (to floating-point tolerance). It is
      1.0 before any wipeout, ``-1/net_step[s*]`` on the wiping step ``s*`` (so
      the whole-account loss is the remaining equity, i.e. -100% of it), and
      0.0 afterwards.

    See section 6 of :func:`evaluate_signal` for how the two are consumed.
    """
    n = net_step.size
    T = n + 1
    ratio = np.ones(T, dtype=np.float64)
    step_scale = np.ones(n, dtype=np.float64)
    factors = 1.0 + net_step
    wiped = False
    for s in range(n):
        if wiped:
            ratio[s + 1] = 0.0
            step_scale[s] = 0.0
            continue
        f = factors[s]
        if not np.isfinite(f) or f <= 0.0:
            # The bar's full netted loss would overshoot ruin; cap it so the
            # account loses exactly its remaining equity (factor → 0). When
            # ``net_step[s] == 0`` the factor can only be ``<= 0`` via a
            # non-finite value; treat that as a full wipe (scale 0).
            ratio[s + 1] = 0.0
            step_scale[s] = (-1.0 / net_step[s]) if net_step[s] != 0.0 else 0.0
            wiped = True
        else:
            ratio[s + 1] = ratio[s] * f
    return ratio, step_scale


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentPositionResult:
    """Per-(referenced) input output returned by :func:`evaluate_signal`.

    ``values`` is the latched net position (sum over latched entries of
    ``sign(weight) * |weight|/100``). ``clipped_mask`` is always False
    (leverage is allowed; retained for API shape compatibility).
    """

    input_id: str
    instrument: InputInstrument
    values: npt.NDArray[np.float64]
    clipped_mask: npt.NDArray[np.bool_]
    realized_pnl: npt.NDArray[np.float64]
    price_label: str | None = None
    price_values: npt.NDArray[np.float64] | None = None


@dataclass(frozen=True)
class BlockEvent:
    """Per-block firing / latching / active record (v4 trace schema).

    * ``kind``: ``"entry"``, ``"exit"`` or ``"reset"``.
    * ``fired_indices``: bars where the block's AND-condition was True.
    * ``latched_indices``: for entries, bars where the latch transitioned
      False→True ("effective entry"); for exits, bars where the exit
      actually cleared a previously-open entry latch ("effective exit");
      for resets, bars where AT LEAST ONE bound block (entry or exit)
      had its per-block arm flipped False→True by this reset's fire
      ("effective arm" — one entry per reset fire that armed ≥1 block).
    * ``active_indices``: entries only — bars where this entry's latch
      was True *at emission time* (i.e. contributed to position[t]).
      Empty for exit and reset blocks.
    * ``target_entry_block_names``: exits only — the names of the
      entries this exit targets (one or more). Empty tuple ``()`` on
      entries and resets. For resets the ``input_id`` field is always
      ``""`` (resets are signal-global). On exits, ``input_id`` carries
      the operating input of the FIRST resolvable targeted entry (a
      cross-input exit spans several inputs; the per-target detail lives
      in ``target_entry_block_names``).

    The frontend computes the "don't repeat" effective filter directly
    from these: effective entry bars = ``latched_indices`` on entry
    blocks, effective exit bars = ``latched_indices`` on exit blocks.
    """

    input_id: str
    block_id: str
    kind: Literal["entry", "exit", "reset"]
    fired_indices: tuple[int, ...]
    latched_indices: tuple[int, ...]
    active_indices: tuple[int, ...] = ()
    target_entry_block_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndicatorSeriesResult:
    input_id: str
    indicator_id: str
    series: npt.NDArray[np.float64]
    params_override: dict[str, float | int | bool] | None = None


@dataclass(frozen=True)
class SignalEvalResult:
    index: npt.NDArray[np.int64]
    positions: tuple[InstrumentPositionResult, ...]
    clipped: bool
    events: tuple[BlockEvent, ...]
    indicator_series: tuple[IndicatorSeriesResult, ...]
    diagnostics: dict[str, object]
    trades: tuple[Trade, ...] = ()
    # Capital-free compounded equity curve for the whole signal, treated as
    # ONE account on the net per-bar exposure: ``equity_ratio[0] == 1.0`` and
    # ``equity_ratio[t] = Π_{s<=t}(1 + Σ_i pos_i[s-1]·r_i[s])`` clamped at 0
    # (ruin is absorbing). Multiply by a starting capital to get the equity
    # curve. ``Σ_i positions[i].realized_pnl[t] == equity_ratio[t] - 1`` to
    # floating-point tolerance (per-input realized_pnl are cumulative
    # contributions to this one curve).
    equity_ratio: npt.NDArray[np.float64] = field(
        default_factory=lambda: np.array([], dtype=np.float64)
    )
    # Cumulative transaction cost as PERCENT of initial capital (1.0), tracked
    # SEPARATELY. 0.0 when the cost feature is off (0 bps). MAY exceed 100% for
    # high-turnover strategies.
    total_slippage_paid_pct: float = 0.0
    total_fees_paid_pct: float = 0.0


async def evaluate_signal(
    signal: Signal,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
    cost_config: CostConfig | None = None,
) -> SignalEvalResult:
    """Evaluate a v4 ``Signal`` and return per-input positions + events."""

    # ── 1. Input table ──
    inputs = _input_table(signal)

    # ── 2. Identify usable entry / exit blocks ──
    entry_blocks: list[Block] = [
        b for b in signal.rules.entries if _usable_entry(b, inputs)
    ]
    entry_ids: set[str] = {b.id for b in entry_blocks}
    # Duplicate ids within entries are an invariant break.
    if len(entry_ids) != len(entry_blocks):
        raise SignalValidationError(
            "duplicate entry block id within signal.rules.entries"
        )

    # Index usable entries by name once; exits derive their operating
    # input from their target entry via this map.
    entry_names: set[str] = {b.name for b in entry_blocks if b.name}
    if len(entry_names) != len([b for b in entry_blocks if b.name]):
        raise SignalValidationError(
            "duplicate entry block name within signal.rules.entries"
        )
    entries_by_name: dict[str, Block] = {b.name: b for b in entry_blocks if b.name}

    exit_blocks: list[Block] = [
        b for b in signal.rules.exits if _usable_exit(b, inputs, entry_names)
    ]
    # Exits may have duplicate ids across entries; but distinct ids from
    # each other are preferred for trace clarity — not enforced here.

    reset_blocks: list[Block] = [b for b in signal.rules.resets if _usable_reset(b)]

    # Referenced inputs = union of usable blocks' input_ids, in
    # declaration order (entries then exits). Exits contribute the
    # input_id of each targeted entry (there is no block-level input_id
    # on exits); a cross-input exit therefore contributes several.
    referenced_ids: list[str] = []
    seen_ids: set[str] = set()
    for blk in entry_blocks:
        if blk.input_id in seen_ids:
            continue
        seen_ids.add(blk.input_id)
        referenced_ids.append(blk.input_id)
    for blk in exit_blocks:
        for iid in _exit_input_ids(blk, entries_by_name):
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            referenced_ids.append(iid)

    # ── 3. Collect operands + implicit close-price operands per input ──
    operands: list[Operand] = list(_walk_operands(signal, inputs, entry_names))
    for ref_id in referenced_ids:
        operands.append(InstrumentOperand(input_id=ref_id, field="close"))

    unique_keys: dict[tuple, Operand] = {}
    for op in operands:
        unique_keys.setdefault(_operand_key(op, indicators, inputs), op)

    resolved: dict[
        tuple,
        tuple[
            npt.NDArray[np.int64] | None,
            npt.NDArray[np.float64] | None,
            float | None,
        ],
    ] = {}
    for key, op in unique_keys.items():
        resolved[key] = await _resolve_operand(op, indicators, inputs, fetcher)

    index, values_by_key = _union_align(resolved)
    T = index.size

    # ── 3b. Hold-mode option roll info (fixed-contract dollar-P&L side-channel) ──
    #
    # A hold-mode (``hold_between_rolls``) option input needs, beyond its held
    # premium LEVEL (already in ``values_by_key``), the resolver's roll structure
    # (``is_roll`` + each segment's roll-day OPEN premium) to run the
    # fixed-contract dollar-P&L recurrence.  The fetcher exposes it OPTIONALLY as
    # ``fetch_hold_roll_info(instrument) -> (dates, is_roll, roll_premium)``.  We
    # consult it ONLY for referenced hold-mode option inputs and align each array
    # onto ``index`` exactly as ``_union_align`` does; a hold input whose fetcher
    # lacks the capability is a wiring error (loud, not silent — the $-P&L path
    # cannot be run without the roll structure).
    # Each entry: (is_roll_aligned, roll_premium_aligned, roll_future_ref_aligned,
    # mult_fut, mult_opt).  ``roll_future_ref_aligned`` is None + the multipliers are
    # NaN for a premium_notional leg (they are read only in futures_notional mode).
    hold_roll_info: dict[
        str,
        tuple[
            npt.NDArray[np.float64],
            npt.NDArray[np.float64],
            "npt.NDArray[np.float64] | None",
            float,
            float,
        ],
    ] = {}
    if T > 0:
        _roll_fetch = getattr(fetcher, "fetch_hold_roll_info", None)
        _mult_fetch = getattr(fetcher, "fetch_hold_multipliers", None)
        for ref_id in referenced_ids:
            inp = inputs[ref_id]
            if not (
                isinstance(inp.instrument, InstrumentOptionStream)
                and inp.instrument.hold_between_rolls
            ):
                continue
            if _roll_fetch is None:
                raise SignalDataError(
                    f"input {ref_id!r}: hold_between_rolls option requires the "
                    f"fetcher to provide 'fetch_hold_roll_info' (fixed-contract "
                    f"dollar-P&L roll structure); none available"
                )
            # 3→4-tuple ripple (Guardrail Sign 4): the production fetcher returns
            # ``(dates, is_roll, roll_premium, roll_future_ref)``; a legacy 3-tuple
            # double (premium_notional only) still works — roll_future_ref → None.
            _rres = await _roll_fetch(inp.instrument)
            if len(_rres) == 4:
                r_dates, r_is_roll, r_roll_premium, r_roll_fref = _rres
            else:
                r_dates, r_is_roll, r_roll_premium = _rres
                r_roll_fref = None
            is_roll_aligned = _align_series_to_index(
                r_dates, r_is_roll.astype(np.float64), index, fill=0.0
            )
            roll_premium_aligned = _align_series_to_index(
                r_dates, r_roll_premium.astype(np.float64), index, fill=np.nan
            )
            fut_mode = inp.instrument.sizing_mode == "futures_notional"
            roll_fref_aligned: "npt.NDArray[np.float64] | None" = None
            mult_fut = float("nan")
            mult_opt = float("nan")
            if fut_mode:
                if r_roll_fref is not None:
                    roll_fref_aligned = _align_series_to_index(
                        r_dates,
                        np.asarray(r_roll_fref, dtype=np.float64),
                        index,
                        fill=np.nan,
                    )
                # Multipliers come from the fetcher side-channel (live-first /
                # config fallback resolved in the core layer — the engine never
                # reads dwh).  A NaN pair triggers the engine's tail carry-forward.
                if _mult_fetch is not None:
                    mult_fut, mult_opt = await _mult_fetch(inp.instrument)
            hold_roll_info[ref_id] = (
                is_roll_aligned,
                roll_premium_aligned,
                roll_fref_aligned,
                float(mult_fut),
                float(mult_opt),
            )

    if T == 0:
        return SignalEvalResult(
            index=np.array([], dtype=np.int64),
            positions=tuple(
                InstrumentPositionResult(
                    input_id=rid,
                    instrument=inputs[rid].instrument,
                    values=np.array([], dtype=np.float64),
                    clipped_mask=np.array([], dtype=np.bool_),
                    realized_pnl=np.array([], dtype=np.float64),
                    price_label=None,
                    price_values=None,
                )
                for rid in referenced_ids
            ),
            clipped=False,
            events=(),
            indicator_series=(),
            equity_ratio=np.array([], dtype=np.float64),
            diagnostics={"T": 0, "inputs": len(referenced_ids)},
        )

    # ── 4. Per-block condition truth + nan-poison ──
    #
    # OPTIONAL reset-fire side-channel for ``count_mode="since_reset"`` cross
    # counts (Feature 2). A since_reset block's crossing counter resets when its
    # BOUND reset block (``requires_reset_block_id``) fires. We compute each such
    # reset's firing array ONCE (cached by reset id) and thread it into that
    # block's activity eval. Reset firing = ``reset_active & ~reset_nan`` (the
    # same ``reset_fired`` semantics used in the sequential loop) and is a pure
    # function of ``values_by_key`` — independent of entry/exit results — so it is
    # safe to evaluate here. This whole block is skipped unless a block actually
    # uses since_reset, keeping the default path byte-identical.
    usable_reset_by_id: dict[str, Block] = {b.id: b for b in reset_blocks}
    reset_fire_cache: dict[str, npt.NDArray[np.bool_]] = {}

    def _reset_fire_for(block: Block) -> npt.NDArray[np.bool_] | None:
        if not _block_uses_since_reset(block):
            return None
        rid = block.requires_reset_block_id
        if not rid or rid not in usable_reset_by_id:
            # since_reset with no usable bound reset → "never reset" (None lets
            # _eval_condition fall back to an all-False reset_fire).
            return None
        if rid not in reset_fire_cache:
            r_active, r_nan = _eval_block_activity(
                usable_reset_by_id[rid], indicators, inputs, values_by_key, T
            )
            reset_fire_cache[rid] = r_active & ~r_nan
        return reset_fire_cache[rid]

    # Exits are evaluated FIRST (Item 3b): an exit's own ``active`` does not
    # depend on any entry's chain state, so we can build each targeted entry's
    # always-on ``chain_reset`` (OR of the firing bars of the exits targeting it)
    # before evaluating the entries. ``chain_reset`` is None for any entry no
    # exit targets, so signals without exits keep the byte-identical path.
    exit_truth: dict[str, npt.NDArray[np.bool_]] = {}
    exit_nan: dict[str, npt.NDArray[np.bool_]] = {}
    for blk in exit_blocks:
        active, blk_nan = _eval_block_activity(
            blk, indicators, inputs, values_by_key, T, _reset_fire_for(blk)
        )
        exit_truth[blk.id] = active
        exit_nan[blk.id] = blk_nan

    # Per-entry exit-reset array: OR of ``exit_active & ~exit_nan`` over every
    # exit whose ``target_entry_block_names`` includes this entry's name. A
    # NaN-guarded firing (matching the reset_fire convention) — NOT gated by the
    # exit's own arm; the exit reset is unconditional/always-on per spec. None
    # when no exit targets the entry.
    entry_chain_reset: dict[str, npt.NDArray[np.bool_] | None] = {}
    for entry in entry_blocks:
        acc: npt.NDArray[np.bool_] | None = None
        if entry.name:
            for xb in exit_blocks:
                if entry.name in xb.target_entry_block_names:
                    fire = exit_truth[xb.id] & ~exit_nan[xb.id]
                    acc = fire if acc is None else (acc | fire)
        entry_chain_reset[entry.id] = acc

    entry_truth: dict[str, npt.NDArray[np.bool_]] = {}
    entry_nan: dict[str, npt.NDArray[np.bool_]] = {}
    for blk in entry_blocks:
        active, blk_nan = _eval_block_activity(
            blk,
            indicators,
            inputs,
            values_by_key,
            T,
            _reset_fire_for(blk),
            entry_chain_reset[blk.id],
        )
        entry_truth[blk.id] = active
        entry_nan[blk.id] = blk_nan

    # Reset blocks evaluate the same condition vocabulary. Their nan-mask
    # poisons the resets' own ``fired``/``latched`` traces but is NOT
    # aggregated into ``nan_poison`` (which zeroes per-input positions),
    # because resets are signal-global and don't bind to an input.
    reset_truth: dict[str, npt.NDArray[np.bool_]] = {}
    reset_nan: dict[str, npt.NDArray[np.bool_]] = {}
    for blk in reset_blocks:
        active, blk_nan = _eval_block_activity(
            blk, indicators, inputs, values_by_key, T
        )
        reset_truth[blk.id] = active
        reset_nan[blk.id] = blk_nan

    # Per-input nan-poison mask: union of every usable block's nan mask
    # bound to that input (preserves v3 semantics — nan bar zeroes output
    # but does NOT clear latches).
    nan_poison: dict[str, npt.NDArray[np.bool_]] = {
        rid: np.zeros(T, dtype=np.bool_) for rid in referenced_ids
    }
    for blk in entry_blocks:
        if blk.input_id in nan_poison:
            nan_poison[blk.input_id] = nan_poison[blk.input_id] | entry_nan[blk.id]
    for blk in exit_blocks:
        # A cross-input exit's NaN mask poisons every input it operates on.
        for iid in _exit_input_ids(blk, entries_by_name):
            if iid in nan_poison:
                nan_poison[iid] = nan_poison[iid] | exit_nan[blk.id]

    # ── 5. Latch state + per-bar positions (sequential) ──
    #
    # latches[entry_block_id] = bool.
    latched: dict[str, bool] = {b.id: False for b in entry_blocks}

    # Signed contribution of each latched entry block (sign * |w|/100).
    signed_weight: dict[str, float] = {
        b.id: (1.0 if b.weight > 0 else -1.0) * abs(float(b.weight)) / 100.0
        for b in entry_blocks
    }

    # Entry block by id (for position summation).
    entry_by_id: dict[str, Block] = {b.id: b for b in entry_blocks}

    # Output buffer: per-input net position.
    position: dict[str, npt.NDArray[np.float64]] = {
        rid: np.zeros(T, dtype=np.float64) for rid in referenced_ids
    }

    # Event accumulators.
    entry_fired: dict[str, list[int]] = {b.id: [] for b in entry_blocks}
    entry_latched: dict[str, list[int]] = {b.id: [] for b in entry_blocks}
    entry_active: dict[str, list[int]] = {b.id: [] for b in entry_blocks}
    exit_fired: dict[str, list[int]] = {b.id: [] for b in exit_blocks}
    exit_latched: dict[str, list[int]] = {b.id: [] for b in exit_blocks}
    reset_fired: dict[str, list[int]] = {b.id: [] for b in reset_blocks}
    reset_latched: dict[str, list[int]] = {b.id: [] for b in reset_blocks}

    # ``bound_target`` only contains entries/exits with a non-None
    # ``requires_reset_block_id``; unbound blocks short-circuit the
    # arm check via the ``b.id in block_arm`` membership test.
    bound_target: dict[str, str] = {
        b.id: b.requires_reset_block_id
        for b in (entry_blocks + exit_blocks)
        if b.requires_reset_block_id is not None
    }
    # Per-binding cumulative re-arm count (>= 1). With count = N the bound
    # reset must fire N times (CUMULATIVE) after a disarm before the block
    # re-arms. Clamped to >= 1 defensively (the API validates, but a
    # directly-constructed Signal could carry a smaller value).
    bound_count: dict[str, int] = {
        b.id: max(1, int(b.requires_reset_count))
        for b in (entry_blocks + exit_blocks)
        if b.requires_reset_block_id is not None
    }
    # All bound blocks start armed — first fire of a bound block does
    # NOT require a prior reset (it consumes the initial arm).
    block_arm: dict[str, bool] = {b_id: True for b_id in bound_target}
    # Remaining bound-reset fires needed to re-arm a DISARMED block. Seeded
    # to its count on each disarm; decremented once per bound-reset fire;
    # at 0 the block re-arms. Entries absent/irrelevant while armed.
    arm_countdown: dict[str, int] = {}

    # Per-entry trade ledger — parallel lists keyed by entry block id.
    # ``opens[i]`` pairs with ``closes[i]`` (when present) to form one
    # trade. ``closes[i]`` records (close_bar, exit_block_id) for the
    # exit that actually cleared this latch open.
    trade_opens: dict[str, list[int]] = {b.id: [] for b in entry_blocks}
    trade_closes: dict[str, list[tuple[int, str]]] = {b.id: [] for b in entry_blocks}

    for t in range(T):
        # --- (a) record fired-indices ---
        for b in entry_blocks:
            if bool(entry_truth[b.id][t]):
                entry_fired[b.id].append(t)
        for b in exit_blocks:
            if bool(exit_truth[b.id][t]):
                exit_fired[b.id].append(t)
        for b in reset_blocks:
            # Reset firing inherits the same nan-poison semantics as
            # entries/exits — operands with NaN at t do not count as fired.
            if bool(reset_truth[b.id][t]) and not bool(reset_nan[b.id][t]):
                reset_fired[b.id].append(t)

        # --- (b) clear pass: exits clear every targeted entry latch ---
        for b in exit_blocks:
            if not bool(exit_truth[b.id][t]):
                continue
            # Per-block arm gate (Sign 1): bound exits need their arm
            # True. Unbound exits short-circuit via membership test. The
            # gate is per-EXIT and applies to the WHOLE firing — one
            # firing closes all targets and consumes one arm.
            if b.id in block_arm and not block_arm[b.id]:
                continue
            # Loop over every targeted entry name. Targets are resolved
            # against usable entries only; unresolvable names are ignored
            # (the API rejects dangling names, but a directly-constructed
            # Signal could carry them). All resolvable, currently-open
            # targets are cleared at this same bar t.
            cleared_any = False
            for target_name in b.target_entry_block_names:
                target_entry = entries_by_name.get(target_name)
                # Position-state guard (Sign 3) preserved INDEPENDENTLY of
                # the arm — only an actually-open target latch can clear.
                if target_entry and latched.get(target_entry.id, False):
                    latched[target_entry.id] = False
                    trade_closes[target_entry.id].append((t, b.id))
                    cleared_any = True
            if cleared_any:
                # Effective exit at t (cleared ≥1 latch). Record once.
                exit_latched[b.id].append(t)
                # Disarm AFTER a successful fire — bound exits only, and
                # only when the firing actually cleared something (matches
                # the single-target semantics: an arm is consumed by an
                # EFFECTIVE exit, not by a fire over zero open targets).
                # Seed the cumulative re-arm countdown to this binding's
                # count.
                if b.id in block_arm:
                    block_arm[b.id] = False
                    arm_countdown[b.id] = bound_count[b.id]

        # --- (c) entry pass: declaration order; leverage allowed ---
        # Bound entries gated by their own per-block arm. Unbound entries
        # short-circuit via membership (unconditional firing, no gate).
        for b in entry_blocks:
            if not bool(entry_truth[b.id][t]):
                continue
            if b.id in block_arm and not block_arm[b.id]:
                continue
            # Position-state guard (Sign 3): a bound entry whose arm is
            # True still cannot double-latch.
            if latched[b.id]:
                continue
            latched[b.id] = True
            entry_latched[b.id].append(t)
            trade_opens[b.id].append(t)
            # Disarm AFTER a successful latch — bound entries only. Seed
            # the cumulative re-arm countdown to this binding's count.
            if b.id in block_arm:
                block_arm[b.id] = False
                arm_countdown[b.id] = bound_count[b.id]

        # --- (c.5) reset pass: per-fire effectiveness (Sign 2).
        # Runs AFTER entries so same-bar entry+reset → entry-at-t,
        # reset-arms-for-t+1. ``latched_indices`` records the bar iff
        # ≥1 bound block transitioned False→True via this fire.
        for r in reset_blocks:
            if not bool(reset_truth[r.id][t]) or bool(reset_nan[r.id][t]):
                continue
            armed_at_least_one = False
            for b_id in block_arm:
                if bound_target[b_id] != r.id:
                    continue
                if block_arm[b_id]:
                    # Already armed → this fire is a no-op for this block
                    # (Sign 2; matches T7/B8/B10). No countdown, no marker.
                    continue
                # Disarmed: this fire counts toward the cumulative re-arm.
                # ``arm_countdown`` was seeded on disarm; one fire = -1.
                arm_countdown[b_id] -= 1
                if arm_countdown[b_id] <= 0:
                    block_arm[b_id] = True
                    armed_at_least_one = True
                # else: still counting down → no transition → ineffective.
            if armed_at_least_one:
                reset_latched[r.id].append(t)

        # --- (d) emit per-input net position at t ---
        for rid in referenced_ids:
            acc = 0.0
            for bid, open_ in latched.items():
                if not open_:
                    continue
                blk = entry_by_id[bid]
                if blk.input_id == rid:
                    acc += signed_weight[bid]
                    entry_active[bid].append(t)
            position[rid][t] = acc

    # ── 6. Assemble per-input results + the compounded net-exposure curve ──
    #
    # The signal is ONE account. Each input contributes a per-bar
    # position-weighted simple return ``contrib_step_i[t] = pos_i[t-1]·r_i[t]``
    # (``r_i`` is the guarded close-to-close return; the SAME finite /
    # prev!=0 / nan-poison guards as before — invalid bars contribute 0).
    # The net per-bar return is the SUM across inputs, and the equity curve
    # compounds that single netted return:
    #
    #     net_step[t]     = Σ_i contrib_step_i[t]
    #     equity_ratio[t] = Π_{s<=t}(1 + net_step[s])          (clamped at 0)
    #
    # Per-input ``realized_pnl`` is then the cumulative CONTRIBUTION (as a
    # fraction of starting capital) to that one curve, using the equity at
    # the START of each bar as the capital actually deployed:
    #
    #     realized_pnl_i[t] = Σ_{s<=t} equity_ratio[s-1]·contrib_step_i[s]
    #
    # which reconciles to floating-point tolerance:
    #     Σ_i realized_pnl_i[t] == equity_ratio[t] - 1.
    # NEVER cumprod per input then sum — that double-counts cross-exposure.

    # 6a. Per-input metadata + guarded per-bar contribution steps.
    @dataclass
    class _InputAccum:
        ref_id: str
        instrument: InputInstrument
        pos: npt.NDArray[np.float64]
        price_label: str | None
        price_values: npt.NDArray[np.float64] | None
        contrib_step: npt.NDArray[np.float64]  # length T-1 (0 when T<2)
        # Non-None for a hold-mode option input: its fixed-contract dollar-P&L
        # spec.  Such an input's ``contrib_step`` is NOT computed here (it is
        # equity-coupled) — it is filled in by ``_compound_with_hold`` in 6b.
        hold_spec: "_HoldPnLSpec | None" = None

    accums: list[_InputAccum] = []
    for ref_id in referenced_ids:
        inp = inputs[ref_id]

        pos = position[ref_id]
        # Feature 1 — OPTIONAL per-input net-position clamp. Applied to the RAW
        # net latched position (before the no-quote NaN masking below) so a
        # positive lower bound never fabricates exposure on a flat/no-data bar,
        # and BEFORE the return calc so contrib_step / realized_pnl / the equity
        # curve all see the clamped exposure. ``position_cap is None`` (default)
        # skips the clip entirely → BYTE-IDENTICAL to the historical path.
        cap = getattr(inp, "position_cap", None)
        if cap is not None:
            lo_cap, hi_cap = float(cap[0]), float(cap[1])
            pos = np.clip(pos, lo_cap, hi_cap)
        pos = np.where(nan_poison[ref_id], 0.0, pos)

        price_label: str | None = None
        price_values: npt.NDArray[np.float64] | None = None
        key = (
            "instrument",
            _instrument_identity(inp.instrument),
            "close",
        )
        if key in values_by_key:
            if isinstance(inp.instrument, InstrumentSpot):
                price_label = f"{inp.instrument.instrument_id}.close"
            elif isinstance(inp.instrument, InstrumentOptionStream):
                # Both modes emit the option premium (mid) LEVEL — in hold mode it
                # is the HELD contract's mid, otherwise the daily-reselected mid.
                # Label it as the stream either way (it IS a premium level, not a
                # return).
                price_label = f"{inp.instrument.collection}.{inp.instrument.stream}"
            elif isinstance(inp.instrument, InstrumentBasket):
                # Baskets identify themselves by ``basket_id`` (saved) or
                # by their declared ``asset_class`` (inline) so the price
                # label remains stable across the polymorphic-leg shape.
                if inp.instrument.basket_id is not None:
                    price_label = f"basket:{inp.instrument.basket_id}.close"
                else:
                    price_label = f"basket:inline[{inp.instrument.asset_class}].close"
            else:
                price_label = f"{inp.instrument.collection}.continuous.close"
            price_values = values_by_key[key]

        # A SELECT-AND-HOLD option stream (``hold_between_rolls``) books
        # FIXED-CONTRACT DOLLAR P&L, not a price %-return: the held premium LEVEL
        # (``price_values``) plus the roll structure (``hold_roll_info``) drive the
        # equity-coupled recurrence in ``_compound_with_hold`` (6b).  Its
        # ``contrib_step`` is left ZERO here and filled in there.  DEFAULT-OFF
        # option streams (and every non-option input) take the price-level
        # ``Δprice/price`` branch below, byte-identical to before.
        _hold_mode_option = (
            isinstance(inp.instrument, InstrumentOptionStream)
            and inp.instrument.hold_between_rolls
        )
        contrib_step = np.zeros(max(T - 1, 0), dtype=np.float64)
        hold_spec: _HoldPnLSpec | None = None
        if _hold_mode_option and price_values is not None and ref_id in hold_roll_info:
            (
                is_roll_arr,
                roll_premium_arr,
                roll_fref_arr,
                mult_fut,
                mult_opt,
            ) = hold_roll_info[ref_id]
            # Direction is the SIGN of the net latched position; ``nav_times`` is
            # the SIZE (a separate field, may exceed the |weight| the sign carries).
            # ``pos`` already folds every latched block's signed weight for this
            # input, so its sign is the leg's direction and its non-zero mask is
            # "position open".  The MAGNITUDE of pos is NOT used for sizing (that is
            # nav_times) — only its sign + open/closed state.
            with np.errstate(invalid="ignore"):
                pos_sign = np.sign(pos)
            # A single hold-mode option input is driven by one entry block's sign;
            # if multiple blocks on the same input disagree in sign the net sign
            # governs (same as the price path's net position).
            nonzero = pos_sign[pos_sign != 0.0]
            leg_sign = float(nonzero[0]) if nonzero.size else 1.0
            hold_spec = _HoldPnLSpec(
                ref_id=ref_id,
                sign=leg_sign,
                nav_times=float(inp.instrument.nav_times),
                premium=price_values,
                is_roll=is_roll_arr > 0.5,
                roll_premium=roll_premium_arr,
                pos_active=pos != 0.0,
                sizing_mode=inp.instrument.sizing_mode,
                roll_future_ref=roll_fref_arr,
                mult_fut=mult_fut,
                mult_opt=mult_opt,
            )
        elif price_values is not None and T >= 2:
            prev_price = price_values[:-1]
            cur_price = price_values[1:]
            valid = (
                np.isfinite(prev_price) & np.isfinite(cur_price) & (prev_price != 0.0)
            )
            with np.errstate(invalid="ignore", divide="ignore"):
                raw = pos[:-1] * (cur_price - prev_price) / prev_price
            contrib_step[valid] = raw[valid]

        accums.append(
            _InputAccum(
                ref_id=ref_id,
                instrument=inp.instrument,
                pos=pos,
                price_label=price_label,
                price_values=price_values,
                contrib_step=contrib_step,
                hold_spec=hold_spec,
            )
        )

    # 6b. Net per-bar return → compounded, wipeout-clamped equity ratio.
    #     ``step_scale`` caps the loss on a wiping bar so the per-input
    #     contributions below reconcile to ``equity_ratio - 1`` through ruin.
    #
    #     Two paths:
    #       * NO hold-mode option input → the vectorized ``_compound_clamped`` of
    #         the summed per-input ``contrib_step`` (byte-identical to before → the
    #         golden-master is unmoved);
    #       * ≥1 hold-mode option input → ``_compound_with_hold``: a SEQUENTIAL
    #         joint pass because a hold leg's contribution is equity-coupled
    #         (contrib[t] ∝ equity_ratio[roll]/equity_ratio[t-1]).  It sums the
    #         vectorized inputs' equity-independent steps AND each hold leg's
    #         fixed-contract dollar-P&L step, applying the SAME ruin clamp, and
    #         returns each hold leg's actual booked ``contrib_step`` so 6c is
    #         uniform.
    equity_ratio = np.ones(T, dtype=np.float64)
    step_scale = np.ones(max(T - 1, 0), dtype=np.float64)
    hold_specs = [acc.hold_spec for acc in accums if acc.hold_spec is not None]

    # ── Transaction costs (OFF by default → byte-identical). Build a per-step
    #    turnover drag and subtract it from the netted per-bar return BEFORE
    #    compounding, so equity/Sharpe/etc. reflect it. Two turnover sources:
    #    (a) VECTORIZED legs — the Σ|w_target−w_drift| formula over the priced,
    #        non-hold inputs' positions + returns;
    #    (b) HOLD option legs — a round-trip (2 sides) on the leg's nav_times
    #        notional at each roll (one side at the initial open). Folding it into
    #        the vectorized step keeps ``_compound_with_hold`` untouched (the roll
    #        drag is a fixed fraction-of-NAV, so it is equity-independent). ──
    _cost_on = cost_config is not None and not cost_config.is_zero() and T >= 2
    slip_drag = np.zeros(max(T - 1, 0), dtype=np.float64)
    fees_drag = np.zeros(max(T - 1, 0), dtype=np.float64)
    total_slippage_paid_pct = 0.0
    total_fees_paid_pct = 0.0
    if _cost_on:
        priced = [
            acc
            for acc in accums
            if acc.hold_spec is None and acc.price_values is not None
        ]
        turnover = np.zeros(T - 1, dtype=np.float64)
        if priced:
            k = len(priced)
            pos_mat = np.empty((T, k), dtype=np.float64)
            rets_mat = np.full((T, k), np.nan, dtype=np.float64)
            gross_net = np.zeros(T - 1, dtype=np.float64)
            for j, acc in enumerate(priced):
                pos_mat[:, j] = acc.pos
                pv = np.asarray(acc.price_values, dtype=np.float64)
                with np.errstate(invalid="ignore", divide="ignore"):
                    rets_mat[1:, j] = (pv[1:] - pv[:-1]) / pv[:-1]
                gross_net += acc.contrib_step
            turnover += establish_turnover(pos_mat, rets_mat, gross_net)
        # Hold-leg roll round-trips (one side at the initial open).
        for spec in hold_specs:
            turnover += roll_turnover_from_flags(spec.is_roll, spec.nav_times, T - 1)
        slip_drag, fees_drag = split_drag(turnover, cost_config)
    total_drag = slip_drag + fees_drag

    if T >= 2 and not hold_specs:
        net_step = np.zeros(T - 1, dtype=np.float64)
        for acc in accums:
            net_step += acc.contrib_step
        net_step = net_step - total_drag
        equity_ratio, step_scale = _compound_clamped(net_step)
    elif T >= 2:
        vectorized_net_step = np.zeros(T - 1, dtype=np.float64)
        for acc in accums:
            if acc.hold_spec is None:
                vectorized_net_step += acc.contrib_step
        vectorized_net_step = vectorized_net_step - total_drag
        equity_ratio, step_scale, hold_contrib = _compound_with_hold(
            vectorized_net_step, hold_specs
        )
        # Write each hold leg's actual booked per-step contribution back so its
        # ``realized_pnl`` is built by the SAME 6c formula as every other input.
        for acc in accums:
            if acc.hold_spec is not None:
                acc.contrib_step = hold_contrib[acc.ref_id]

    if _cost_on:
        er_start = equity_ratio[:-1]
        total_slippage_paid_pct = cumulative_cost_pct(slip_drag, er_start)
        total_fees_paid_pct = cumulative_cost_pct(fees_drag, er_start)

    # 6c. Per-input cumulative contributions (deploy prior-bar equity, with
    #     the wipeout loss-cap applied uniformly across inputs on the wiping
    #     bar). ``Σ_i realized_pnl_i == equity_ratio - 1`` to fp tolerance.
    results: list[InstrumentPositionResult] = []
    for acc in accums:
        realized_pnl = np.zeros(T, dtype=np.float64)
        if T >= 2:
            # capital deployed over step s→s+1 is equity_ratio[s] (start of bar).
            realized_pnl[1:] = np.cumsum(
                step_scale * equity_ratio[:-1] * acc.contrib_step
            )
        results.append(
            InstrumentPositionResult(
                input_id=acc.ref_id,
                instrument=acc.instrument,
                values=acc.pos,
                clipped_mask=np.zeros(T, dtype=np.bool_),
                realized_pnl=realized_pnl,
                price_label=acc.price_label,
                price_values=acc.price_values,
            )
        )

    # ── 7. Events payload ──
    events: list[BlockEvent] = []
    for b in entry_blocks:
        events.append(
            BlockEvent(
                input_id=b.input_id,
                block_id=b.id,
                kind="entry",
                fired_indices=tuple(entry_fired[b.id]),
                latched_indices=tuple(entry_latched[b.id]),
                active_indices=tuple(entry_active[b.id]),
                target_entry_block_names=(),
            )
        )
    for b in exit_blocks:
        # ``input_id`` carries the FIRST resolvable target's operating
        # input (the exit is usable, so at least one resolves). The full
        # per-target detail lives in ``target_entry_block_names``; only
        # names that resolve to a usable entry are emitted.
        exit_input_ids = _exit_input_ids(b, entries_by_name)
        resolved_targets = tuple(
            name for name in b.target_entry_block_names if name in entries_by_name
        )
        events.append(
            BlockEvent(
                input_id=exit_input_ids[0] if exit_input_ids else "",
                block_id=b.id,
                kind="exit",
                fired_indices=tuple(exit_fired[b.id]),
                latched_indices=tuple(exit_latched[b.id]),
                active_indices=(),
                target_entry_block_names=resolved_targets,
            )
        )
    for b in reset_blocks:
        events.append(
            BlockEvent(
                input_id="",
                block_id=b.id,
                kind="reset",
                fired_indices=tuple(reset_fired[b.id]),
                latched_indices=tuple(reset_latched[b.id]),
                active_indices=(),
                target_entry_block_names=(),
            )
        )

    # ── 8. Indicator series (expose every unique indicator operand) ──
    # Dedup by full operand key (includes params_override, series_override,
    # and output), so the same indicator used with different parameters on
    # the same input produces separate series in the response.
    indicator_series: list[IndicatorSeriesResult] = []
    seen_keys: set[tuple] = set()
    for op in _walk_operands(signal, inputs, entry_names):
        if not isinstance(op, IndicatorOperand):
            continue
        k = _operand_key(op, indicators, inputs)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        if k in values_by_key:
            indicator_series.append(
                IndicatorSeriesResult(
                    input_id=op.input_id,
                    indicator_id=op.indicator_id,
                    series=values_by_key[k],
                    params_override=(
                        dict(op.params_override) if op.params_override else None
                    ),
                )
            )

    diagnostics: dict[str, object] = {
        "T": int(T),
        "inputs": len(referenced_ids),
    }

    # ── 9. Derive trades by pairing each entry latch-open with its
    #       matching close (same entry, k-th open ↔ k-th close).
    exit_by_id: dict[str, Block] = {b.id: b for b in exit_blocks}
    trades: list[Trade] = []
    for b in entry_blocks:
        opens = trade_opens[b.id]
        closes = trade_closes[b.id]
        direction = "long" if b.weight > 0 else "short"
        sw = signed_weight[b.id]
        for k, open_bar in enumerate(opens):
            if k < len(closes):
                close_bar, exit_id = closes[k]
                exit_blk = exit_by_id.get(exit_id)
                exit_name = exit_blk.name if exit_blk is not None else ""
                trades.append(
                    Trade(
                        input_id=b.input_id,
                        entry_block_id=b.id,
                        entry_block_name=b.name,
                        exit_block_id=exit_id,
                        exit_block_name=exit_name,
                        open_bar=int(open_bar),
                        close_bar=int(close_bar),
                        direction=direction,
                        signed_weight=sw,
                    )
                )
            else:
                trades.append(
                    Trade(
                        input_id=b.input_id,
                        entry_block_id=b.id,
                        entry_block_name=b.name,
                        exit_block_id=None,
                        exit_block_name=None,
                        open_bar=int(open_bar),
                        close_bar=None,
                        direction=direction,
                        signed_weight=sw,
                    )
                )
    trades.sort(key=lambda tr: (tr.open_bar, tr.entry_block_id))

    return SignalEvalResult(
        index=index,
        positions=tuple(results),
        clipped=False,
        events=tuple(events),
        indicator_series=tuple(indicator_series),
        diagnostics=diagnostics,
        trades=tuple(trades),
        equity_ratio=equity_ratio,
        total_slippage_paid_pct=total_slippage_paid_pct,
        total_fees_paid_pct=total_fees_paid_pct,
    )


__all__ = [
    "BlockEvent",
    "IndicatorSeriesResult",
    "IndicatorSpecInput",
    "InstrumentPositionResult",
    "PriceFetcher",
    "SignalDataError",
    "SignalEvalResult",
    "SignalRuntimeError",
    "SignalValidationError",
    "evaluate_signal",
]
