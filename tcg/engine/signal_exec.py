"""Signal evaluator -- pure NumPy, vectorised.

v2 (iter-3): per-instrument, weighted composition
--------------------------------------------------
Each :class:`~tcg.types.signal.Block` carries an
``instrument: InstrumentRef | None`` and a ``weight: float``.  For every
unique instrument referenced by any block (across all four directions)
the evaluator emits ONE position series, composed as follows (per
timestep ``t`` and per instrument ``I``):

    long_score_I  = Σ b.weight over active long_entry blocks with b.instrument == I
    short_score_I = Σ b.weight over active short_entry blocks with b.instrument == I
    long_exit_I   = any active long_exit  block with b.instrument == I
    short_exit_I  = any active short_exit block with b.instrument == I
    long_pos_I    = 0 if long_exit_I  else min(long_score_I,  1.0)
    short_pos_I   = 0 if short_exit_I else min(short_score_I, 1.0)
    position_I    = long_pos_I - short_pos_I           # ∈ [-1, 1]
    clipped_I     = (long_score_I  > 1 and not long_exit_I)
                    OR (short_score_I > 1 and not short_exit_I)

An ``active`` block is one for which **every** condition fires at ``t``
(AND-within-block). Blocks whose instrument is ``None`` or whose weight
is ``0.0`` are skipped (sentinel "not yet picked").

Indicator override caching
--------------------------
A :class:`IndicatorOperand` may carry ``params_override`` and
``series_override`` maps.  When present they are merged on top of the
base indicator spec (shipped in the request) before execution. Cache key
includes the frozen merged params + series_map, so two operands that
reference the same indicator with the same overrides share one compute;
different overrides produce separate cached entries.

Design invariants
-----------------
* Operands are resolved ONCE per ``(kind, ...)`` identity. For indicator
  operands the identity folds in the override; for instrument operands
  it folds in the price field; constants are keyed by value.
* Every referenced series is reindexed onto the union of all encountered
  dates; missing timestamps become NaN.  A condition that reads NaN on
  either side evaluates to ``False`` at that step.  Cross-instrument
  "NaN→0" is applied **per instrument**: an instrument's position at
  ``t`` is forced to 0 whenever any operand referenced by any of its
  blocks is NaN at ``t``.
* All math below operates on length-T arrays (``T = |union index|``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable

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
    InRangeCondition,
    IndicatorOperand,
    InstrumentOperand,
    InstrumentRef,
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
    """Inline indicator spec shipped in a signals compute request.

    Maps to what ``/api/indicators/compute`` accepts: ``code``, ``params``,
    and a ``series_map`` from label → ``(collection, instrument_id)``.
    """

    code: str
    params: dict[str, float | int | bool]
    series_map: dict[str, tuple[str, str]]  # label → (collection, instrument_id)


# ---------------------------------------------------------------------------
# Price fetch protocol (injected — avoids a hard dep on the FastAPI layer)
# ---------------------------------------------------------------------------


# (collection, instrument_id) → (dates int64 YYYYMMDD, values float64)
PriceFetcher = Callable[
    [str, str, str],  # collection, instrument_id, field ("close"/"open"/...)
    Awaitable[tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]],
]


# ---------------------------------------------------------------------------
# Override merge + cache key helpers (indicator operand)
# ---------------------------------------------------------------------------


def _merge_indicator_effective(
    op: IndicatorOperand,
    base: IndicatorSpecInput,
) -> tuple[dict[str, float | int | bool], dict[str, tuple[str, str]]]:
    """Produce the (effective_params, effective_series_map) after merging
    an :class:`IndicatorOperand`'s optional overrides on top of ``base``.

    * ``params_override`` -- merged shallowly; override values REPLACE
      matching base keys.  Extra keys not in the base are allowed (the
      indicator's ``compute`` signature determines validity at run time).
    * ``series_override`` -- maps indicator-label → instrument_id string.
      An overridden label keeps the base collection, swapping only the
      instrument_id. If the label is not in ``base.series_map`` the
      override is treated as a fresh entry -- the collection falls back
      to ``"INDEX"`` (v2 default).  That's unusual enough that we log via
      SignalValidationError to prevent silent typos: we require the label
      to exist in the base map.
    """
    eff_params: dict[str, float | int | bool] = dict(base.params)
    if op.params_override:
        for k, v in op.params_override.items():
            eff_params[k] = v  # type: ignore[assignment]

    eff_series: dict[str, tuple[str, str]] = dict(base.series_map)
    if op.series_override:
        for label, new_instrument_id in op.series_override.items():
            if label not in eff_series:
                raise SignalValidationError(
                    f"indicator {op.indicator_id!r}: series_override references "
                    f"unknown label {label!r}; base series_map has "
                    f"{sorted(eff_series.keys())!r}"
                )
            coll, _old = eff_series[label]
            eff_series[label] = (coll, str(new_instrument_id))

    return eff_params, eff_series


def _freeze_params(p: dict[str, float | int | bool]) -> tuple:
    """Canonical hashable key for a params map (sorted by key)."""
    return tuple(sorted((k, v) for k, v in p.items()))


def _freeze_series_map(m: dict[str, tuple[str, str]]) -> tuple:
    """Canonical hashable key for a series map (sorted by label)."""
    return tuple(sorted((label, coll, iid) for label, (coll, iid) in m.items()))


# ---------------------------------------------------------------------------
# Operand cache key
# ---------------------------------------------------------------------------


def _operand_key(
    operand: Operand,
    indicators: dict[str, IndicatorSpecInput] | None = None,
) -> tuple:
    """Cache key identifying an operand's resolved series.

    For indicator operands the key folds in the effective (merged) params
    and series_map so that two references to the same indicator with
    different overrides get distinct cache slots; two references with
    identical overrides share one.
    """
    if isinstance(operand, IndicatorOperand):
        if indicators is None:
            # Pre-validation callers (e.g. _walk_operands for identity) don't
            # need the resolved effective map -- use a coarse key that still
            # distinguishes different override payloads via their repr.
            return (
                "indicator",
                operand.indicator_id,
                operand.output,
                repr(operand.params_override or {}),
                repr(operand.series_override or {}),
            )
        base = indicators.get(operand.indicator_id)
        if base is None:
            # Defer the "missing spec" error to resolution time; for keying
            # purposes treat it uniquely.
            return (
                "indicator",
                operand.indicator_id,
                operand.output,
                ("MISSING",),
                ("MISSING",),
            )
        eff_p, eff_s = _merge_indicator_effective(operand, base)
        return (
            "indicator",
            operand.indicator_id,
            operand.output,
            _freeze_params(eff_p),
            _freeze_series_map(eff_s),
        )
    if isinstance(operand, InstrumentOperand):
        return (
            "instrument",
            operand.collection,
            operand.instrument_id,
            operand.field,
        )
    if isinstance(operand, ConstantOperand):
        return ("constant", float(operand.value))
    raise SignalValidationError(f"unknown operand kind: {operand!r}")


# ---------------------------------------------------------------------------
# Walks
# ---------------------------------------------------------------------------


def _walk_operands(signal: Signal) -> list[Operand]:
    """Walk every operand in the signal in the canonical stable order.

    Order:
    * directions: ``long_entry → long_exit → short_entry → short_exit``
    * blocks in array order;
    * conditions in array order;
    * for binary conditions: ``lhs`` before ``rhs``;
    * for ``in_range``: ``operand`` → ``min`` → ``max``;
    * for rolling: ``operand`` only.
    """
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


def _walk_block_operands(block: Block) -> list[Operand]:
    """Per-block operand walk in the same stable order as ``_walk_operands``."""
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


def _first_instrument_operand_in_block(
    block: Block,
) -> InstrumentOperand | None:
    """First :class:`InstrumentOperand` encountered inside ``block`` using
    the canonical per-condition walk order.  Returns ``None`` if the block
    has no instrument operand.

    Used by the API layer to choose the marker-price series for the
    block's target instrument; see :func:`_block_price_for_instrument`.
    """
    for op in _walk_block_operands(block):
        if isinstance(op, InstrumentOperand):
            return op
    return None


# ---------------------------------------------------------------------------
# Operand resolution
# ---------------------------------------------------------------------------


async def _resolve_indicator_operand(
    op: IndicatorOperand,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Run a referenced indicator (after merging overrides) and return its
    ``(dates, values)`` series.

    The indicator's effective ``series_map`` is fetched on-demand and
    inner-joined on the intersection of dates, matching
    ``/api/indicators/compute`` semantics.
    """
    base = indicators.get(op.indicator_id)
    if base is None:
        raise SignalDataError(
            f"indicator spec not provided for indicator_id={op.indicator_id!r}"
        )
    if not base.series_map:
        raise SignalValidationError(
            f"indicator {op.indicator_id!r}: series_map must be non-empty"
        )

    eff_params, eff_series = _merge_indicator_effective(op, base)

    fetched: list[tuple[str, npt.NDArray[np.int64], npt.NDArray[np.float64]]] = []
    for label, (collection, instrument_id) in eff_series.items():
        dates, values = await fetcher(collection, instrument_id, "close")
        if dates.size >= 2 and not bool(np.all(np.diff(dates) > 0)):
            raise SignalValidationError(
                f"indicator {op.indicator_id!r} series {label!r}: "
                f"non-monotonic or duplicate dates"
            )
        fetched.append((label, dates, values))

    # Inner-join on the intersection of dates.
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
    fetcher: PriceFetcher,
) -> tuple[npt.NDArray[np.int64] | None, npt.NDArray[np.float64] | None, float | None]:
    """Resolve one operand to a (dates, values) pair or a scalar.

    Constants return ``(None, None, value)``; series return
    ``(dates, values, None)``.
    """
    if isinstance(operand, ConstantOperand):
        return None, None, float(operand.value)
    if isinstance(operand, InstrumentOperand):
        dates, values = await fetcher(
            operand.collection, operand.instrument_id, operand.field
        )
        if dates.size >= 2 and not bool(np.all(np.diff(dates) > 0)):
            raise SignalValidationError(
                f"instrument {operand.collection}/{operand.instrument_id}: "
                f"non-monotonic or duplicate dates"
            )
        return dates.astype(np.int64, copy=False), values.astype(
            np.float64, copy=False
        ), None
    if isinstance(operand, IndicatorOperand):
        dates, values = await _resolve_indicator_operand(
            operand, indicators, fetcher
        )
        return dates, values, None
    raise SignalValidationError(f"unknown operand kind: {operand!r}")


