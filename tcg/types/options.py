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
# Cycle-tag normalisation (the "monthly 3rd-Friday series" spans two tags)
# ---------------------------------------------------------------------------
#
# The dwh tags the SAME economic series — the standard monthly option, expiring
# the 3rd Friday of the month — under DIFFERENT ``expiration_cycle`` values
# across eras / providers: early E-mini (``OPT_SP_500``) history tags it ``"M"``;
# later history tags the very same 3rd-Friday contract ``"W3 Friday"`` and leaves
# ``"M"`` for the QUARTERLIES only.  A selection filtered on ``"M"`` alone
# therefore tracks the monthly in early years but silently falls back to sparse
# quarterlies in later years — the direct cause of the delta-selected option
# landing on a poorly-covered expiry (garbage strike) in 2022+.
#
# ``"M"`` is the UI's "Monthly" choice, so its ROBUST meaning is "the monthly
# 3rd-Friday series across all eras" = the union of both tags.  ``expand_cycle``
# maps ``"M"`` → both tags and passes most other values through unchanged, so the
# monthly filter is complete without disturbing another cycle (``"Q"``, the
# specific ``"W1/2/4 Friday"`` weeklies) or the ``None`` all-cycles case.
#
# ``"W"`` (the UI's generic "Weekly") is the SAME shape of problem across a
# DIFFERENT tagging split: crypto/VIX roots (``OPT_BTC``/``OPT_ETH``/``OPT_VIX``)
# tag every weekly under the literal ``"W"``, whereas the index roots
# (``OPT_SP_500`` …) tag their weeklies per-week as ``"W1/W2/W3/W4 Friday"`` and
# have NO literal ``"W"`` at all.  So a ``"W"`` filter returns the crypto/VIX
# weeklies but ZERO rows for S&P — the reported "weekly S&P put" build failure.
# The ROBUST meaning of "Weekly" is therefore the union of BOTH conventions
# (:data:`WEEKLY_CYCLE_TAGS`).  Because each tag is a no-op for the other root
# family — crypto has only ``"W"``; S&P has only the ``"W# Friday"`` tags — the
# union is exactly the literal ``"W"`` for crypto/VIX and exactly the four
# Friday tags for S&P, so crypto/VIX weekly results are UNCHANGED.

#: The ``expiration_cycle`` tags that together make up the standard MONTHLY
#: (3rd-Friday) series.  ``"W3 Friday"`` IS the 3rd-Friday weekly = the monthly;
#: on dates where a contract is double-tagged (both ``"M"`` and ``"W3 Friday"``
#: for the same expiration) the caller de-dupes by expiration / contract id.
MONTHLY_CYCLE_TAGS: tuple[str, ...] = ("M", "W3 Friday")

#: The ``expiration_cycle`` tags that together make up ALL weekly contracts,
#: spanning both tagging conventions: the literal ``"W"`` (crypto/VIX generic
#: weekly) and the per-week ``"W1/W2/W3/W4 Friday"`` tags (index roots such as
#: ``OPT_SP_500``).  A root uses only one convention, so the union is a no-op
#: for whichever tags it lacks — see the module note.
WEEKLY_CYCLE_TAGS: tuple[str, ...] = (
    "W",
    "W1 Friday",
    "W2 Friday",
    "W3 Friday",
    "W4 Friday",
)


def expand_cycle(cycle: str | None) -> str | tuple[str, ...] | None:
    """Expand a cycle filter to its full tag set for option-STREAM selection.

    ``"M"`` → :data:`MONTHLY_CYCLE_TAGS` (the monthly 3rd-Friday series spans two
    dwh tags across eras) and ``"W"`` → :data:`WEEKLY_CYCLE_TAGS` (the generic
    "Weekly" spans two tagging conventions — literal ``"W"`` for crypto/VIX and
    per-week ``"W# Friday"`` for index roots — see the module note).  Every other
    value (a specific cycle such as ``"Q"`` / ``"W3 Friday"``, or ``None`` for
    all-cycles) is returned unchanged, so only the "Monthly" and "Weekly"
    umbrella filters are broadened.  The result is a scalar / ``None`` (pass
    through) or a tuple (query all of them); the data layer accepts both.

    This is applied ONLY on the stream-selection path (signals / option-stream
    series), NOT on the raw chain browser — a user inspecting the ``"M"`` chain
    still sees exactly the ``"M"``-tagged contracts.
    """
    if cycle == "M":
        return MONTHLY_CYCLE_TAGS
    if cycle == "W":
        return WEEKLY_CYCLE_TAGS
    return cycle


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

    collection: str  # "OPT_SP_500"
    contract_id: str  # internalSymbol|expirationCycle joined
    root_underlying: str  # e.g. "IND_SP_500", "GOLD"
    underlying_ref: str | None  # FUT_*._id when option-on-future; None for OPT_VIX
    underlying_symbol: str | None  # provider ticker, display only
    expiration: date
    expiration_cycle: str  # "M", "W3 Friday", "D", ...
    strike: float
    type: Literal["C", "P"]
    contract_size: float | None
    currency: str | None
    provider: str  # "IVOLATILITY" | "INTERNAL" | ...
    strike_factor_verified: bool  # see spec §4.7


@dataclass(frozen=True)
class OptionDailyRow:
    """One trading day for one contract — quote + stored greeks (no computation)."""

    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None  # often 0.0 on iVolatility — see DB §3.1
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    volume: float | None
    open_interest: float | None
    mid: float | None  # derived: (bid+ask)/2 if both present
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
    rows: tuple[OptionDailyRow, ...]  # chronological; use list(rows) for pandas


