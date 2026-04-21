"""Signal evaluator -- pure NumPy + per-bar stateful latching (iter-5).

v3 (iter-4) — inputs replace block/operand instruments
------------------------------------------------------
Every signal declares a top-level ``inputs`` list; blocks reference an
input by id, and operands (instrument + indicator) bind through an input.
This means rebinding an input swaps the instrument for every block and
every operand referencing it — the single code-path is now the bound
input.

iter-5 semantics — latched positions with leverage
---------------------------------------------------
Positions are no longer stateless per-bar sums. Each entry block owns a
boolean latch keyed by (input_id, block_id, side). Per bar ``t``, in
declaration order of blocks:

  1. **Clear pass (same-side only).** For each long_exit block whose
     AND-condition fires at ``t`` and whose ``input_id`` matches, clear
     every long latch under the same input. Same for short_exit/short.
     Cross-side clearing is forbidden.
  2. **Entry pass.** For each entry block in declaration order whose
     condition fires at ``t`` AND whose latch is currently False:
     set latch True. No budget cap — leverage is allowed (total weight
     across all inputs and both sides may exceed 1.0).

After both passes at bar ``t``:

    long_pos_I(t)  = Σ latched long weights for input I
    short_pos_I(t) = Σ latched short weights for input I
    position_I(t)  = long_pos_I(t) − short_pos_I(t)   # unbounded
    position_I(t)  = 0 if any-nan-poison at t (preserved from iter-4)

Latch state is per-(input, block); exit clearing is per-input-per-side
(never cross-side, guardrail N6).

A block is "usable" iff
  * it has ≥1 condition;
  * ``input_id`` resolves to a declared Input;
  * entry tabs additionally require ``weight > 0``;
  * every operand's ``input_id`` resolves;
  * the bound Input's instrument is fully configured.

Indicator operand resolution (v3)
---------------------------------
For each :class:`IndicatorOperand`:
  * the primary series-map label (first by declaration order) receives the
    instrument of ``inputs[operand.input_id]``;
  * ``series_override[label] = some_input_id`` remaps ``label`` to that
    input's instrument;
  * ``params_override`` is merged on top of the indicator's base params.

Instruments that cannot be fetched (unsupported ``InstrumentContinuous``
when the fetcher protocol doesn't support it) surface as
:class:`SignalDataError`.
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
# Indicator override merge (v3)
# ---------------------------------------------------------------------------


def _merge_indicator_effective(
    op: IndicatorOperand,
    base: IndicatorSpecInput,
    inputs: dict[str, Input],
) -> tuple[
    dict[str, float | int | bool],
    dict[str, InputInstrument],
]:
    """Compute effective (params, per-label-instrument) after v3 merge.

    Resolution:
      * ``params_override`` merged on top of ``base.params``.
      * Each label in ``base.series_labels`` gets an instrument:
          * If ``series_override[label]`` is set → its input's instrument.
          * Else if label is the PRIMARY (index 0) → operand's input.
          * Else → fallback: a synthetic :class:`InstrumentSpot` from
            the base ``series_map[label]`` (preserves v2 multi-label
            indicators where only the primary was ever a user-facing
            input).
    """
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
    for rules in (
        signal.rules.long_entry,
        signal.rules.long_exit,
        signal.rules.short_entry,
        signal.rules.short_exit,
    ):
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
        pos = np.searchsorted(dates, index)
        safe_pos = np.clip(pos, 0, dates.size - 1)
        match = dates[safe_pos] == index
        out = np.full(index.size, np.nan, dtype=np.float64)
        out[match] = vals[safe_pos[match]]
        values_by_key[key] = out

    return index, values_by_key


# ---------------------------------------------------------------------------
# Condition evaluation (vectorised)
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


def _is_usable_block(
    block: Block,
    *,
    is_entry: bool,
    inputs: dict[str, Input],
) -> bool:
    """A block participates iff it has ≥1 condition, a valid input_id,
    and (for entry tabs) a strictly positive weight.
    """
    if not block.conditions:
        return False
    if not block.input_id or block.input_id not in inputs:
        return False
    if is_entry and not (block.weight > 0.0):
        return False
    return True


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

    iter-5:
      * ``values`` is now the latched net position (sum of latched long
        weights − sum of latched short weights).
      * ``clipped_mask`` is always False (leverage is allowed; retained
        for API contract compatibility).
      * ``realized_pnl`` is a per-bar cumulative return contribution for
        this input:
            pnl(0)   = 0
            pnl(t)   = pnl(t-1) + position(t-1) * (price(t) − price(t-1))
                       / price(t-1)   (when price(t-1) finite & non-zero)
        NaN prices produce a 0-step for that bar (no drift on missing
        data).
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
    """Per-block firing/latching record emitted in the response (P5-6)."""

    input_id: str
    block_id: str
    kind: Literal["long_entry", "long_exit", "short_entry", "short_exit"]
    fired_indices: tuple[int, ...]
    latched_indices: tuple[int, ...]


@dataclass(frozen=True)
class IndicatorSeriesResult:
    """Resolved indicator operand series exposed alongside positions."""

    input_id: str
    indicator_id: str
    series: npt.NDArray[np.float64]


@dataclass(frozen=True)
class SignalEvalResult:
    """Top-level evaluation result (v3 + iter-5 extensions)."""

    index: npt.NDArray[np.int64]
    positions: tuple[InstrumentPositionResult, ...]
    clipped: bool
    events: tuple[BlockEvent, ...]
    indicator_series: tuple[IndicatorSeriesResult, ...]
    entries_skipped_budget: int
    diagnostics: dict[str, object]


async def evaluate_signal(
    signal: Signal,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
) -> SignalEvalResult:
    """Evaluate a v3 ``Signal`` and return per-input positions + clip flag."""

    # ── 1. Input table ──
    inputs = _input_table(signal)

    # ── 2. Identify referenced inputs (via USABLE blocks only) ──
    referenced_ids: list[str] = []
    seen_ids: set[str] = set()
    for rules_tuple, is_entry_tab in (
        (signal.rules.long_entry, True),
        (signal.rules.long_exit, False),
        (signal.rules.short_entry, True),
        (signal.rules.short_exit, False),
    ):
        for blk in rules_tuple:
            if not _is_usable_block(blk, is_entry=is_entry_tab, inputs=inputs):
                continue
            if blk.input_id in seen_ids:
                continue
            seen_ids.add(blk.input_id)
            referenced_ids.append(blk.input_id)

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
            entries_skipped_budget=0,
            diagnostics={"T": 0, "inputs": len(referenced_ids)},
        )

    # ── 4. Build per-(input, side, block_index) tables of usable blocks ──
    #
    # Each usable block is assigned a stable declaration-order id within
    # its side ("{kind}#{idx}" where idx is zero-based declaration order
    # in the side's tuple). Entries are processed in declaration order
    # so latching order is deterministic.
    BlockRec = tuple[
        str,  # block_id
        Literal[
            "long_entry", "long_exit", "short_entry", "short_exit"
        ],  # kind
        Block,
        npt.NDArray[np.bool_],  # condition truth (T-vector)
        npt.NDArray[np.bool_],  # condition nan mask (T-vector)
    ]

    def _side_records(
        rules_tuple: tuple[Block, ...],
        *,
        is_entry: bool,
        kind: Literal[
            "long_entry", "long_exit", "short_entry", "short_exit"
        ],
    ) -> list[BlockRec]:
        recs: list[BlockRec] = []
        for idx, blk in enumerate(rules_tuple):
            if not _is_usable_block(blk, is_entry=is_entry, inputs=inputs):
                continue
            active, blk_nan = _eval_block_activity(
                blk, indicators, inputs, values_by_key, T
            )
            recs.append((f"{kind}#{idx}", kind, blk, active, blk_nan))
        return recs

    long_entry_recs = _side_records(
        signal.rules.long_entry, is_entry=True, kind="long_entry"
    )
    short_entry_recs = _side_records(
        signal.rules.short_entry, is_entry=True, kind="short_entry"
    )
    long_exit_recs = _side_records(
        signal.rules.long_exit, is_entry=False, kind="long_exit"
    )
    short_exit_recs = _side_records(
        signal.rules.short_exit, is_entry=False, kind="short_exit"
    )

    # Per-input nan-poison mask: union of every usable block's condition
    # nan mask for blocks bound to that input (preserves iter-4
    # semantics — nan bar zeroes output but does NOT clear latches).
    nan_poison: dict[str, npt.NDArray[np.bool_]] = {
        rid: np.zeros(T, dtype=np.bool_) for rid in referenced_ids
    }
    for recs in (
        long_entry_recs, short_entry_recs, long_exit_recs, short_exit_recs
    ):
        for _bid, _kind, blk, _truth, blk_nan in recs:
            if blk.input_id in nan_poison:
                nan_poison[blk.input_id] = nan_poison[blk.input_id] | blk_nan

    # ── 5. Latch state + per-bar positions (sequential) ──
    #
    # latches[(input_id, block_id, side)] = bool. Only entry blocks
    # carry latches; exits CLEAR latches.
    latches_long: dict[tuple[str, str], bool] = {}
    latches_short: dict[tuple[str, str], bool] = {}
    for bid, _kind, blk, _truth, _nan in long_entry_recs:
        latches_long[(blk.input_id, bid)] = False
    for bid, _kind, blk, _truth, _nan in short_entry_recs:
        latches_short[(blk.input_id, bid)] = False

    # Output buffers.
    long_pos: dict[str, npt.NDArray[np.float64]] = {
        rid: np.zeros(T, dtype=np.float64) for rid in referenced_ids
    }
    short_pos: dict[str, npt.NDArray[np.float64]] = {
        rid: np.zeros(T, dtype=np.float64) for rid in referenced_ids
    }
    # Event accumulators (fired + latched indices per block).
    event_fired: dict[tuple[str, str], list[int]] = {}
    event_latched: dict[tuple[str, str], list[int]] = {}
    event_meta: dict[
        tuple[str, str],
        tuple[str, Literal["long_entry", "long_exit", "short_entry", "short_exit"]],
    ] = {}
    for recs in (
        long_entry_recs, short_entry_recs, long_exit_recs, short_exit_recs
    ):
        for bid, kind, blk, _truth, _nan in recs:
            key = (blk.input_id, bid)
            event_fired[key] = []
            event_latched[key] = []
            event_meta[key] = (blk.input_id, kind)

    entries_skipped_budget = 0

    # Fast weight lookups keyed by (input_id, block_id).
    _weight_lookup_long: dict[tuple[str, str], float] = {
        (blk.input_id, bid): float(blk.weight)
        for bid, _kind, blk, _truth, _nan in long_entry_recs
    }
    _weight_lookup_short: dict[tuple[str, str], float] = {
        (blk.input_id, bid): float(blk.weight)
        for bid, _kind, blk, _truth, _nan in short_entry_recs
    }
    for t in range(T):
        # --- (a) record fired-indices for ALL usable blocks at t ---
        for recs in (
            long_entry_recs,
            short_entry_recs,
            long_exit_recs,
            short_exit_recs,
        ):
            for bid, _kind, blk, truth, _nan in recs:
                if bool(truth[t]):
                    event_fired[(blk.input_id, bid)].append(t)

        # --- (b) clear-pass: same-side exits clear same-side latches ---
        for bid, _kind, blk, truth, _nan in long_exit_recs:
            if bool(truth[t]):
                # Record the exit's "latched index" = same as fired.
                event_latched[(blk.input_id, bid)].append(t)
                for (lkey_in, lkey_b), v in list(latches_long.items()):
                    if lkey_in == blk.input_id and v:
                        latches_long[(lkey_in, lkey_b)] = False
        for bid, _kind, blk, truth, _nan in short_exit_recs:
            if bool(truth[t]):
                event_latched[(blk.input_id, bid)].append(t)
                for (lkey_in, lkey_b), v in list(latches_short.items()):
                    if lkey_in == blk.input_id and v:
                        latches_short[(lkey_in, lkey_b)] = False

        # --- (c) entry-pass (declaration order; leverage allowed) ---
        for bid, _kind, blk, truth, _nan in long_entry_recs:
            if not bool(truth[t]):
                continue
            if latches_long[(blk.input_id, bid)]:
                continue
            # Leverage is allowed — no budget cap.
            latches_long[(blk.input_id, bid)] = True
            event_latched[(blk.input_id, bid)].append(t)

        for bid, _kind, blk, truth, _nan in short_entry_recs:
            if not bool(truth[t]):
                continue
            if latches_short[(blk.input_id, bid)]:
                continue
            # Leverage is allowed — no budget cap.
            latches_short[(blk.input_id, bid)] = True
            event_latched[(blk.input_id, bid)].append(t)

        # --- (d) emit per-input net position at t ---
        for rid in referenced_ids:
            lsum = 0.0
            ssum = 0.0
            for (in_id, bid), latched in latches_long.items():
                if in_id == rid and latched:
                    lsum += _weight_lookup_long[(in_id, bid)]
            for (in_id, bid), latched in latches_short.items():
                if in_id == rid and latched:
                    ssum += _weight_lookup_short[(in_id, bid)]
            long_pos[rid][t] = lsum
            short_pos[rid][t] = ssum

    # ── 6. Assemble per-input results (prices, pnl, clipped mask) ──
    results: list[InstrumentPositionResult] = []

    for ref_id in referenced_ids:
        inp = inputs[ref_id]

        position = long_pos[ref_id] - short_pos[ref_id]
        position = np.where(nan_poison[ref_id], 0.0, position)

        clipped_mask = np.zeros(T, dtype=np.bool_)

        # Price series overlay — the input's instrument close price.
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
            else:  # continuous
                price_label = f"{inp.instrument.collection}.continuous.close"
            price_values = values_by_key[key]

        # Realized PnL: cumulative of position[t-1] * pct_return[t].
        # NaN-safe: zero contribution when either price is nan/zero.
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
                raw = position[:-1] * (cur_price - prev_price) / prev_price
            step[valid] = raw[valid]
            realized_pnl[1:] = np.cumsum(step)

        results.append(
            InstrumentPositionResult(
                input_id=ref_id,
                instrument=inp.instrument,
                values=position,
                clipped_mask=clipped_mask,
                realized_pnl=realized_pnl,
                price_label=price_label,
                price_values=price_values,
            )
        )

    # ── 7. Events payload ──
    events: list[BlockEvent] = []
    for key, fired_list in event_fired.items():
        in_id, kind = event_meta[key]
        latched_list = event_latched[key]
        events.append(
            BlockEvent(
                input_id=in_id,
                block_id=key[1],
                kind=kind,
                fired_indices=tuple(fired_list),
                latched_indices=tuple(latched_list),
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
        "entries_skipped_budget": int(entries_skipped_budget),
    }

    return SignalEvalResult(
        index=index,
        positions=tuple(results),
        clipped=False,
        events=tuple(events),
        indicator_series=tuple(indicator_series),
        entries_skipped_budget=int(entries_skipped_budget),
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
