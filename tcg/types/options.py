"""Shared frozen dataclass DTOs for the options feature (Phase 1).

All types here are pure Python dataclasses with no external dependencies.
This is the shared vocabulary consumed by every Phase 1B module.

Design decision (Decision A): frozen dataclasses live here; Pydantic v2
request/response mirrors live in ``tcg.core.api._models_options``.

``OptionContractSeries.rows`` is typed as ``tuple[OptionDailyRow, ...]``
for frozen-dataclass immutability. Downstream code that needs a pandas
DataFrame should call ``list(series.rows)`` before converting.

Spec references: §3.1 (Module 1 DTOs), §3.3 (selection), §3.4 (maturity),
§3.5 (roll), §3.6 (chain), §3.7 (P&L), §4.4 (ComputeResult).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Literal, Mapping, Optional


# ---------------------------------------------------------------------------
# Enumeration types
# ---------------------------------------------------------------------------


class GreekKind(StrEnum):
    """The five Greek/vol quantities that Module 2 can compute or retrieve."""
    DELTA = "delta"
    GAMMA = "gamma"
    THETA = "theta"
    VEGA = "vega"
    IV = "iv"


# ---------------------------------------------------------------------------
# ComputeResult — the universal provenance envelope (§4.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputeResult:
    """Provenance-carrying wrapper for any Greek or quote-derived value.

    Rules (from §4.4):
    - source="stored"   → model=None, inputs_used=None, error_*=None
    - source="computed" → model and inputs_used non-null
    - source="missing"  → value=None, error_code non-null, missing_inputs non-null
    """
    value: float | None
    source: Literal["stored", "computed", "missing"]
    model: str | None = None
    inputs_used: Optional[Mapping[str, Any]] = None
    missing_inputs: tuple[str, ...] | None = None
    error_code: str | None = None
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Core contract/series types (§3.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionContractDoc:
    """Static metadata of one option contract."""
    collection: str                         # "OPT_SP_500"
    contract_id: str                        # internalSymbol|expirationCycle joined
    root_underlying: str                    # e.g. "IND_SP_500", "GOLD"
    underlying_ref: str | None             # FUT_*._id when option-on-future; None for OPT_VIX
    underlying_symbol: str | None          # provider ticker, display only
    expiration: date
    expiration_cycle: str                  # "M", "W3 Friday", "D", ...
    strike: float
    type: Literal["C", "P"]
    contract_size: float | None
    currency: str | None
    provider: str                          # "IVOLATILITY" | "INTERNAL" | ...
    strike_factor_verified: bool           # see spec §4.7


@dataclass(frozen=True)
class OptionDailyRow:
    """One trading day for one contract — quote + stored greeks (no computation)."""
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None                    # often 0.0 on iVolatility — see DB §3.1
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    volume: float | None
    open_interest: float | None
    mid: float | None                      # derived: (bid+ask)/2 if both present
    # Stored greeks — None when absent, never silently filled.
    iv_stored: float | None
    delta_stored: float | None
    gamma_stored: float | None
    theta_stored: float | None
    vega_stored: float | None
    underlying_price_stored: float | None  # only INTERNAL provider populates this


@dataclass(frozen=True)
class OptionContractSeries:
    """A contract with its full time-series of daily rows."""
    contract: OptionContractDoc
    rows: tuple[OptionDailyRow, ...]       # chronological; use list(rows) for pandas


@dataclass(frozen=True)
class OptionRootInfo:
    """Per-root metadata returned by /api/options/roots (§5)."""
    collection: str                        # "OPT_SP_500"
    name: str                             # display: "SP 500"
    has_greeks: bool
    providers: tuple[str, ...]            # e.g. ("IVOLATILITY",)
    expiration_first: date | None
    expiration_last: date | None
    doc_count_estimated: int
    strike_factor_verified: bool


# ---------------------------------------------------------------------------
# Greek envelope (§3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputedGreeks:
    """Set of five computed/stored greeks for one contract-row."""
    iv: ComputeResult
    delta: ComputeResult
    gamma: ComputeResult
    theta: ComputeResult
    vega: ComputeResult


# ---------------------------------------------------------------------------
# Selection criterion union (§3.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ByDelta:
    """Select the contract whose stored (or computed) delta is nearest target."""
    target_delta: float                   # signed; -0.10 for 10Δ put, +0.50 for 50Δ call
    tolerance: float = 0.05              # accept |delta - target| <= tolerance
    strict: bool = False                 # if True: raise on no-match; else closest


@dataclass(frozen=True)
class ByMoneyness:
    """Select by K/S ratio."""
    target_K_over_S: float               # e.g. 1.02 for 2% OTM call
    tolerance: float = 0.01


@dataclass(frozen=True)
class ByStrike:
    """Select by exact strike value."""
    strike: float


SelectionCriterion = ByDelta | ByMoneyness | ByStrike


@dataclass(frozen=True)
class SelectionResult:
    """Outcome of a Module 3 selection call."""
    contract: OptionContractDoc | None
    matched_value: float | None          # the actual delta / K/S / strike found
    error_code: str | None              # "no_chain_for_date" | "no_match_within_tolerance" | ...
    diagnostic: str | None


# ---------------------------------------------------------------------------
# Maturity rules (§3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NextThirdFriday:
    """Roll to the third Friday of the offset month."""
    offset_months: int = 1              # 0 = current month, 1 = next month, ...


@dataclass(frozen=True)
class EndOfMonth:
    """Roll to the last business day of the offset month."""
    offset_months: int = 0


@dataclass(frozen=True)
class PlusNDays:
    """Roll to ref_date + n calendar days."""
    n: int


@dataclass(frozen=True)
class FixedDate:
    """Use a fixed absolute date as the target expiration."""
    date: date


@dataclass(frozen=True)
class NearestToTarget:
    """Find the available expiration nearest to target_dte_days from ref_date."""
    target_dte_days: int                # find the available expiration nearest target DTE


MaturityRule = NextThirdFriday | EndOfMonth | PlusNDays | FixedDate | NearestToTarget
# Alias for compatibility with spec (uses both names interchangeably).
MaturitySpec = MaturityRule


# ---------------------------------------------------------------------------
# Roll rules (§3.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtExpiry:
    """Roll into a new contract when the held contract expires."""
    pass


@dataclass(frozen=True)
class NDaysBeforeExpiry:
    """Roll n calendar days before expiry (Phase 2 only)."""
    n: int


@dataclass(frozen=True)
class DeltaCross:
    """Roll when |delta| crosses a threshold (Phase 2 only)."""
    threshold: float


RollRule = AtExpiry | NDaysBeforeExpiry | DeltaCross


@dataclass(frozen=True)
class RollResult:
    """Outcome of a Module 5 roll evaluation."""
    new_contract: OptionContractDoc | None
    roll_date: date | None
    reason: str                          # human-readable: "expired" / "delta crossed 0.30"
    error_code: str | None


# ---------------------------------------------------------------------------
# Chain types (§3.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainRow:
    """One row in the chain table for a given date."""
    contract_id: str
    expiration: date
    type: Literal["C", "P"]
    strike: float
    K_over_S: float | None              # computed: strike / underlying_price
    bid: float | None
    ask: float | None
    mid: float | None
    open_interest: float | None
    iv: ComputeResult
    delta: ComputeResult
    gamma: ComputeResult
    theta: ComputeResult
    vega: ComputeResult


@dataclass(frozen=True)
class ChainSnapshot:
    """Full chain for a (root, date) pair (Module 6 output)."""
    root: str
    date: date
    underlying_price: float | None      # float | None per Decision B; router wraps to ComputeResult
    rows: tuple[ChainRow, ...]
    notes: tuple[str, ...]             # e.g. "T_NOTE strikeFactor unverified"


# ---------------------------------------------------------------------------
# P&L types (§3.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PnLPoint:
    """Daily mark and cumulative P&L for one contract on one date."""
    date: date
    mark: float | None                  # mid (or close fallback if specified)
    pnl_cumulative: float              # qty * sign * (mark_t - entry_price) * contract_size
    pnl_daily: float                   # delta_cum from t-1


@dataclass(frozen=True)
class PnLSeries:
    """Full P&L replay for a held contract."""
    contract: OptionContractDoc
    entry_date: date
    entry_price: float
    qty: float                          # signed: + buy, - sell
    points: tuple[PnLPoint, ...]
    exit_reason: Literal["held_to_expiry", "exit_date", "contract_data_ended"] | None
    notes: tuple[str, ...]
