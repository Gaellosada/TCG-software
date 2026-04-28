"""Pydantic v2 request/response models for the options API (Phase 1).

All models here mirror the frozen dataclasses in ``tcg.types.options`` but
use ``pydantic.BaseModel`` so they can be used for FastAPI request parsing
and response validation.

Per Decision F (ORDERS.md): routers return ``model.model_dump()`` — plain
dict on the wire. The Pydantic models exist for type/doc value, not for
direct JSON serialisation in the response body.

Per Decision D (ORDERS.md): ``ContractRowWithGreeks`` includes all
``OptionDailyRow`` fields PLUS 5 ``ComputeResult`` Greek fields alongside
the ``*_stored`` raw fields (self-documenting — both views are present).

Spec references: §3 (all 7 modules' Protocol signatures + DTO shapes),
§4.4 (ComputeResult definition), §5 (API request/response models).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# ComputeResult mirror
# ---------------------------------------------------------------------------


class ComputeResult(BaseModel):
    """Provenance-carrying wrapper for any Greek or quote-derived value.

    source="stored"   → model=None, inputs_used=None, error_*=None
    source="computed" → model and inputs_used non-null
    source="missing"  → value=None, error_code non-null, missing_inputs non-null
    """
    model_config = ConfigDict(frozen=True)

    value: float | None
    source: Literal["stored", "computed", "missing"]
    model: str | None = None
    inputs_used: Optional[Mapping[str, Any]] = None
    missing_inputs: tuple[str, ...] | None = None
    error_code: str | None = None
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Core contract/series Pydantic mirrors
# ---------------------------------------------------------------------------


class OptionContractDoc(BaseModel):
    """Static metadata of one option contract."""
    model_config = ConfigDict(frozen=True)

    collection: str
    contract_id: str
    root_underlying: str
    underlying_ref: str | None
    underlying_symbol: str | None
    expiration: date
    expiration_cycle: str
    strike: float
    type: Literal["C", "P"]
    contract_size: float | None
    currency: str | None
    provider: str
    strike_factor_verified: bool


class OptionDailyRow(BaseModel):
    """One trading day for one contract — quote + stored greeks."""
    model_config = ConfigDict(frozen=True)

    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    volume: float | None
    open_interest: float | None
    mid: float | None
    iv_stored: float | None
    delta_stored: float | None
    gamma_stored: float | None
    theta_stored: float | None
    vega_stored: float | None
    underlying_price_stored: float | None


class OptionContractSeries(BaseModel):
    """A contract with its full time-series of daily rows."""
    model_config = ConfigDict(frozen=True)

    contract: OptionContractDoc
    rows: list[OptionDailyRow]


class OptionRootInfo(BaseModel):
    """Per-root metadata returned by /api/options/roots."""
    model_config = ConfigDict(frozen=True)

    collection: str
    name: str
    has_greeks: bool
    providers: tuple[str, ...]
    expiration_first: date | None
    expiration_last: date | None
    doc_count_estimated: int
    strike_factor_verified: bool
    last_trade_date: date | None = None


# ---------------------------------------------------------------------------
# Greek envelope Pydantic mirror
# ---------------------------------------------------------------------------


class ComputedGreeks(BaseModel):
    """Set of five computed/stored greeks for one contract-row."""
    model_config = ConfigDict(frozen=True)

    iv: ComputeResult
    delta: ComputeResult
    gamma: ComputeResult
    theta: ComputeResult
    vega: ComputeResult


# ---------------------------------------------------------------------------
# Selection criterion discriminated union
# ---------------------------------------------------------------------------


class ByDelta(BaseModel):
    """Select the contract whose delta is nearest to target."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["by_delta"] = "by_delta"
    target_delta: float
    tolerance: float = 0.05
    strict: bool = False


class ByMoneyness(BaseModel):
    """Select by K/S ratio."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["by_moneyness"] = "by_moneyness"
    target_K_over_S: float
    tolerance: float = 0.01


class ByStrike(BaseModel):
    """Select by exact strike value."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["by_strike"] = "by_strike"
    strike: float


SelectionCriterion = Annotated[
    Union[ByDelta, ByMoneyness, ByStrike],
    Field(discriminator="kind"),
]


