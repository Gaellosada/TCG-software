"""Signal evaluator -- pure NumPy, vectorised.

Consumes a :class:`tcg.types.signal.Signal` plus a map of referenced
indicator specs, resolves every operand to an aligned float series, and
returns per-timestep ``long_score``/``short_score``/``position`` vectors
and ``entries/exits`` index lists.

Design decisions
----------------
* Operands are resolved ONCE — each distinct ``(kind, ...)`` tuple is
  fetched/evaluated exactly once, and its values are reused across every
  condition that references it. This matters because an indicator spec
  is expensive (executes user code).
* After resolution every operand owns its own ``(timestamps, values)``
  pair. We compute the union of all timestamps, sort ascending, and
  reindex every operand onto it (missing → NaN). From that point on all
  work is vectorised over length ``T``.
* A condition evaluator returns a ``bool`` vector of length ``T`` AND a
  ``nan_mask`` vector marking timesteps where any referenced operand of
  the condition was NaN (forced to ``False`` in the bool output, but
  tracked separately so the top-level NaN→0 rule can be applied after
  composition).
* Block = AND of conditions. Direction score = (# firing blocks)/(# blocks).
* ``long_pos = long_entry_score if long_exit_score == 0 else 0``; same for
  short. ``position = long_pos - short_pos``; if ANY operand across the
  whole spec is NaN at t, ``position[t] = 0``.
* Entries/exits are computed by diffing the per-timestep ``*_pos`` mask.
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
# Operand resolution
# ---------------------------------------------------------------------------


def _operand_key(operand: Operand) -> tuple:
    """Cache key identifying an operand's resolved series.

    Constants share a key per value so we don't allocate duplicates.
    """
    if isinstance(operand, IndicatorOperand):
        return ("indicator", operand.indicator_id, operand.output)
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


def _walk_operands(signal: Signal) -> list[Operand]:
    """Walk every operand in the signal in the canonical stable order.

    Order (must stay consistent with :func:`_first_instrument_operand`):
    * directions: ``long_entry → long_exit → short_entry → short_exit``
    * blocks in array order;
    * conditions in array order;
    * for binary conditions (``gt``/``lt``/``ge``/``le``/``eq``/
      ``cross_above``/``cross_below``): ``lhs`` before ``rhs``;
    * for ``in_range``: ``operand`` then ``min`` then ``max``;
    * for rolling (``rolling_gt``/``rolling_lt``): ``operand`` only.
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


def _first_instrument_operand(signal: Signal) -> InstrumentOperand | None:
    """Return the first :class:`InstrumentOperand` encountered during a
    stable walk of ``signal``'s rules, or ``None`` if none exists.

    The walk order is identical to :func:`_walk_operands` — directions in
    the order ``long_entry → long_exit → short_entry → short_exit``, blocks
    and conditions in array order, and within a condition: ``lhs`` before
    ``rhs``; ``operand`` before ``min`` before ``max``; rolling's single
    ``operand``.

    Used by the API layer to emit the ``price`` field of the compute
    response (the "chart marker price" series).
    """
    for op in _walk_operands(signal):
        if isinstance(op, InstrumentOperand):
            return op
    return None


