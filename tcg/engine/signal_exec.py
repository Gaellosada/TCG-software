"""Signal evaluator -- pure NumPy + per-bar stateful latching (v4).

v4 — unified Entries/Exits with signed weights
----------------------------------------------
The v3 four-direction model is gone. Each signal declares two block
lists:

  * ``rules.entries`` -- blocks with stable ``id`` and signed
    ``weight`` in ``[-100, +100]``; ``sign(weight)`` decides long/short.
  * ``rules.exits`` -- blocks that each target *exactly one* entry via
    ``target_entry_block_id``. When an exit's AND-condition fires at
    bar ``t``, the referenced entry block's latch is cleared; no other
    latches are touched (no "same-side-under-input" blanket clear).

Per-bar execution (declaration order within each list):

  1. **Clear pass.** For every usable exit block whose condition fires
     at ``t``: look up the entry latch by id, if True set False.
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
  * exit blocks require ``target_entry_block_id`` referencing a usable
    entry block in the same signal's rules (a dangling target makes
    the exit a no-op — the engine tolerates this so latent bad state
    degrades gracefully; the API layer rejects it with HTTP 400). Exit
    blocks do NOT carry their own ``input_id``; the operating input is
    always derived from the target entry's ``input_id``.
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
    InstrumentContinuous,
    InstrumentOperand,
    InstrumentSpot,
    Operand,
    RollingCondition,
    Signal,
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


def _require_input(
    input_id: str, inputs: dict[str, Input], *, ctx: str
) -> Input:
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
                ctx=(
                    f"indicator {op.indicator_id!r} series_override"
                    f"[{label!r}]"
                ),
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
            eff_label_inst[label] = InstrumentSpot(
                collection=coll, instrument_id=iid
            )

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
        inp = _require_input(
            operand.input_id, inputs, ctx="instrument operand"
        )
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


def _walk_operands(signal: Signal) -> list[Operand]:
    out: list[Operand] = []
    for rules in (signal.rules.entries, signal.rules.exits):
        for block in rules:
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
        raise SignalValidationError(
            f"indicator {op.indicator_id!r}: {exc}"
        ) from exc
    except IndicatorRuntimeError as exc:
        raise SignalRuntimeError(
            f"indicator {op.indicator_id!r}: {exc}",
            user_traceback=exc.user_traceback,
        ) from exc

    return common.astype(np.int64, copy=False), result.astype(
        np.float64, copy=False
    )


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
        inp = _require_input(
            operand.input_id, inputs, ctx="instrument operand"
        )
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
            raise SignalValidationError(
                f"rolling lookback must be >= 1, got {kk}"
            )
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
    signed weight in (−100..0) ∪ (0..100]."""
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


def _usable_exit(
    block: Block, inputs: dict[str, Input], entry_ids: set[str]
) -> bool:
    """Exit block is usable iff it has id + conditions AND
    target_entry_block_id references a usable entry.

    Exit blocks do not carry their own ``input_id``; the operating input
    is derived from the target entry at execution time.
    """
    if not block.id:
        return False
    if not block.conditions:
        return False
    if not block.target_entry_block_id:
        return False
    if block.target_entry_block_id not in entry_ids:
        return False
    return True