# ---------------------------------------------------------------------------
# Union alignment
# ---------------------------------------------------------------------------


def _union_align(
    resolved: dict[tuple, tuple[npt.NDArray[np.int64] | None, npt.NDArray[np.float64] | None, float | None]],
) -> tuple[npt.NDArray[np.int64], dict[tuple, npt.NDArray[np.float64]]]:
    """Sort-union the timestamps of every series operand and reindex values.

    Constants are broadcast to a constant array. Missing timestamps fill
    with NaN.
    """
    all_dates: list[npt.NDArray[np.int64]] = []
    for dates, _vals, _scalar in resolved.values():
        if dates is not None:
            all_dates.append(dates)

    if not all_dates:
        index = np.array([], dtype=np.int64)
    else:
        index = np.unique(np.concatenate(all_dates))  # sorted

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
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """Evaluate one condition.

    Returns ``(truth, nan_at_t)``:
      * ``truth`` -- bool[T]; False wherever any operand is NaN.
      * ``nan_at_t`` -- bool[T]; True where any referenced operand was NaN
        (used by the per-instrument NaN→0 rule after composition).
    """
    # All operand keys in this file fold the override into the indicator
    # identity so ``values_by_key`` lookups match the resolution cache.
    k = lambda o: _operand_key(o, indicators)

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
                else:  # cross_below
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