async def _resolve_indicator_operand(
    op: IndicatorOperand,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Run a referenced indicator and return its (dates, values) series.

    The indicator's series_map is fetched on-demand, then inner-joined on
    the intersection of dates, matching the semantics used by
    ``/api/indicators/compute``.
    """
    spec = indicators.get(op.indicator_id)
    if spec is None:
        raise SignalDataError(
            f"indicator spec not provided for indicator_id={op.indicator_id!r}"
        )
    if not spec.series_map:
        raise SignalValidationError(
            f"indicator {op.indicator_id!r}: series_map must be non-empty"
        )

    fetched: list[tuple[str, npt.NDArray[np.int64], npt.NDArray[np.float64]]] = []
    for label, (collection, instrument_id) in spec.series_map.items():
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
        result = run_indicator(spec.code, dict(spec.params), aligned)
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

    Returns ``(index, values_by_key)`` where ``values_by_key[key]`` is a
    float64 array of length ``T``. Constants are broadcast to a constant
    array. Missing timestamps are filled with NaN.
    """
    all_dates: list[npt.NDArray[np.int64]] = []
    for dates, _vals, _scalar in resolved.values():
        if dates is not None:
            all_dates.append(dates)

    if not all_dates:
        # Pure-constant signal — degenerate. Return an empty index; caller
        # decides whether this is a validation error.
        index = np.array([], dtype=np.int64)
    else:
        index = np.unique(np.concatenate(all_dates))  # np.unique returns sorted

    values_by_key: dict[tuple, npt.NDArray[np.float64]] = {}
    for key, (dates, vals, scalar) in resolved.items():
        if scalar is not None:
            values_by_key[key] = np.full(index.size, scalar, dtype=np.float64)
            continue
        assert dates is not None and vals is not None
        # Map each timestamp in `index` to a position in `dates` if present.
        # `dates` is strictly increasing (validated upstream), so searchsorted
        # gives a correct insertion point; equality is checked via a direct
        # comparison at that position.
        pos = np.searchsorted(dates, index)
        # Clip to stay in-bounds for the comparison; any out-of-range becomes
        # a miss via the equality check below.
        safe_pos = np.clip(pos, 0, dates.size - 1)
        match = dates[safe_pos] == index
        out = np.full(index.size, np.nan, dtype=np.float64)
        # Only copy where the timestamps matched.
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
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
) -> tuple[npt.NDArray[np.bool_], npt.NDArray[np.bool_]]:
    """Evaluate one condition.

    Returns ``(truth, nan_at_t)``:

    * ``truth`` — bool[T], the condition's firing vector (False where any
      referenced operand is NaN).
    * ``nan_at_t`` — bool[T], True where any referenced operand was NaN
      (used by the top-level NaN→0 rule).
    """
    if isinstance(cond, CompareCondition):
        a = values_by_key[_operand_key(cond.lhs)]
        b = values_by_key[_operand_key(cond.rhs)]
        nan_at_t = np.isnan(a) | np.isnan(b)
        # np.greater etc. on NaN returns False, which is the desired semantic.
        with np.errstate(invalid="ignore"):
            truth = _COMPARE_OPS[cond.op](a, b)
        truth = truth & ~nan_at_t
        return truth.astype(np.bool_, copy=False), nan_at_t

    if isinstance(cond, CrossCondition):
        a = values_by_key[_operand_key(cond.lhs)]
        b = values_by_key[_operand_key(cond.rhs)]
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
        x = values_by_key[_operand_key(cond.operand)]
        lo = values_by_key[_operand_key(cond.min)]
        hi = values_by_key[_operand_key(cond.max)]
        nan_at_t = np.isnan(x) | np.isnan(lo) | np.isnan(hi)
        with np.errstate(invalid="ignore"):
            truth = (x >= lo) & (x <= hi)
        truth = truth & ~nan_at_t
        return truth.astype(np.bool_, copy=False), nan_at_t

    if isinstance(cond, RollingCondition):
        x = values_by_key[_operand_key(cond.operand)]
        k = int(cond.lookback)
        if k < 1:
            raise SignalValidationError(
                f"rolling lookback must be >= 1, got {k}"
            )
        truth = np.zeros(T, dtype=np.bool_)
        nan_at_t = np.isnan(x).copy()
        if T > k:
            cur = x[k:]
            prev = x[:-k]
            prev_or_cur_nan = np.isnan(cur) | np.isnan(prev)
            with np.errstate(invalid="ignore"):
                if cond.op == "rolling_gt":
                    fired = cur > prev
                else:  # rolling_lt
                    fired = cur < prev
            fired = fired & ~prev_or_cur_nan
            truth[k:] = fired
            # A timestep at index >= k depends on x[t-k] too; if that lookback
            # sample is NaN, mark the current step as "nan-influenced" so the
            # top-level NaN→0 rule zero-outs position here as well.
            lookback_nan = np.zeros(T, dtype=np.bool_)
            lookback_nan[k:] = np.isnan(prev)
            nan_at_t = nan_at_t | lookback_nan
            # Timesteps t < k never fire and, per the authoritative spec,
            # should not by themselves force NaN→0 (a purely-rolling signal
            # would otherwise produce a zero position for the entire warmup
            # window even on perfectly clean data). We therefore do NOT mark
            # t < k as nan_at_t.
        return truth, nan_at_t

    raise SignalValidationError(f"unknown condition type: {type(cond).__name__}")