class SelectionResult(BaseModel):
    """Outcome of a Module 3 selection call."""
    model_config = ConfigDict(frozen=True)

    contract: OptionContractDoc | None
    matched_value: float | None
    error_code: str | None
    diagnostic: str | None


# ---------------------------------------------------------------------------
# Maturity rule discriminated union
# ---------------------------------------------------------------------------


class NextThirdFriday(BaseModel):
    """Roll to the third Friday of the offset month."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["next_third_friday"] = "next_third_friday"
    offset_months: int = 1


class EndOfMonth(BaseModel):
    """Roll to the last business day of the offset month."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["end_of_month"] = "end_of_month"
    offset_months: int = 0


class PlusNDays(BaseModel):
    """Roll to ref_date + n calendar days."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["plus_n_days"] = "plus_n_days"
    n: int


class FixedDate(BaseModel):
    """Use a fixed absolute date as the target expiration."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["fixed"] = "fixed"
    date: date


class NearestToTarget(BaseModel):
    """Find the available expiration nearest to target_dte_days from ref_date."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["nearest_to_target"] = "nearest_to_target"
    target_dte_days: int


MaturityRule = Annotated[
    Union[NextThirdFriday, EndOfMonth, PlusNDays, FixedDate, NearestToTarget],
    Field(discriminator="kind"),
]
# Alias for spec compatibility.
MaturitySpec = MaturityRule


# ---------------------------------------------------------------------------
# Roll rule discriminated union
# ---------------------------------------------------------------------------


class AtExpiry(BaseModel):
    """Roll into a new contract when the held contract expires."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["at_expiry"] = "at_expiry"


