"""Signal specification types -- v3 (iter-4, named inputs).

A ``Signal`` is an OR of AND-blocks of boolean conditions across four
directions: ``long_entry``, ``long_exit``, ``short_entry``, ``short_exit``.
Each condition compares one or two operands, each of which resolves to a
numeric time series.

v3 architectural rewrite (iter-4)
---------------------------------
* Signals now carry a top-level ``inputs: tuple[Input, ...]`` list. Each
  Input has a single-letter ``id`` (X, Y, Z, ...) assigned on creation,
  user-renameable, and an ``instrument`` which is either
  :class:`InstrumentSpot` (collection + instrument_id) or
  :class:`InstrumentContinuous` (a rolling futures spec).
* Blocks carry ``input_id`` instead of ``instrument`` / ``InstrumentRef``.
  A block contributes to the position series of its bound input's
  instrument.
* ``InstrumentOperand`` no longer carries ``collection`` /
  ``instrument_id``. It now carries only ``input_id`` (and ``field`` for
  OHLCV selection).
* ``IndicatorOperand`` carries ``input_id``. The bound input's instrument
  replaces the indicator's base ``seriesMap`` primary label. Optional
  ``series_override`` now maps ``label -> input_id`` (label → instrument
  provided by another input).

There is NO v2 reader and NO migration — v2 state is discarded on load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Input instrument (discriminated union)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentSpot:
    """Spot instrument — a single (collection, instrument_id)."""

    collection: str
    instrument_id: str
    kind: Literal["spot"] = "spot"


@dataclass(frozen=True)
class InstrumentContinuous:
    """Continuous-futures instrument — rolled from a FUT_* collection."""

    collection: str
    adjustment: Literal["none", "proportional", "difference"] = "none"
    cycle: str | None = None  # e.g. "HMUZ" quarterly
    roll_offset: int = 0
    strategy: Literal["front_month"] = "front_month"
    kind: Literal["continuous"] = "continuous"


InputInstrument = InstrumentSpot | InstrumentContinuous


@dataclass(frozen=True)
class Input:
    """A named price-series input declared at the top of a signal.

    ``id`` is typically a single letter (X, Y, Z, ...) assigned
    automatically on creation and user-renameable. ``instrument`` is the
    fully-configured source; an unconfigured input (missing collection
    or instrument_id, missing cycle/adjustment for continuous) is not
    runnable.
    """

    id: str
    instrument: InputInstrument


# ---------------------------------------------------------------------------
# Operand
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorOperand:
    """Operand backed by a user-defined indicator.

    v3: ``input_id`` identifies the bound input; the input's instrument
    replaces the indicator's primary series-map instrument at execution
    time. ``series_override`` maps ``label -> input_id`` to rebind
    non-primary labels to other declared inputs. ``params_override``
    still maps ``param_name -> value`` (unchanged).
    """

    indicator_id: str
    input_id: str
    output: str = "default"
    params_override: dict[str, Any] | None = None
    series_override: dict[str, str] | None = None  # v3: label -> input_id
    kind: Literal["indicator"] = "indicator"


@dataclass(frozen=True)
class InstrumentOperand:
    """Operand backed by an input's price field (default: ``close``).

    v3: ``input_id`` replaces the v2 ``collection`` + ``instrument_id``
    pair. The operand resolves through the bound input's instrument.
    """

    input_id: str
    field: str = "close"
    kind: Literal["instrument"] = "instrument"


@dataclass(frozen=True)
class ConstantOperand:
    """Operand backed by a constant scalar broadcast across all timesteps."""

    value: float
    kind: Literal["constant"] = "constant"


Operand = IndicatorOperand | InstrumentOperand | ConstantOperand


# ---------------------------------------------------------------------------
# Condition variants (unchanged vs v2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompareCondition:
    """Binary comparison: ``lhs <op> rhs`` for ``gt|lt|ge|le|eq``."""

    op: Literal["gt", "lt", "ge", "le", "eq"]
    lhs: Operand
    rhs: Operand


@dataclass(frozen=True)
class CrossCondition:
    """Directional crossover between two series."""

    op: Literal["cross_above", "cross_below"]
    lhs: Operand
    rhs: Operand


@dataclass(frozen=True)
class InRangeCondition:
    """``min <= operand <= max`` (all three resolved as series)."""

    op: Literal["in_range"]
    operand: Operand
    min: Operand
    max: Operand


@dataclass(frozen=True)
class RollingCondition:
    """``operand[t] <op> operand[t - lookback]`` for ``rolling_gt|rolling_lt``."""

    op: Literal["rolling_gt", "rolling_lt"]
    operand: Operand
    lookback: int


Condition = CompareCondition | CrossCondition | InRangeCondition | RollingCondition


# ---------------------------------------------------------------------------
# Block / Rules / Signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """A single AND-block of conditions.

    v3 fields:
      * ``input_id`` -- id of the declared :class:`Input` whose
        instrument this block's position contributes to. ``""`` is the
        sentinel "not yet picked" (evaluator skips the block).
      * ``weight`` -- unsigned contribution in [0, 1+]. Ignored on exit
        tabs. ``0.0`` on entry tabs = sentinel "not yet picked".
    """

    conditions: tuple[Condition, ...] = ()
    input_id: str = ""
    weight: float = 0.0


@dataclass(frozen=True)
class SignalRules:
    """Per-direction lists of OR-ed AND-blocks."""

    long_entry: tuple[Block, ...] = ()
    long_exit: tuple[Block, ...] = ()
    short_entry: tuple[Block, ...] = ()
    short_exit: tuple[Block, ...] = ()


@dataclass(frozen=True)
class Signal:
    """A signal spec — declared inputs + direction rules.

    v3: ``inputs`` is a tuple of :class:`Input`. Blocks and operands
    bind through these inputs by id.
    """

    id: str
    name: str
    inputs: tuple[Input, ...] = ()
    rules: SignalRules = field(default_factory=SignalRules)


__all__ = [
    "Block",
    "CompareCondition",
    "Condition",
    "ConstantOperand",
    "CrossCondition",
    "IndicatorOperand",
    "Input",
    "InputInstrument",
    "InRangeCondition",
    "InstrumentContinuous",
    "InstrumentOperand",
    "InstrumentSpot",
    "Operand",
    "RollingCondition",
    "Signal",
    "SignalRules",
]
