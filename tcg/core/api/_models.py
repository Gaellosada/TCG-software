"""Shared Pydantic request models for instrument / series references.

Both the signals and indicators routers accept the same shape for
identifying an instrument — either a spot instrument (collection +
instrument_id), a rolled continuous futures stream (collection +
adjustment + cycle + roll offset + strategy), an options-derived
stream (root + option_type + maturity + selection + stream), or a
basket (saved-reference OR inline composition).  Keeping the schema in
one place guarantees the routers can't drift apart and lets adapters
consume any shape without reimporting.

The options variant — :class:`OptionStreamRef` — REUSES the existing
discriminated unions from ``tcg.core.api._models_options``
(:data:`MaturityRule`, :data:`SelectionCriterion`) rather than
redeclaring them.  See guardrail 2 in the task brief.

The basket variant ships in two wire shapes that share the same
``type: "basket"`` discriminator value: ``{kind: "saved", basket_id}``
and ``{kind: "inline", asset_class, legs}``.  Since both share the
outer-discriminator tag, the union here is flattened and routed via a
*callable* :class:`Discriminator` that reads both ``type`` and
``kind``.  This (a) keeps the locked wire shape unchanged and
(b) emits an OpenAPI 3.x-compatible schema, which a nested
``Annotated[Union[...], Field(discriminator=...)]`` member does not.

Each inline-basket leg now carries a polymorphic ``instrument`` payload
discriminated on ``instrument.type`` (``spot`` / ``continuous`` /
``option_stream``) so a basket can mix asset-class-compatible spec
shapes (e.g. continuous-rolled futures legs) instead of being limited
to single-contract pointers.  The nested discriminator on
``BasketLeg.instrument`` is a single level deep (no nested-nested form)
which the standard ``Field(discriminator="type")`` machinery handles
cleanly — only the *outer* SeriesRef union needed the iter-1 callable
Discriminator refactor.

`BasketLeg` lives here (not in ``persistence.py``) because the
import-linter contract forbids ``_models.py`` from depending on
``persistence.py`` — they sit at the same layer but ``persistence.py``
imports application-layer write-repo bits that ``_models.py`` must
remain free of.  ``persistence.py`` redefines the same shape file-local
(iter-1/2 precedent).
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

from tcg.core.api._models_options import (
    MaturityRule,
    RollOffset,
    SelectionCriterion,
    reject_contradicting_delta_sign,
)


def _validate_nav_times(v: float) -> float:
    """Shared field-validator body for the hold-mode premium-notional multiple.

    A non-finite or non-positive ``nav_times`` makes the fixed-contract sizing
    (``nav_times·NAV/premium``) meaningless — reject at the boundary rather than
    emit NaN/inf P&L.  ONE source of truth shared by :class:`OptionStreamRef`
    (signals / baskets) and ``portfolio.LegSpec`` (portfolio legs) so the two
    can't drift.  (Only consulted in hold mode, but a bad value is always a spec
    error, so both models validate it unconditionally.)
    """
    if not math.isfinite(v) or v <= 0.0:
        raise ValueError("nav_times must be a finite number > 0")
    return float(v)


class SpotInstrumentRef(BaseModel):
    type: Literal["spot"]
    collection: str
    instrument_id: str


class ContinuousInstrumentRef(BaseModel):
    type: Literal["continuous"]
    collection: str
    adjustment: Literal["none", "ratio", "difference"] = "none"
    cycle: str | None = None
    # Accept camelCase from the frontend. Bounded 0..365 days for parity with
    # the Data-page continuous endpoint (data.py) and the portfolio continuous-
    # leg validator, so a signal's continuous instrument can't smuggle an
    # unbounded roll offset past those caps.
    rollOffset: int = Field(default=0, ge=0, le=365)
    # Issue #3: ``end_of_month`` rolls on the last trading day of each month
    # regardless of expiry; default ``front_month`` keeps existing refs valid.
    strategy: Literal["front_month", "end_of_month"] = "front_month"


# Streams readable off a single option contract row.  Listed verbatim
# from ``tcg.types.options.OptionDailyRow`` (numeric fields only); ``mid``
# is the canonical mark.  The resolver maps each label to the matching
# ``*_stored`` (or quote) field on the row.  ``bs_mid`` is the exception: a
# COMPUTED premium — the Black-76 theoretical price from the contract's stored
# IV + the underlying FUTURE price (the Java sim's price basis), intrinsic at
# expiry — not a row field.  It is price-like (a premium), like ``mid``.  ``close``
# is the raw EOD settlement price — the faithful realized mark for a held-to-roll
# option (>0-guarded in the resolver); also price-like.
OptionStreamLabel = Literal[
    "mid",
    "bs_mid",
    "close",
    "iv",
    "delta",
    "gamma",
    "vega",
    "theta",
    "open_interest",
    "volume",
]


class OptionStreamRef(BaseModel):
    """Series reference materialising a 1-D float stream off a selected
    option contract on each trade date.

    Wave 2a: backend-only.  The frontend picker (Wave 2b) builds the
    matching JSON.  The resolver lives at
    ``tcg/engine/options/series/stream_resolver.py``.

    The carried ``maturity`` and ``selection`` are the Pydantic v2
    discriminated unions from :mod:`tcg.core.api._models_options` —
    NOT redeclared here (guardrail 2).  The resolver translates them
    to their ``tcg.types.options`` dataclass twins before calling the
    engine selector.
    """

    type: Literal["option_stream"]
    collection: str
    option_type: Literal["C", "P"]
    # NOTE: option continuous series carry NO back-adjustment.  Ratio/difference
    # are conceptually ill-posed for option premia (a back-adjusted premium
    # represents no tradable instrument; theta decay toward 0 makes the ratio
    # factor diverge and the additive offset swamp the premia), so — unlike
    # ``ContinuousInstrumentRef`` (futures) — this model has no ``adjustment``
    # field.  The resolver always returns the raw stitched stream.  Legacy refs
    # that still carry an ``adjustment`` key are tolerated (extra fields are
    # ignored, not forbidden, on this model) and have no effect.
    # ``cycle`` filter — None means "no cycle filter applied" (caller's
    # intent for monthly is typically the explicit string "M" since some
    # roots — OPT_SP_500 — have weeklies named "W3 Friday" mixed in).
    # Blank-string normalisation matches ``ChainQuery`` (see
    # ``_models_options._blank_cycle_to_none``).
    cycle: str | None = None
    maturity: MaturityRule
    selection: SelectionCriterion
    # ASK B: default to ``mid`` so the roll/stream UX does not force the
    # caller to choose which series to extract.  ``mid`` is the option
    # premium mark — the natural default for a rolled options price series.
    # An explicit value (iv / delta / greeks / volume / open_interest) still
    # overrides; an out-of-enum value is still rejected by the Literal.
    stream: OptionStreamLabel = "mid"
    # Roll offset — the ROLL-EARLY axis: ``{value, unit: 'days'|'months'}``.  The
    # engine resolves the maturity rule as of ``date + offset`` for each date, so
    # every roll fires that much EARLIER.  Default ``{value:0}`` = no shift.
    # DISTINCT from the maturity rule's ``offset_months`` (the TARGET-month axis —
    # which expiration to aim at).  No-op for ``fixed`` maturity.  A shipped bare
    # int (the old days-only field) reads back as ``{value:int, unit:'days'}`` via
    # the model's before-validator.
    #
    # NOTE: "roll at end of month" is NOT expressed here — it is the ``EndOfMonth``
    # maturity rule (which makes the resolver hold one contract per month).  The
    # former separate ``roll_schedule`` field was removed (it duplicated that).
    roll_offset: RollOffset = Field(default_factory=RollOffset)
    # SELECT-AND-HOLD (default False = current daily-reselect behaviour).  When
    # True, the resolver picks the contract ONCE per maturity roll, HOLDS it
    # between rolls, and emits the per-date HELD-CONTRACT MID LEVEL (the OLD
    # contract's mid on the roll day) plus an is_roll/roll_premium side-channel;
    # signal_exec then books FIXED-CONTRACT DOLLAR P&L (a quantity sized once per
    # roll off the compounding NAV and the roll premium, qty·Δpremium daily) —
    # oracle-exact, and NOT a stitched level (so no option ratio-adjust).  Fixes
    # the meaningless P&L a delta/moneyness-selected option signal gets from the
    # daily strike churn AND from %-returns that explode as a held premium decays
    # toward zero.  The correct mode for BACKTESTING a delta-selected option; the
    # default (raw daily-reselect mid LEVEL) remains the Data-page/chart display
    # series.  Ignored by the display-only stream materialiser.
    hold_between_rolls: bool = False
    # PREMIUM-NOTIONAL MULTIPLE for the fixed-contract dollar-P&L sizing (hold mode
    # only).  Held quantity at each roll = ``nav_times · NAV_at_roll /
    # premium_at_roll``; DIRECTION (long/short) comes from the block WEIGHT SIGN.
    # ``nav_times`` may exceed 1 (leverage on the premium notional) which the
    # weight ∈ [-100, 100] cannot express — hence a separate field.  Must be finite
    # and > 0.  Ignored when ``hold_between_rolls`` is False.
    nav_times: float = 1.0
    # SIZING MODE (hold-mode $-P&L only).  ``premium_notional`` (DEFAULT, byte-
    # identical to shipped): qty = nav_times·NAV_roll/premium_roll.
    # ``futures_notional`` (opt-in): qty = nav_times·NAV_roll/(F_ref·M_fut) and
    # daily $ = qty·Δpremium·M_opt — sized off the corresponding FUTURE's notional.
    sizing_mode: Literal["premium_notional", "futures_notional"] = "premium_notional"
    # Reference-future selection — only meaningful when sizing_mode=='futures_notional'.
    # ``nearest_on_or_after`` (DEFAULT): nearest LISTED future expiring >= the option
    # expiry (root's real cycle); ``nearest_abs``: closest |time| expiry;
    # ``continuous_front``: the app's continuous front-month price at the roll date.
    futures_reference: Literal[
        "nearest_on_or_after", "continuous_front", "nearest_abs"
    ] = "nearest_on_or_after"

    @field_validator("cycle", mode="before")
    @classmethod
    def _blank_cycle_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("nav_times")
    @classmethod
    def _check_nav_times(cls, v: float) -> float:
        return _validate_nav_times(v)

    @model_validator(mode="after")
    def _default_hold_stream_to_close(self) -> "OptionStreamRef":
        """CONDITIONAL DEFAULT: a HOLD leg with NO explicit stream prices on
        ``close`` (the faithful EOD settlement mark), not ``mid``.

        For a held-to-roll option the exchange settlement ``close`` reproduces the
        ground-truth realized P&L to the cent, while the bid-ask ``mid`` is
        materially STALE (see ``output/price_source_and_multiplier.md``).  So a
        NEW hold leg that did not pick a stream should default to ``close``.

        The default is resolved at CONSTRUCTION using ``model_fields_set`` so it is
        applied ONLY when ``stream`` was absent from the payload:
          * NON-hold legs are untouched (they keep the ``mid`` display default);
          * a leg that EXPLICITLY set ``stream`` (mid/bs_mid/close/…) is honoured
            verbatim — never coerced;
          * PERSISTED legs are safe: persistence always serialises ``stream``
            explicitly (``signals._serialize_instrument`` / the basket mirror), so
            a reload carries ``stream`` in ``model_fields_set`` and is never
            flipped — saved research stays reproducible.
        """
        if self.hold_between_rolls and "stream" not in self.model_fields_set:
            self.stream = "close"
        return self

    @model_validator(mode="after")
    def _check_delta_sign_matches_type(self) -> "OptionStreamRef":
        """Reject a ``ByDelta`` target whose sign contradicts ``option_type``.

        This model is the only place BOTH ``option_type`` and the ``ByDelta``
        selection are known, so the sign-vs-type rule is enforced here (a PUT
        needs ``target_delta <= 0``, a CALL ``>= 0``; ``0`` is allowed).  NO-OP
        for every correctly-signed selection and for non-ByDelta criteria.
        """
        reject_contradicting_delta_sign(self.option_type, self.selection)
        return self


# Per-asset-class strict mapping from the basket's declared ``asset_class``
# to the leg's ``instrument.type``.  Equity and index baskets accept spot
# legs only; futures must roll into a continuous spec; options must roll
# into an option-stream spec.  The validator on ``BasketRefInline`` reads
# this map to compute mismatch detail messages.
_ASSET_CLASS_TO_INSTRUMENT_TYPE: dict[str, str] = {
    "equity": "spot",
    "index": "spot",
    "future": "continuous",
    "option": "option_stream",
}


class BasketLeg(BaseModel):
    """A leg in an *inline* basket descriptor — polymorphic.

    Each leg carries an ``instrument`` sub-object whose ``type``
    discriminator selects one of:

    * ``"spot"`` → :class:`SpotInstrumentRef` (equity / index legs)
    * ``"continuous"`` → :class:`ContinuousInstrumentRef` (future legs;
      rolled, possibly adjusted)
    * ``"option_stream"`` → :class:`OptionStreamRef` (option legs)

    ``weight`` is a signed fraction (positive = long, negative = short)
    and must be non-zero.  Strict per-asset-class enforcement lives on
    :class:`BasketRefInline` (model-level validator) — at the leg level
    we accept any of the three shapes; the basket envelope rejects
    mismatches.

    This model is defined here rather than imported from
    ``tcg.core.api.persistence`` to satisfy the import-linter contract
    that keeps ``_models.py`` free of write-layer dependencies (Sign 8
    of the iter-3 guardrails).  ``persistence.py`` redefines the same
    shape file-local; the two definitions track each other.
    """

    model_config = ConfigDict(extra="forbid")

    instrument: Annotated[
        Union[SpotInstrumentRef, ContinuousInstrumentRef, OptionStreamRef],
        Field(discriminator="type"),
    ]
    weight: float = Field(..., description="signed; must be non-zero")

    @field_validator("weight")
    @classmethod
    def _check_weight_nonzero(cls, v: float) -> float:
        if v == 0.0:
            raise ValueError("weight must be non-zero")
        return v


class BasketRefSaved(BaseModel):
    """Saved-basket reference — points to a persisted ``BasketDoc``.

    The API layer looks up the basket in MongoDB at signal-resolution
    time (via ``_resolve_basket_inputs``), snapshots its legs, and
    constructs an :class:`~tcg.types.signal.InstrumentBasket`.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["basket"] = "basket"
    kind: Literal["saved"] = "saved"
    basket_id: str = Field(..., min_length=1, max_length=128)