class NDaysBeforeExpiry(BaseModel):
    """Roll n calendar days before expiry (Phase 2 only)."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["n_days_before_expiry"] = "n_days_before_expiry"
    n: int


class DeltaCross(BaseModel):
    """Roll when |delta| crosses a threshold (Phase 2 only)."""
    model_config = ConfigDict(frozen=True)

    kind: Literal["delta_cross"] = "delta_cross"
    threshold: float


RollRule = Annotated[
    Union[AtExpiry, NDaysBeforeExpiry, DeltaCross],
    Field(discriminator="kind"),
]


class RollResult(BaseModel):
    """Outcome of a Module 5 roll evaluation."""
    model_config = ConfigDict(frozen=True)

    new_contract: OptionContractDoc | None
    roll_date: date | None
    reason: str
    error_code: str | None


# ---------------------------------------------------------------------------
# Chain types Pydantic mirrors
# ---------------------------------------------------------------------------


class ChainRow(BaseModel):
    """One row in the chain table for a given date."""
    model_config = ConfigDict(frozen=True)

    contract_id: str
    expiration: date
    type: Literal["C", "P"]
    strike: float
    K_over_S: float | None
    bid: float | None
    ask: float | None
    mid: float | None
    open_interest: float | None
    iv: ComputeResult
    delta: ComputeResult
    gamma: ComputeResult
    theta: ComputeResult
    vega: ComputeResult


class ChainSnapshot(BaseModel):
    """Full chain for a (root, date) pair (Module 6 output)."""
    model_config = ConfigDict(frozen=True)

    root: str
    date: date
    underlying_price: float | None
    rows: tuple[ChainRow, ...]
    notes: tuple[str, ...]


# ---------------------------------------------------------------------------
# P&L types Pydantic mirrors
# ---------------------------------------------------------------------------


class PnLPoint(BaseModel):
    """Daily mark and cumulative P&L for one contract on one date."""
    model_config = ConfigDict(frozen=True)

    date: date
    mark: float | None
    pnl_cumulative: float
    pnl_daily: float


class PnLSeries(BaseModel):
    """Full P&L replay for a held contract."""
    model_config = ConfigDict(frozen=True)

    contract: OptionContractDoc
    entry_date: date
    entry_price: float
    qty: float
    points: tuple[PnLPoint, ...]
    exit_reason: Literal["held_to_expiry", "exit_date", "contract_data_ended"] | None
    notes: tuple[str, ...]


# ---------------------------------------------------------------------------
# ContractRowWithGreeks (Decision D) — all OptionDailyRow fields +
# 5 ComputeResult Greek fields ALONGSIDE the *_stored raw fields
# ---------------------------------------------------------------------------


class ContractRowWithGreeks(BaseModel):
    """Per-contract daily row combining quote fields, stored greeks, and
    ComputeResult-wrapped Greek fields for the API response.

    Decision D: both the *_stored raw scalars AND the ComputeResult
    wrappers are present. This is intentionally self-documenting:
    callers can see both the raw stored value and its provenance wrapper.
    """
    model_config = ConfigDict(frozen=True)

    # Quote fields (from OptionDailyRow)
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    volume: float | None
    open_interest: float | None
    mid: float | None
    # Raw stored Greek scalars (from OptionDailyRow)
    iv_stored: float | None
    delta_stored: float | None
    gamma_stored: float | None
    theta_stored: float | None
    vega_stored: float | None
    underlying_price_stored: float | None
    # ComputeResult-wrapped Greek fields (added by router/Module 6)
    iv: ComputeResult
    delta: ComputeResult
    gamma: ComputeResult
    theta: ComputeResult
    vega: ComputeResult


# ---------------------------------------------------------------------------
# API Request models (§5.2)
# ---------------------------------------------------------------------------


class ListRootsRequest(BaseModel):
    """Request model for GET /api/options/roots (no parameters)."""
    pass


class ChainQuery(BaseModel):
    """Request model for GET /api/options/chain."""
    root: str
    date: date
    type: Literal["C", "P", "both"] = "both"
    expiration_min: date
    expiration_max: date
    strike_min: float | None = None
    strike_max: float | None = None
    compute_missing: bool = False


class ContractQuery(BaseModel):
    """Request model for GET /api/options/contract/{coll}/{id}."""
    collection: str
    contract_id: str
    compute_missing: bool = False
    date_from: date | None = None
    date_to: date | None = None


class SelectQuery(BaseModel):
    """Request model for GET /api/options/select."""
    root: str
    date: date
    type: Literal["C", "P"]
    criterion: SelectionCriterion
    maturity: MaturityRule
    compute_missing_for_delta_selection: bool = False


class ChainSnapshotQuery(BaseModel):
    """Request model for GET /api/options/chain-snapshot.

    ``expirations`` is limited to 8 entries (UI guard per spec §5
    and brief item I.10).
    """
    root: str
    date: date
    type: Literal["C", "P"] = "C"
    expirations: list[date]
    field: Literal["iv", "delta"] = "iv"

    @field_validator("expirations")
    @classmethod
    def max_eight_expirations(cls, v: list[date]) -> list[date]:
        if len(v) > 8:
            raise ValueError(
                f"At most 8 expirations allowed per request; got {len(v)}."
            )
        return v


# ---------------------------------------------------------------------------
# API Response models (§5.2)
# ---------------------------------------------------------------------------


class ListRootsResponse(BaseModel):
    """Response for GET /api/options/roots."""
    roots: list[OptionRootInfo]


class ChainResponse(BaseModel):
    """Response for GET /api/options/chain.

    ``underlying_price`` is a ComputeResult here (source="stored" with
    provider attribution, or "missing") per spec §5.2. The dataclass layer
    (Module 6) keeps it as ``float | None`` per Decision B; the router wraps
    it to ComputeResult before populating this model.
    """
    root: str
    date: date
    underlying_price: ComputeResult
    rows: list[ChainRow]
    notes: list[str]


class ContractResponse(BaseModel):
    """Response for GET /api/options/contract/{coll}/{id}."""
    contract: OptionContractDoc
    rows: list[ContractRowWithGreeks]


class SelectResponse(BaseModel):
    """Response for GET /api/options/select."""
    contract: OptionContractDoc | None
    matched_value: float | None
    error_code: str | None
    diagnostic: str | None


class SmilePoint(BaseModel):
    """One (strike, value) point in a smile curve."""
    strike: float
    K_over_S: float | None
    value: ComputeResult   # the IV or delta at this strike


class SmileSeries(BaseModel):
    """One expiration's smile curve."""
    expiration: date
    points: list[SmilePoint]


class ChainSnapshotResponse(BaseModel):
    """Response for GET /api/options/chain-snapshot."""
    root: str
    date: date
    underlying_price: ComputeResult
    series: list[SmileSeries]
