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
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
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


def _chain_window_list(block: Block) -> list[int] | None:
    """Return the per-link window list for a temporal chain, or ``None`` for a
    zero-link (pure-CNF) block.

    ``block.links`` maps successor-condition index → ``within`` window in bars.
    A valid v1 chain is ONE contiguous forward chain over indices
    ``1..len(conditions)-1`` (keys ``{1, 2, ..., m-1}``) with finite windows
    ``>= 1``; a window of 0 is treated as a non-link (W=0 folds to plain AND).
    The API layer validates and rejects malformed ``links`` (HTTP 400); here we
    defensively treat anything that is not a clean full forward chain of
    positive windows as "no chain" so a directly-constructed Signal degrades to
    CNF rather than misbehaving.

    Returns a list ``W`` of length ``m-1`` where ``W[i]`` is the window from
    condition ``i`` to condition ``i+1`` (``i`` in ``0..m-2``). Returns ``None``
    when there are no usable links.
    """
    links = block.links
    if not links:
        return None
    m = len(block.conditions)
    if m < 2:
        return None
    # Drop W<=0 entries (W=0 == non-link). Keep only positive windows.
    pos = {int(kk): int(vv) for kk, vv in links.items() if int(vv) >= 1}
    if not pos:
        return None
    # Must be a single contiguous forward chain starting at index 1:
    # keys exactly {1, 2, ..., k} for some k in 1..m-1 (no gaps, no index 0,
    # no index >= m). Anything else degrades to CNF here (API rejects upstream).
    keys = sorted(pos)
    if keys[0] != 1 or keys[-1] >= m:
        return None
    if keys != list(range(1, keys[-1] + 1)):
        return None
    # Build the window list; links only cover indices 1..keys[-1]. A chain that
    # stops short of the last condition is not a single linear chain over the
    # whole block, so require it to reach the final condition.
    if keys[-1] != m - 1:
        return None
    return [pos[i + 1] for i in range(m - 1)]