def _exit_input_id(
    exit_block: Block, entries_by_id: dict[str, Block]
) -> str:
    """Return the operating input id for an exit block, derived from its
    target entry.

    Callers must only pass exit blocks that are already known to be
    usable (i.e. ``target_entry_block_id`` resolves in ``entries_by_id``);
    this helper does not re-validate. Returns the target entry's
    ``input_id``.
    """
    target = exit_block.target_entry_block_id
    assert target is not None, "exit_block must have target_entry_block_id"
    entry = entries_by_id[target]
    return entry.input_id


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

    * ``kind``: ``"entry"`` or ``"exit"``.
    * ``fired_indices``: bars where the block's AND-condition was True.
    * ``latched_indices``: for entries, bars where the latch transitioned
      False→True ("effective entry"); for exits, bars where the exit
      actually cleared a previously-open entry latch ("effective exit").
    * ``active_indices``: entries only — bars where this entry's latch
      was True *at emission time* (i.e. contributed to position[t]).
      Empty for exit blocks.
    * ``target_entry_block_id``: exits only — the id of the entry this
      exit targets. ``None`` on entries.

    The frontend computes the "don't repeat" effective filter directly
    from these: effective entry bars = ``latched_indices`` on entry
    blocks, effective exit bars = ``latched_indices`` on exit blocks.
    """

    input_id: str
    block_id: str
    kind: Literal["entry", "exit"]
    fired_indices: tuple[int, ...]
    latched_indices: tuple[int, ...]
    active_indices: tuple[int, ...] = ()
    target_entry_block_id: str | None = None


@dataclass(frozen=True)
class IndicatorSeriesResult:
    input_id: str
    indicator_id: str
    series: npt.NDArray[np.float64]


@dataclass(frozen=True)
class SignalEvalResult:
    index: npt.NDArray[np.int64]
    positions: tuple[InstrumentPositionResult, ...]
    clipped: bool
    events: tuple[BlockEvent, ...]
    indicator_series: tuple[IndicatorSeriesResult, ...]
    diagnostics: dict[str, object]


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

    # Index usable entries by id once; exits derive their operating
    # input from their target entry via this map.
    entries_by_id: dict[str, Block] = {b.id: b for b in entry_blocks}

    exit_blocks: list[Block] = [
        b for b in signal.rules.exits if _usable_exit(b, inputs, entry_ids)
    ]
    # Exits may have duplicate ids across entries; but distinct ids from
    # each other are preferred for trace clarity — not enforced here.

    # Referenced inputs = union of usable blocks' input_ids, in
    # declaration order (entries then exits). Exits contribute the
    # target entry's input_id (there is no block-level input_id on exits).
    referenced_ids: list[str] = []
    seen_ids: set[str] = set()
    for blk in entry_blocks:
        if blk.input_id in seen_ids:
            continue
        seen_ids.add(blk.input_id)
        referenced_ids.append(blk.input_id)
    for blk in exit_blocks:
        iid = _exit_input_id(blk, entries_by_id)
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        referenced_ids.append(iid)

    # ── 3. Collect operands + implicit close-price operands per input ──
    operands: list[Operand] = list(_walk_operands(signal))
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
        iid = _exit_input_id(blk, entries_by_id)
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

    # Entry block by id (for position summation). Re-bind the earlier
    # ``entries_by_id`` under a local name for readability below.
    entry_by_id: dict[str, Block] = entries_by_id

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

    for t in range(T):
        # --- (a) record fired-indices ---
        for b in entry_blocks:
            if bool(entry_truth[b.id][t]):
                entry_fired[b.id].append(t)
        for b in exit_blocks:
            if bool(exit_truth[b.id][t]):
                exit_fired[b.id].append(t)

        # --- (b) clear pass: exits clear their target-entry latch only ---
        for b in exit_blocks:
            if not bool(exit_truth[b.id][t]):
                continue
            target = b.target_entry_block_id  # validated usable above
            # Effective exit = only when the target was actually open.
            if latched.get(target, False):
                latched[target] = False
                exit_latched[b.id].append(t)

        # --- (c) entry pass: declaration order; leverage allowed ---
        for b in entry_blocks:
            if not bool(entry_truth[b.id][t]):
                continue
            if latched[b.id]:
                continue
            latched[b.id] = True
            entry_latched[b.id].append(t)

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
            else:
                price_label = f"{inp.instrument.collection}.continuous.close"
            price_values = values_by_key[key]

        realized_pnl = np.zeros(T, dtype=np.float64)
        if price_values is not None and T >= 2:
            prev_price = price_values[:-1]
            cur_price = price_values[1:]
            valid = (
                np.isfinite(prev_price)
                & np.isfinite(cur_price)
                & (prev_price != 0.0)
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
                target_entry_block_id=None,
            )
        )
    for b in exit_blocks:
        events.append(
            BlockEvent(
                input_id=_exit_input_id(b, entries_by_id),
                block_id=b.id,
                kind="exit",
                fired_indices=tuple(exit_fired[b.id]),
                latched_indices=tuple(exit_latched[b.id]),
                active_indices=(),
                target_entry_block_id=b.target_entry_block_id,
            )
        )

    # ── 8. Indicator series (expose every indicator operand value) ──
    indicator_series: list[IndicatorSeriesResult] = []
    seen_pairs: set[tuple[str, str]] = set()
    for op in _walk_operands(signal):
        if not isinstance(op, IndicatorOperand):
            continue
        pair = (op.input_id, op.indicator_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        k = _operand_key(op, indicators, inputs)
        if k in values_by_key:
            indicator_series.append(
                IndicatorSeriesResult(
                    input_id=op.input_id,
                    indicator_id=op.indicator_id,
                    series=values_by_key[k],
                )
            )

    diagnostics: dict[str, object] = {
        "T": int(T),
        "inputs": len(referenced_ids),
    }

    return SignalEvalResult(
        index=index,
        positions=tuple(results),
        clipped=False,
        events=tuple(events),
        indicator_series=tuple(indicator_series),
        diagnostics=diagnostics,
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