@dataclass(frozen=True)
class OptionRootInfo:
    """Per-root metadata returned by /api/options/roots (§5)."""

    collection: str  # "OPT_SP_500"
    name: str  # display: "SP 500"
    has_greeks: bool  # stored_greeks_ratio > 0 OR has_computed_greeks
    providers: tuple[str, ...]  # e.g. ("IVOLATILITY",)
    expiration_first: date | None
    expiration_last: date | None
    doc_count_estimated: int
    strike_factor_verified: bool
    # Latest trade date with bar data. Used by the frontend to default the
    # chain-query date to a value the data actually covers (the ingestion
    # cutoff is typically weeks behind "today"; defaulting to today returns
    # zero rows).
    last_trade_date: date | None = None
    # Fraction of docs in this collection carrying ``eodGreeks`` (0.0-1.0).
    # Drives the left-nav badge: >=0.9 → green "Greeks"; 0.1-0.9 → split
    # "Greeks" (partial coverage); <0.1 → fall through to the computed badge
    # if `has_computed_greeks` is True. Defaults to 0.0 so legacy fixtures
    # that don't populate the field render as "no stored greeks".
    stored_greeks_ratio: float = 0.0
    # Whether the engine can compute greeks for this root (mirrors
    # `tcg.engine.options.pricing._gating._BLOCKED_ROOTS`). Drives the gray
    # "Comp. Greeks" fallback badge when stored coverage is below threshold.
    has_computed_greeks: bool = False


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

    target_delta: float  # signed; -0.10 for 10Δ put, +0.50 for 50Δ call
    tolerance: float = 0.05  # accept |delta - target| <= tolerance
    strict: bool = False  # if True: raise on no-match; else closest


@dataclass(frozen=True)
class ByMoneyness:
    """Select by K/S ratio."""

    target_K_over_S: float  # e.g. 1.02 for 2% OTM call
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
    matched_value: float | None  # the actual delta / K/S / strike found
    error_code: str | None  # "no_chain_for_date" | "no_match_within_tolerance" | ...
    diagnostic: str | None


# ---------------------------------------------------------------------------
# Maturity rules (§3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NextThirdFriday:
    """Roll to the third Friday of the offset month."""

    offset_months: int = 1  # 0 = current month, 1 = next month, ...


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

    target_dte_days: int  # find the available expiration nearest target DTE


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


# ---------------------------------------------------------------------------
# Roll offset — the ROLL-EARLY axis (how early to roll), in days OR months
# ---------------------------------------------------------------------------
#
# This is ONE of the two distinct roll/maturity axes; keep them straight:
#   * TARGET-month  — the ``MaturityRule``'s ``offset_months`` (NextThirdFriday /
#     EndOfMonth): WHICH expiration to aim at (this month, next month, ...).
#   * ROLL-EARLY    — ``RollOffset`` below: resolve the maturity rule as of
#     ``date + offset`` so every roll happens that much EARLIER.
# They are NOT the same concept (target vs roll-early), so both legitimately
# speak in months; what was redundant — and is now removed — was the separate
# ``roll_schedule`` cadence (its "end of month" duplicated the EndOfMonth
# maturity).  "Roll at end of month" is now expressed ONLY by the EndOfMonth
# maturity, which the resolver detects to hold one contract per month.
#
# ``unit`` carries days or months so a single control covers both granularities
# (replaces the old days-only ``roll_offset: int``).  A shipped int (days) reads
# back as ``RollOffset(value=int, unit="days")`` via the API read-shim.


RollOffsetUnit = Literal["days", "months"]


@dataclass(frozen=True)
class RollOffset:
    """How early to roll (the ROLL-EARLY axis), in ``days`` or ``months``.

    The resolver shifts the maturity-resolution ref date forward by this amount
    (``date + offset``) so each roll fires that much sooner.  ``value == 0`` is
    the no-op default (roll at the maturity rule's natural time).  No-op for
    ``FixedDate`` maturity (a single absolute expiration)."""

    value: int = 0
    unit: RollOffsetUnit = "days"


@dataclass(frozen=True)
class RollResult:
    """Outcome of a Module 5 roll evaluation."""

    new_contract: OptionContractDoc | None
    roll_date: date | None
    reason: str  # human-readable: "expired" / "delta crossed 0.30"
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
    K_over_S: float | None  # computed: strike / underlying_price
    bid: float | None
    ask: float | None
    mid: float | None
    open_interest: float | None
    iv: ComputeResult
    delta: ComputeResult
    gamma: ComputeResult
    theta: ComputeResult
    vega: ComputeResult
    # Carried through from OptionContractDoc so the smile UI can
    # disambiguate same-date / multi-cycle roots (e.g. OPT_SP_500
    # SPX-monthly vs SPXW-weekly settling on the same Friday).
    expiration_cycle: str = ""


@dataclass(frozen=True)
class ChainSnapshot:
    """Full chain for a (root, date) pair (Module 6 output)."""

    root: str
    date: date
    underlying_price: (
        float | None
    )  # float | None per Decision B; router wraps to ComputeResult
    rows: tuple[ChainRow, ...]
    notes: tuple[str, ...]  # e.g. "T_NOTE strikeFactor unverified"


# ---------------------------------------------------------------------------
# P&L types (§3.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PnLPoint:
    """Daily mark and cumulative P&L for one contract on one date."""

    date: date
    mark: float | None  # mid (or close fallback if specified)
    pnl_cumulative: float  # qty * sign * (mark_t - entry_price) * contract_size
    pnl_daily: float  # delta_cum from t-1


@dataclass(frozen=True)
class PnLSeries:
    """Full P&L replay for a held contract."""

    contract: OptionContractDoc
    entry_date: date
    entry_price: float
    qty: float  # signed: + buy, - sell
    points: tuple[PnLPoint, ...]
    exit_reason: Literal["held_to_expiry", "exit_date", "contract_data_ended"] | None
    notes: tuple[str, ...]