def _eval_blocks(
    blocks: tuple[Block, ...],
    values_by_key: dict[tuple, npt.NDArray[np.float64]],
    T: int,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.bool_]]:
    """Return (score[T] in [0,1], any_nan_at_t[T]) for a single direction."""
    any_nan = np.zeros(T, dtype=np.bool_)
    if not blocks:
        return np.zeros(T, dtype=np.float64), any_nan

    n_blocks = len(blocks)
    fire_count = np.zeros(T, dtype=np.int64)
    for block in blocks:
        if not block.conditions:
            # Zero-condition block is always False per the spec.
            continue
        block_truth = np.ones(T, dtype=np.bool_)
        for cond in block.conditions:
            c_truth, c_nan = _eval_condition(cond, values_by_key, T)
            block_truth &= c_truth
            any_nan |= c_nan
        fire_count += block_truth.astype(np.int64, copy=False)

    score = fire_count.astype(np.float64) / float(n_blocks)
    return score, any_nan


# ---------------------------------------------------------------------------
# Entry / exit index extraction
# ---------------------------------------------------------------------------


def _entries_exits(pos: npt.NDArray[np.float64]) -> tuple[list[int], list[int]]:
    """From a direction's per-timestep position vector, derive entry/exit idx.

    ``entries[t]`` fires when ``pos[t] > 0`` AND (``t == 0`` OR ``pos[t-1] == 0``).
    ``exits[t]`` fires when ``pos[t] == 0`` AND ``t > 0`` AND ``pos[t-1] > 0``.
    """
    T = pos.size
    if T == 0:
        return [], []
    active = pos > 0.0
    prev = np.zeros(T, dtype=np.bool_)
    prev[1:] = active[:-1]
    entries = np.nonzero(active & ~prev)[0].tolist()
    exits = np.nonzero(~active & prev)[0].tolist()
    return [int(i) for i in entries], [int(i) for i in exits]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalEvalResult:
    index: npt.NDArray[np.int64]  # YYYYMMDD
    position: npt.NDArray[np.float64]
    long_score: npt.NDArray[np.float64]
    short_score: npt.NDArray[np.float64]
    entries_long: list[int]
    exits_long: list[int]
    entries_short: list[int]
    exits_short: list[int]
    # Marker-price series: the first instrument operand encountered during
    # a stable rule walk (see :func:`_first_instrument_operand`). ``None``
    # when the signal contains no instrument operand.
    price_label: str | None = None
    price_values: npt.NDArray[np.float64] | None = None