def _is_usable_block(block: Block, *, is_entry: bool) -> bool:
    """A block participates in composition only if it has at least one
    condition AND a resolved instrument. For entry tabs it also needs a
    strictly-positive weight; exit tabs accept any weight (including 0).

    Returning False silently skips the block. The API layer's Run-gate
    uses the same predicate (via the frontend's blockShape helpers) to
    prevent partially-configured blocks from ever reaching compute.
    """
    if not block.conditions:
        return False
    if block.instrument is None:
        return False
    if is_entry and not (block.weight > 0.0):
        return False
    return True


def _eval_block_activity(
    block: Block,
    indicators: dict[str, IndicatorSpecInput],
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """AND-reduce a block's conditions.

    Returns ``(active_mask[T], any_nan_at_t[T])``. ``active_mask`` is
    ``True`` at timesteps where every condition fires; ``any_nan_at_t``
    is ``True`` wherever any operand referenced in this block was NaN.
    """
    active = np.ones(T, dtype=np.bool_)
    any_nan = np.zeros(T, dtype=np.bool_)
    for cond in block.conditions:
        c_truth, c_nan = _eval_condition(cond, indicators, values_by_key, T)
        active &= c_truth
        any_nan |= c_nan
    return active, any_nan


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentPositionResult:
    """Per-instrument output returned by :func:`evaluate_signal`."""

    instrument: InstrumentRef
    values: npt.NDArray[np.float64]       # position[t] ∈ [-1, 1], NaN-masked to 0
    clipped_mask: npt.NDArray[np.bool_]   # True where pre-clip score > 1 and no exit
    price_label: str | None = None
    price_values: npt.NDArray[np.float64] | None = None


@dataclass(frozen=True)
class SignalEvalResult:
    """Top-level evaluation result (v2)."""

    index: npt.NDArray[np.int64]          # YYYYMMDD, union across all operands
    positions: tuple[InstrumentPositionResult, ...]
    clipped: bool                         # OR across instruments and timesteps
    diagnostics: dict[str, object]


def _block_price_for_instrument(
    block: Block,
) -> tuple[str, str, str] | None:
    """Pick the block's marker-price (collection, instrument_id, field).

    Walk order: the first :class:`InstrumentOperand` inside the block's
    conditions (see :func:`_first_instrument_operand_in_block`) wins.  If
    no condition references an instrument, fall back to the block's
    top-level ``instrument`` with ``field='close'``.  Returns ``None``
    when the block has no top-level instrument and no instrument operand.
    """
    op = _first_instrument_operand_in_block(block)
    if op is not None:
        return (op.collection, op.instrument_id, op.field)
    if block.instrument is not None:
        return (block.instrument.collection, block.instrument.instrument_id, "close")
    return None


async def evaluate_signal(
    signal: Signal,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
) -> SignalEvalResult:
    """Evaluate a ``Signal`` and return per-instrument positions + clip flag.

    Parameters
    ----------
    signal:
        The parsed signal spec (v2 — blocks carry instrument + weight).
    indicators:
        Map ``indicator_id → IndicatorSpecInput`` for every
        :class:`IndicatorOperand` referenced in ``signal``.
    fetcher:
        Async callable ``(collection, instrument_id, field) →
        (dates, values)`` — injected to keep this module free of
        ``tcg.core`` / FastAPI imports.
    """

    # ── 1. Identify unique instruments (via USABLE blocks only) ──
    # Stable order: long_entry → long_exit → short_entry → short_exit, then
    # block index. The first occurrence wins for response ordering. Blocks
    # whose instrument is None or whose entry-weight is 0.0 or that have
    # no conditions are skipped -- they contribute nothing and MUST NOT
    # spawn a phantom instrument entry in the response.
    unique_instruments: list[InstrumentRef] = []
    seen: set[tuple[str, str]] = set()
    for rules_tuple, is_entry_tab in (
        (signal.rules.long_entry, True),
        (signal.rules.long_exit, False),
        (signal.rules.short_entry, True),
        (signal.rules.short_exit, False),
    ):
        for blk in rules_tuple:
            if not _is_usable_block(blk, is_entry=is_entry_tab):
                continue
            # Safe: _is_usable_block guarantees instrument is not None.
            assert blk.instrument is not None
            key = (blk.instrument.collection, blk.instrument.instrument_id)
            if key in seen:
                continue
            seen.add(key)
            unique_instruments.append(blk.instrument)

    # ── 2. Resolve every operand (with override-aware cache keys) ──
    #
    # We also need price fallbacks per instrument: for each unique
    # instrument referenced by a block that lacks a condition-level
    # instrument operand, we still want to emit a close-price series for
    # the chart. We add an implicit ``InstrumentOperand(close)`` to the
    # resolution set so its dates participate in the union index.
    operands: list[Operand] = list(_walk_operands(signal))
    for ref in unique_instruments:
        operands.append(
            InstrumentOperand(
                collection=ref.collection,
                instrument_id=ref.instrument_id,
                field="close",
            )
        )

    unique_keys: dict[tuple, Operand] = {}
    for op in operands:
        unique_keys.setdefault(_operand_key(op, indicators), op)

    resolved: dict[
        tuple,
        tuple[npt.NDArray[np.int64] | None, npt.NDArray[np.float64] | None, float | None],
    ] = {}
    for key, op in unique_keys.items():
        resolved[key] = await _resolve_operand(op, indicators, fetcher)

    index, values_by_key = _union_align(resolved)
    T = index.size

    # Degenerate: no series referenced at all.
    if T == 0:
        return SignalEvalResult(
            index=np.array([], dtype=np.int64),
            positions=tuple(
                InstrumentPositionResult(
                    instrument=ref,
                    values=np.array([], dtype=np.float64),
                    clipped_mask=np.array([], dtype=np.bool_),
                    price_label=None,
                    price_values=None,
                )
                for ref in unique_instruments
            ),
            clipped=False,
            diagnostics={"T": 0, "instruments": len(unique_instruments)},
        )

    # ── 3. Per-instrument composition ──
    results: list[InstrumentPositionResult] = []
    any_clipped_overall = False

    for ref in unique_instruments:
        long_score = np.zeros(T, dtype=np.float64)
        short_score = np.zeros(T, dtype=np.float64)
        long_exit_fired = np.zeros(T, dtype=np.bool_)
        short_exit_fired = np.zeros(T, dtype=np.bool_)
        nan_poison = np.zeros(T, dtype=np.bool_)

        # Walk each direction; blocks on OTHER instruments do not contribute
        # to this ref's scores/exits, but their NaN-poison is scoped away too.
        def _add_blocks(
            blocks: tuple[Block, ...],
            *,
            is_entry: bool,
            bucket: npt.NDArray,  # score array (float) or exit array (bool)
            kind: str,            # "score" or "exit"
        ) -> None:
            nonlocal nan_poison
            for blk in blocks:
                if blk.instrument is None:
                    continue
                if (blk.instrument.collection, blk.instrument.instrument_id) != (
                    ref.collection,
                    ref.instrument_id,
                ):
                    continue
                if not _is_usable_block(blk, is_entry=is_entry):
                    continue
                active, blk_nan = _eval_block_activity(
                    blk, indicators, values_by_key, T
                )
                nan_poison = nan_poison | blk_nan
                if kind == "score":
                    bucket += active.astype(np.float64) * float(blk.weight)
                else:  # "exit"
                    bucket |= active

        _add_blocks(
            signal.rules.long_entry, is_entry=True, bucket=long_score, kind="score"
        )
        _add_blocks(
            signal.rules.short_entry, is_entry=True, bucket=short_score, kind="score"
        )
        _add_blocks(
            signal.rules.long_exit, is_entry=False, bucket=long_exit_fired, kind="exit"
        )
        _add_blocks(
            signal.rules.short_exit, is_entry=False, bucket=short_exit_fired, kind="exit"
        )

        # Clipping: score > 1 AND no exit.
        clipped_mask = (
            ((long_score > 1.0) & ~long_exit_fired)
            | ((short_score > 1.0) & ~short_exit_fired)
        )

        long_pos = np.where(long_exit_fired, 0.0, np.minimum(long_score, 1.0))
        short_pos = np.where(short_exit_fired, 0.0, np.minimum(short_score, 1.0))
        position = long_pos - short_pos
        # NaN poison → force position to 0 (chart gap at those t's).
        position = np.where(nan_poison, 0.0, position)

        if bool(clipped_mask.any()):
            any_clipped_overall = True

        # Price series for chart overlay. Walk blocks belonging to this
        # instrument in stable order (long_entry → long_exit → short_entry
        # → short_exit, block index) and take the first block's marker
        # price. If none of the blocks carry an instrument-operand at all,
        # fall back to the instrument's close series (which we resolved
        # implicitly above).
        price_label: str | None = None
        price_values: npt.NDArray[np.float64] | None = None
        for rules_tuple in (
            signal.rules.long_entry,
            signal.rules.long_exit,
            signal.rules.short_entry,
            signal.rules.short_exit,
        ):
            if price_label is not None:
                break
            for blk in rules_tuple:
                if blk.instrument is None:
                    continue
                if (blk.instrument.collection, blk.instrument.instrument_id) != (
                    ref.collection,
                    ref.instrument_id,
                ):
                    continue
                if not _is_usable_block(
                    blk, is_entry=rules_tuple is signal.rules.long_entry
                    or rules_tuple is signal.rules.short_entry,
                ):
                    continue
                triple = _block_price_for_instrument(blk)
                if triple is None:
                    continue
                coll, iid, fld = triple
                key = ("instrument", coll, iid, fld)
                if key in values_by_key:
                    price_label = f"{iid}.{fld}"
                    price_values = values_by_key[key]
                    break
        if price_label is None:
            # Last-ditch fallback to the instrument's close (implicit op).
            key = ("instrument", ref.collection, ref.instrument_id, "close")
            if key in values_by_key:
                price_label = f"{ref.instrument_id}.close"
                price_values = values_by_key[key]

        results.append(
            InstrumentPositionResult(
                instrument=ref,
                values=position,
                clipped_mask=clipped_mask,
                price_label=price_label,
                price_values=price_values,
            )
        )

    diagnostics = {
        "T": int(T),
        "instruments": len(unique_instruments),
    }

    return SignalEvalResult(
        index=index,
        positions=tuple(results),
        clipped=any_clipped_overall,
        diagnostics=diagnostics,
    )


__all__ = [
    "IndicatorSpecInput",
    "InstrumentPositionResult",
    "PriceFetcher",
    "SignalDataError",
    "SignalEvalResult",
    "SignalRuntimeError",
    "SignalValidationError",
    "evaluate_signal",
]
