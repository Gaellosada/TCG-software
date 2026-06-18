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

from dataclasses import dataclass
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
            inst.adjustment,
            int(inst.roll_offset),
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
        return truth, nan_at_t

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


def _eval_block_activity(
    block: Block,
    indicators: dict[str, IndicatorSpecInput],
    inputs: dict[str, Input],
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    active = np.ones(T, dtype=np.bool_)
    any_nan = np.zeros(T, dtype=np.bool_)
    for cond in block.conditions:
        c_truth, c_nan = _eval_condition(cond, indicators, inputs, values_by_key, T)
        active &= c_truth
        any_nan |= c_nan
    return active, any_nan


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

    # ── 6. Assemble per-input results (prices, pnl, clipped mask) ──
    results: list[InstrumentPositionResult] = []
    for ref_id in referenced_ids:
        inp = inputs[ref_id]

        pos = position[ref_id]
        pos = np.where(nan_poison[ref_id], 0.0, pos)

        clipped_mask = np.zeros(T, dtype=np.bool_)

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

        realized_pnl = np.zeros(T, dtype=np.float64)
        if price_values is not None and T >= 2:
            prev_price = price_values[:-1]
            cur_price = price_values[1:]
            valid = (
                np.isfinite(prev_price) & np.isfinite(cur_price) & (prev_price != 0.0)
            )
            step = np.zeros(T - 1, dtype=np.float64)
            with np.errstate(invalid="ignore", divide="ignore"):
                raw = pos[:-1] * (cur_price - prev_price) / prev_price
            step[valid] = raw[valid]
            realized_pnl[1:] = np.cumsum(step)

        results.append(
            InstrumentPositionResult(
                input_id=ref_id,
                instrument=inp.instrument,
                values=pos,
                clipped_mask=clipped_mask,
                realized_pnl=realized_pnl,
                price_label=price_label,
                price_values=price_values,
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
