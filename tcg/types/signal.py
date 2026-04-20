"""Signal specification types -- entry/exit rule composition for trading.

A ``Signal`` is an OR of AND-blocks of boolean conditions across four
directions: ``long_entry``, ``long_exit``, ``short_entry``, ``short_exit``.
Each condition compares one or two operands, each of which resolves to a
numeric time series (an Indicator, an instrument price field, or a
constant).

All types are frozen dataclasses -- the contract is authoritative and
must not be mutated after construction. Condition variants are
discriminated by ``op`` at parse time; this module provides the concrete
dataclass types and a :func:`parse_condition` helper.

v2 additions (iter-3)
---------------------
* :class:`Block` gains ``instrument: InstrumentRef | None`` and
  ``weight: float``. ``instrument`` points to the instrument whose
  position series this block contributes to; ``weight`` is the unsigned
  contribution added to that instrument's long/short score when the
  block fires. ``weight`` is ignored on exit tabs but kept on the
  dataclass for a uniform shape. Sentinel values ``instrument=None`` and
  ``weight=0.0`` mean "not yet picked" -- such blocks are skipped by the
  evaluator (they contribute nothing).
* :class:`IndicatorOperand` gains ``params_override`` and
  ``series_override`` optional maps; when supplied they are merged on
  top of the base indicator spec before execution. ``None`` or empty
  maps ⇒ inherit defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Instrument reference (v2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentRef:
    """Reference to an instrument (used as a per-block identifier in v2).

    Distinct from :class:`InstrumentOperand`, which is a condition operand
    pointing at a specific price ``field``.  ``InstrumentRef`` only
    identifies *which* instrument a block's position contributes to; the
    block does not pick a field.
    """

    collection: str
    instrument_id: str


# ---------------------------------------------------------------------------
# Operand
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndicatorOperand:
    """Operand backed by a user-defined indicator (spec shipped in request).

    v2: optional ``params_override`` / ``series_override`` let a single
    condition customise the indicator's params or series map without
    editing the base spec. ``None``/empty means "inherit defaults".
    """

    indicator_id: str
    output: str = "default"
    params_override: dict[str, Any] | None = None
    series_override: dict[str, str] | None = None
    kind: Literal["indicator"] = "indicator"


@dataclass(frozen=True)
class InstrumentOperand:
    """Operand backed by an instrument price field (default: ``close``)."""

    collection: str
    instrument_id: str
    field: str = "close"
    kind: Literal["instrument"] = "instrument"


@dataclass(frozen=True)
class ConstantOperand:
    """Operand backed by a constant scalar broadcast across all timesteps."""

    value: float
    kind: Literal["constant"] = "constant"


Operand = IndicatorOperand | InstrumentOperand | ConstantOperand


# ---------------------------------------------------------------------------
# Condition variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompareCondition:
    """Binary comparison: ``lhs <op> rhs`` for ``gt|lt|ge|le|eq``."""

    op: Literal["gt", "lt", "ge", "le", "eq"]
    lhs: Operand
    rhs: Operand


@dataclass(frozen=True)
class CrossCondition:
    """Directional crossover between two series.

    ``cross_above(A, B)[t] = A[t-1] <= B[t-1] AND A[t] > B[t]``; symmetric
    for ``cross_below``. Index 0 is always false.
    """

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
    """``operand[t] <op> operand[t - lookback]`` for ``rolling_gt|rolling_lt``.

    For ``t < lookback`` the condition is false.
    """

    op: Literal["rolling_gt", "rolling_lt"]
    operand: Operand
    lookback: int


Condition = CompareCondition | CrossCondition | InRangeCondition | RollingCondition


# ---------------------------------------------------------------------------
# Block / Rules / Signal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """A single AND-block of conditions. Zero conditions → always false.

    v2 fields:
      * ``instrument`` -- :class:`InstrumentRef` identifying the target
        instrument for the block's contribution, or ``None`` (sentinel
        "not yet picked" -- the evaluator skips the block).
      * ``weight`` -- unsigned contribution in [0, 1+]. Ignored on exit
        tabs. ``0.0`` is the sentinel "not yet picked" on entry tabs; the
        evaluator skips such blocks.
    """

    conditions: tuple[Condition, ...] = ()
    instrument: InstrumentRef | None = None
    weight: float = 0.0


@dataclass(frozen=True)
class SignalRules:
    """Per-direction lists of OR-ed AND-blocks.

    Blocks are stored as tuples (frozen-friendly). Zero blocks in a
    direction ⇒ that direction's score is zero everywhere.
    """

    long_entry: tuple[Block, ...] = ()
    long_exit: tuple[Block, ...] = ()
    short_entry: tuple[Block, ...] = ()
    short_exit: tuple[Block, ...] = ()


@dataclass(frozen=True)
class Signal:
    id: str
    name: str
    rules: SignalRules = field(default_factory=SignalRules)


__all__ = [
    "Block",
    "CompareCondition",
    "Condition",
    "ConstantOperand",
    "CrossCondition",
    "InRangeCondition",
    "IndicatorOperand",
    "InstrumentOperand",
    "InstrumentRef",
    "Operand",
    "RollingCondition",
    "Signal",
    "SignalRules",
]