def _sequence_active(
    stage_truth: list[npt.NDArray[np.bool_]],
    stage_nan: list[npt.NDArray[np.bool_]],
    windows: list[int],
    T: int,
) -> npt.NDArray[np.bool_]:
    """Single forward-only candidate automaton for one linear chain.

    ``stage_truth[r][t]`` = condition ``r`` matched at bar ``t`` (already
    NaN-poisoned: a NaN bar is never a match). ``windows[r]`` = bars allowed
    from stage ``r``'s match to stage ``r+1``'s match (inclusive, strictly
    after — the successor must land on a LATER bar). Returns ``active[T]`` with
    an IMPULSE True only on the bar the final stage completes.

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

    Single candidate, forward-only (``tau`` only advances), so for a single
    linear chain it cannot miss a completion that a multi-candidate scan would
    catch (redteam Finding 1 honest assessment). State is O(1): ``(stage, tau)``.
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
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    windows = _chain_window_list(block)
    if windows is None:
        # Zero-link CNF — the LITERAL historical path. Do not refactor.
        active = np.ones(T, dtype=np.bool_)
        any_nan = np.zeros(T, dtype=np.bool_)
        for cond in block.conditions:
            c_truth, c_nan = _eval_condition(cond, indicators, inputs, values_by_key, T)
            active &= c_truth
            any_nan |= c_nan
        return active, any_nan
    # Temporal chain: per-condition truths feed the single-candidate automaton.
    # ``any_nan`` stays the OR over ALL conditions (G2: NaN-poison preserved —
    # the downstream nan_poison mask and the per-input position zeroing are
    # unchanged). The automaton only READS already-poisoned truth/nan.
    stage_truth: list[npt.NDArray[np.bool_]] = []
    stage_nan: list[npt.NDArray[np.bool_]] = []
    any_nan = np.zeros(T, dtype=np.bool_)
    for cond in block.conditions:
        c_truth, c_nan = _eval_condition(cond, indicators, inputs, values_by_key, T)
        stage_truth.append(c_truth)
        stage_nan.append(c_nan)
        any_nan |= c_nan
    active = _sequence_active(stage_truth, stage_nan, windows, T)
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
# Fixed-contract dollar-P&L for held option positions (hold_between_rolls)
# ---------------------------------------------------------------------------


@dataclass
class _HoldPnLSpec:
    """Per-(hold-mode option input) data for the fixed-contract dollar-P&L path.

    Aligned to the signal's union date axis (length ``T``).  Direction is the
    block-weight SIGN (``sign``); ``nav_times`` is the premium-notional size (NOT
    ``|weight|/100`` — that is the whole reason ``nav_times`` is a separate field).

    * ``premium`` — the HELD contract's mid LEVEL of the contract owning each
      date's value (the resolver's hold-mode ``values``: OLD contract's mid on a
      roll day, held contract otherwise).
    * ``is_roll`` — True at each hold segment's first date (incl. the initial
      open); a roll RESIZES the held quantity off the post-P&L NAV.
    * ``roll_premium`` — at each ``is_roll`` date, the NEW segment's roll-day OPEN
      mid: the base for that segment's daily P&L and its quantity sizing (the ONLY
      place the NEW open premium is surfaced — ``premium`` on a roll date is the
      OLD mid, so the seam is exact, never a raw old→new level gap).
    * ``pos_active`` — per-bar 0/1: whether the input's net position is open
      (latched) on the step START.  A closed position contributes 0 that step; a
      re-open mid-hold is treated as a fresh open at the current premium (a new
      sizing point) so the $-P&L only accrues while the leg is actually held.
    """

    ref_id: str
    sign: float
    nav_times: float
    premium: npt.NDArray[np.float64]
    is_roll: npt.NDArray[np.bool_]
    roll_premium: npt.NDArray[np.float64]
    pos_active: npt.NDArray[np.bool_]


def _compound_with_hold(
    vectorized_net_step: npt.NDArray[np.float64],
    hold_specs: list[_HoldPnLSpec],
) -> tuple[
    npt.NDArray[np.float64],
    npt.NDArray[np.float64],
    dict[str, npt.NDArray[np.float64]],
]:
    """Sequential joint compounding for a mix of vectorized inputs and hold-mode
    option inputs (fixed-contract dollar P&L).

    ``vectorized_net_step`` (length ``T-1``) is the SUM of every non-hold input's
    equity-independent ``contrib_step`` (``pos·Δprice/price`` etc.).  Each entry
    of ``hold_specs`` contributes, PER STEP ``s`` (from bar ``s`` to ``s+1``),

        contrib = sign · nav_times · (equity_ratio[roll] / equity_ratio[s])
                         · (premium[s+1] − base) / premium[roll]

    where ``base`` is the current segment's roll-day open premium on the step
    right after a roll, else ``premium[s]`` (interior); ``premium[roll]`` and
    ``equity_ratio[roll]`` are frozen at the segment's roll.  This is the
    fraction-of-current-NAV form of ``qty·Δpremium`` with the held quantity sized
    once per roll off the compounding NAV — verified equal to the Java oracle NAV
    ratio to machine epsilon.  Because it reads ``equity_ratio[s]`` (the running
    JOINT equity at the step start), the whole account is compounded in ONE
    sequential pass; the vectorized inputs' per-step contributions are added in.

    Returns ``(equity_ratio, step_scale, hold_contrib_steps)`` where:
      * ``equity_ratio`` (length ``T``), ``step_scale`` (length ``T-1``) have the
        SAME meaning as :func:`_compound_clamped` (absorbing ruin clamp; the loss
        cap on the wiping step), so the existing per-input ``realized_pnl`` builder
        (``cumsum(step_scale·equity_ratio[:-1]·contrib_step)``) reconciles to
        ``equity_ratio − 1``;
      * ``hold_contrib_steps[ref_id]`` (length ``T-1``) is each hold input's ACTUAL
        booked per-step contribution (pre-clamp; the clamp is applied uniformly via
        ``step_scale`` in the realized_pnl builder, exactly as for vectorized
        inputs) so its ``realized_pnl`` can be built the same way.
    """
    n = vectorized_net_step.size  # T-1
    T = n + 1
    ratio = np.ones(T, dtype=np.float64)
    step_scale = np.ones(max(n, 0), dtype=np.float64)
    hold_contrib: dict[str, npt.NDArray[np.float64]] = {
        spec.ref_id: np.zeros(max(n, 0), dtype=np.float64) for spec in hold_specs
    }

    # Per-hold-spec running segment state: the roll-day open premium and the
    # equity_ratio captured at the segment's roll (both frozen until the next
    # roll).  ``seg_premium`` is NaN until the leg's first valid open; while NaN
    # the leg books 0 (not yet sized / no quote to size against).  ``holding``
    # tracks whether a sized position is currently held.
    seg_premium: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}
    seg_er: dict[str, float] = {spec.ref_id: 1.0 for spec in hold_specs}
    holding: dict[str, bool] = {spec.ref_id: False for spec in hold_specs}
    # Last FINITE premium of the held contract, carried forward as the interior
    # P&L base across a no-quote (NaN) day — matching the oracle ``java_faithful_s1``
    # (its ``prev_premium`` only updates on a finite premium; a NaN books 0 but does
    # NOT reset the base, so the first finite day after a gap captures the WHOLE
    # move ``qty·(premium_t − last_finite_premium)``).  Reset to the segment open at
    # each roll/open point.  On a gapless segment this equals ``premium[s]`` on every
    # interior step, so the default (continuous-quote) path is byte-identical.
    last_finite: dict[str, float] = {spec.ref_id: np.nan for spec in hold_specs}

    # Seed bar-0 sizing: the loop below sizes at bar s+1, so the initial open at
    # bar 0 (a leg latched at bar 0, whose first date is a segment open) must be
    # sized here off ratio[0]==1 and bar 0's open premium.  A leg not yet open at
    # bar 0 stays flat until its first latch bar, where the loop sizes it.
    for spec in hold_specs:
        rid = spec.ref_id
        if T >= 1 and bool(spec.pos_active[0]):
            open_prem = (
                spec.roll_premium[0] if bool(spec.is_roll[0]) else spec.premium[0]
            )
            if np.isfinite(open_prem) and open_prem > 0.0:
                seg_premium[rid] = float(open_prem)
                seg_er[rid] = ratio[0]  # == 1.0
                holding[rid] = True
                last_finite[rid] = float(open_prem)  # carry-forward base seed

    wiped = False
    for s in range(n):
        if wiped:
            ratio[s + 1] = 0.0
            step_scale[s] = 0.0
            continue

        net = float(vectorized_net_step[s])

        # Book each hold leg's step P&L on the quantity held INTO bar s+1 (sized
        # at the leg's current segment: seg_premium/seg_er, frozen at its roll).
        # The step-owner's move is (premium[s+1] − base): interior → base is the
        # held mid on bar s (premium[s]); the FIRST step of a segment (previous
        # bar was that segment's roll) → base is the segment's roll-day OPEN
        # (roll_premium[s]), NOT premium[s] (which on a roll bar is the OLD mid).
        for spec in hold_specs:
            rid = spec.ref_id
            contrib = 0.0
            if (
                holding[rid]
                and bool(spec.pos_active[s])
                and bool(spec.pos_active[s + 1])
                and ratio[s] != 0.0
            ):
                # Interior base = the LAST FINITE held premium (carried forward
                # across a no-quote day), so a gap books its full move on the next
                # finite day instead of dropping it (matches the oracle's
                # ``prev_premium``).  A roll bar uses the NEW segment's open
                # (roll_premium[s]) — the seam is exact, never carried across.  On a
                # gapless segment ``last_finite`` == ``premium[s]`` here, so this is
                # byte-identical to the prior behaviour.
                base = (
                    spec.roll_premium[s] if bool(spec.is_roll[s]) else last_finite[rid]
                )
                cur = spec.premium[s + 1]
                seg_p = seg_premium[rid]
                dprem = cur - base
                if (
                    np.isfinite(dprem)
                    and np.isfinite(base)
                    and np.isfinite(seg_p)
                    and seg_p != 0.0
                ):
                    contrib = (
                        spec.sign
                        * spec.nav_times
                        * (seg_er[rid] / ratio[s])
                        * dprem
                        / seg_p
                    )
                # Carry the last FINITE held premium forward as the next interior
                # step's base (the oracle updates ``prev_premium`` only on a finite
                # premium — a NaN leaves the base unchanged).
                if np.isfinite(cur):
                    last_finite[rid] = float(cur)
            hold_contrib[rid][s] = contrib
            net += contrib

        # Advance the joint equity with the absorbing ruin clamp (identical to
        # _compound_clamped) — this is the equity_ratio the NEXT step's hold
        # contribs read via ratio[s+1].
        f = 1.0 + net
        if not np.isfinite(f) or f <= 0.0:
            ratio[s + 1] = 0.0
            step_scale[s] = (-1.0 / net) if net != 0.0 else 0.0
            wiped = True
        else:
            ratio[s + 1] = ratio[s] * f

        # AFTER booking bar s+1: (re)size each hold leg whose bar s+1 is a roll or
        # a fresh open, off the POST-step NAV (ratio[s+1]) and the segment's
        # roll-day open premium.  A roll realises the OLD (already folded into
        # ratio[s+1], seam-free) and opens the NEW; a fresh latch-open sizes at the
        # current premium.  Sizing after the step means seg_er = ratio[s+1] — the
        # verified oracle ordering (qty_new = nav_times·NAV_at_roll/premium_roll).
        for spec in hold_specs:
            rid = spec.ref_id
            active_next = bool(spec.pos_active[s + 1])
            if not active_next:
                # Position closed at or before bar s+1 → drop the sizing (a later
                # re-open re-sizes fresh).
                holding[rid] = False
                continue
            is_open_point = bool(spec.is_roll[s + 1]) or not holding[rid]
            if is_open_point:
                open_prem = (
                    spec.roll_premium[s + 1]
                    if bool(spec.is_roll[s + 1])
                    else spec.premium[s + 1]
                )
                if np.isfinite(open_prem) and open_prem > 0.0 and ratio[s + 1] != 0.0:
                    seg_premium[rid] = float(open_prem)
                    seg_er[rid] = ratio[s + 1]
                    holding[rid] = True
                    # A NEW segment's carry-forward base restarts at its OPEN premium
                    # (the seam is exact — never carry the OLD segment's last finite,
                    # nor the roll-day OLD mid that ``premium[s+1]`` holds, across).
                    last_finite[rid] = float(open_prem)
                elif not holding[rid]:
                    # Cannot size (no quotable open premium) → stay flat.
                    holding[rid] = False

    return ratio, step_scale, hold_contrib


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


async def evaluate_signal(
    signal: Signal,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
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
    hold_roll_info: dict[
        str, tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]
    ] = {}
    if T > 0:
        _roll_fetch = getattr(fetcher, "fetch_hold_roll_info", None)
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
            r_dates, r_is_roll, r_roll_premium = await _roll_fetch(inp.instrument)
            is_roll_aligned = _align_series_to_index(
                r_dates, r_is_roll.astype(np.float64), index, fill=0.0
            )
            roll_premium_aligned = _align_series_to_index(
                r_dates, r_roll_premium.astype(np.float64), index, fill=np.nan
            )
            hold_roll_info[ref_id] = (is_roll_aligned, roll_premium_aligned)

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
    entry_truth: dict[str, npt.NDArray[np.bool_]] = {}
    entry_nan: dict[str, npt.NDArray[np.bool_]] = {}
    for blk in entry_blocks:
        active, blk_nan = _eval_block_activity(
            blk, indicators, inputs, values_by_key, T
        )
        entry_truth[blk.id] = active
        entry_nan[blk.id] = blk_nan

    exit_truth: dict[str, npt.NDArray[np.bool_]] = {}
    exit_nan: dict[str, npt.NDArray[np.bool_]] = {}
    for blk in exit_blocks:
        active, blk_nan = _eval_block_activity(
            blk, indicators, inputs, values_by_key, T
        )
        exit_truth[blk.id] = active
        exit_nan[blk.id] = blk_nan

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
            is_roll_arr, roll_premium_arr = hold_roll_info[ref_id]
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
    if T >= 2 and not hold_specs:
        net_step = np.zeros(T - 1, dtype=np.float64)
        for acc in accums:
            net_step += acc.contrib_step
        equity_ratio, step_scale = _compound_clamped(net_step)
    elif T >= 2:
        vectorized_net_step = np.zeros(T - 1, dtype=np.float64)
        for acc in accums:
            if acc.hold_spec is None:
                vectorized_net_step += acc.contrib_step
        equity_ratio, step_scale, hold_contrib = _compound_with_hold(
            vectorized_net_step, hold_specs
        )
        # Write each hold leg's actual booked per-step contribution back so its
        # ``realized_pnl`` is built by the SAME 6c formula as every other input.
        for acc in accums:
            if acc.hold_spec is not None:
                acc.contrib_step = hold_contrib[acc.ref_id]

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