class BasketRefInline(BaseModel):
    """Inline-basket descriptor — legs supplied on the wire.

    Skips the DB pre-pass entirely: ``_parse_input`` builds the
    :class:`~tcg.types.signal.InstrumentBasket` directly from
    ``asset_class`` + polymorphic ``legs``.

    Strict per-class mapping: each leg's ``instrument.type`` must
    match the declared ``asset_class`` per
    ``_ASSET_CLASS_TO_INSTRUMENT_TYPE``.  Mismatches raise a 422 at
    request-validation time with a detail naming the leg index and the
    expected ``instrument.type``.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["basket"] = "basket"
    kind: Literal["inline"] = "inline"
    asset_class: Literal["future", "option", "index", "equity"]
    legs: list[BasketLeg] = Field(..., min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_strict_per_class_mapping(self) -> "BasketRefInline":
        expected = _ASSET_CLASS_TO_INSTRUMENT_TYPE[self.asset_class]
        for i, leg in enumerate(self.legs):
            actual = leg.instrument.type
            if actual != expected:
                raise ValueError(
                    f"basket leg {i}: asset_class={self.asset_class!r} "
                    f"requires instrument.type={expected!r}, got {actual!r}"
                )
        return self


def _series_ref_discriminator(v: Any) -> str | None:
    """Map an incoming payload (dict or model instance) to its tag.

    Combines the outer ``type`` discriminator with the inner ``kind``
    discriminator for the basket branch.  The five tags map 1:1 to the
    five concrete Pydantic models below.

    Returns ``None`` when the payload is missing the keys needed to
    route — Pydantic surfaces that as a ``union_tag_not_found`` error
    naming the discriminator function (the same error class the user
    would see from a missing nested discriminator, with a clearer
    message because it tells them which key is absent).
    """
    if isinstance(v, dict):
        outer = v.get("type")
        if outer == "basket":
            inner = v.get("kind")
            if inner == "saved":
                return "basket_saved"
            if inner == "inline":
                return "basket_inline"
            return None  # missing/unknown kind
        return outer  # "spot" | "continuous" | "option_stream"
    # Model instance — read attrs.
    outer = getattr(v, "type", None)
    if outer == "basket":
        inner = getattr(v, "kind", None)
        if inner == "saved":
            return "basket_saved"
        if inner == "inline":
            return "basket_inline"
        return None
    return outer


# Outer + inner discriminator collapsed into a single flat union with
# per-member ``Tag``s.  Pydantic resolves the right model on inbound
# validation; FastAPI emits a single discriminator block in the OpenAPI
# schema (the OpenAPI 3.0 spec doesn't support nested discriminators
# inside a discriminator branch).
BasketRef = Annotated[
    Union[
        Annotated[BasketRefSaved, Tag("basket_saved")],
        Annotated[BasketRefInline, Tag("basket_inline")],
    ],
    Discriminator(_series_ref_discriminator),
]


SeriesRef = Annotated[
    Union[
        Annotated[SpotInstrumentRef, Tag("spot")],
        Annotated[ContinuousInstrumentRef, Tag("continuous")],
        Annotated[OptionStreamRef, Tag("option_stream")],
        Annotated[BasketRefSaved, Tag("basket_saved")],
        Annotated[BasketRefInline, Tag("basket_inline")],
    ],
    Discriminator(_series_ref_discriminator),
]