async def evaluate_signal(
    signal: Signal,
    indicators: dict[str, IndicatorSpecInput],
    fetcher: PriceFetcher,
) -> SignalEvalResult:
    """Evaluate a ``Signal`` and return per-timestep scores and positions.

    Parameters
    ----------
    signal:
        The parsed signal spec.
    indicators:
        Map ``indicator_id → IndicatorSpecInput`` for every
        :class:`IndicatorOperand` referenced in ``signal``.
    fetcher:
        Async callable ``(collection, instrument_id, field) ->
        (dates, values)`` — injected to keep this module free of
        ``tcg.core`` / FastAPI imports.
    """
    operands = _walk_operands(signal)

    # Deduplicate before resolving — each unique operand is fetched/computed
    # exactly once.
    unique_keys: dict[tuple, Operand] = {}
    for op in operands:
        unique_keys.setdefault(_operand_key(op), op)

    resolved: dict[tuple, tuple[npt.NDArray[np.int64] | None, npt.NDArray[np.float64] | None, float | None]] = {}
    for key, op in unique_keys.items():
        resolved[key] = await _resolve_operand(op, indicators, fetcher)

    index, values_by_key = _union_align(resolved)
    T = index.size

    # Identify the marker-price operand (first instrument operand in stable
    # rule-walk order). Resolved once above, so we just look it up by key.
    price_op = _first_instrument_operand(signal)
    if price_op is None:
        price_label: str | None = None
        price_values: npt.NDArray[np.float64] | None = None
    else:
        price_label = f"{price_op.instrument_id}.{price_op.field}"
        price_values = values_by_key[_operand_key(price_op)]

    # Degenerate: no series referenced at all (constants only).
    if T == 0:
        empty_int = np.array([], dtype=np.int64)
        empty_f = np.array([], dtype=np.float64)
        return SignalEvalResult(
            index=empty_int,
            position=empty_f,
            long_score=empty_f,
            short_score=empty_f,
            entries_long=[],
            exits_long=[],
            entries_short=[],
            exits_short=[],
            price_label=price_label,
            price_values=(
                np.array([], dtype=np.float64) if price_label is not None else None
            ),
        )

    long_entry_score, nan_le = _eval_blocks(
        signal.rules.long_entry, values_by_key, T
    )
    long_exit_score, nan_lx = _eval_blocks(
        signal.rules.long_exit, values_by_key, T
    )
    short_entry_score, nan_se = _eval_blocks(
        signal.rules.short_entry, values_by_key, T
    )
    short_exit_score, nan_sx = _eval_blocks(
        signal.rules.short_exit, values_by_key, T
    )

    # Kill-on-exit composition.
    long_pos = np.where(long_exit_score == 0.0, long_entry_score, 0.0)
    short_pos = np.where(short_exit_score == 0.0, short_entry_score, 0.0)
    position = long_pos - short_pos

    # Top-level NaN→0 rule: if ANY referenced operand across the whole spec
    # was NaN at t, force position[t] = 0.
    any_nan_any_cond = nan_le | nan_lx | nan_se | nan_sx
    # Also: if a timestep has NO condition referencing a nan, but some operand
    # in the spec is still NaN there (e.g. a series referenced only by a
    # direction with zero blocks), treat it as NaN too. The direction-score
    # nan_masks already cover the conditions; for thoroughness, OR in the
    # raw NaN mask from every resolved series at t. This is cheap and makes
    # the "any operand across the spec" wording authoritative.
    raw_nan = np.zeros(T, dtype=np.bool_)
    for vals in values_by_key.values():
        raw_nan |= np.isnan(vals)
    any_nan_any_cond = any_nan_any_cond | raw_nan
    position = np.where(any_nan_any_cond, 0.0, position)

    # Apply the same NaN mask to long_pos / short_pos so entry/exit detection
    # sees a zero position at NaN timesteps (otherwise a short with NaN could
    # still register as a long entry via long_pos > 0 while position == 0).
    long_pos = np.where(any_nan_any_cond, 0.0, long_pos)
    short_pos = np.where(any_nan_any_cond, 0.0, short_pos)

    entries_long, exits_long = _entries_exits(long_pos)
    entries_short, exits_short = _entries_exits(short_pos)

    return SignalEvalResult(
        index=index,
        position=position,
        long_score=long_entry_score,
        short_score=short_entry_score,
        entries_long=entries_long,
        exits_long=exits_long,
        entries_short=entries_short,
        exits_short=exits_short,
        price_label=price_label,
        price_values=price_values,
    )


__all__ = [
    "IndicatorSpecInput",
    "PriceFetcher",
    "SignalDataError",
    "SignalEvalResult",
    "SignalRuntimeError",
    "SignalValidationError",
    "evaluate_signal",
]
