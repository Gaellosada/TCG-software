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

The basket variant is a *nested* discriminated union: the outer
``SeriesRef`` discriminates on ``type`` (``"basket"`` selects the
``BasketRef`` branch); inside that branch a second discriminator on
``kind`` separates ``BasketRefSaved`` (DB-resolved) from
``BasketRefInline`` (legs supplied directly on the wire — no DB
pre-pass).  Pydantic v2 supports nested discriminators natively.

`BasketLegInLite` lives here (not in ``persistence.py``) because the
import-linter contract forbids ``_models.py`` from depending on
``persistence.py`` — they sit at the same layer but ``persistence.py``
imports application-layer write-repo bits that ``_models.py`` must
remain free of.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tcg.core.api._models_options import MaturityRule, SelectionCriterion


class SpotInstrumentRef(BaseModel):
    type: Literal["spot"]
    collection: str
    instrument_id: str


class ContinuousInstrumentRef(BaseModel):
    type: Literal["continuous"]
    collection: str
    adjustment: Literal["none", "ratio", "difference"] = "none"
    cycle: str | None = None
    # Accept camelCase from the frontend.
    rollOffset: int = 0
    strategy: Literal["front_month"] = "front_month"


# Streams readable off a single option contract row.  Listed verbatim
# from ``tcg.types.options.OptionDailyRow`` (numeric fields only); ``mid``
# is the canonical mark.  The resolver maps each label to the matching
# ``*_stored`` (or quote) field on the row.
OptionStreamLabel = Literal[
    "mid",
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
    # ``cycle`` filter — None means "no cycle filter applied" (caller's
    # intent for monthly is typically the explicit string "M" since some
    # roots — OPT_SP_500 — have weeklies named "W3 Friday" mixed in).
    # Blank-string normalisation matches ``ChainQuery`` (see
    # ``_models_options._blank_cycle_to_none``).
    cycle: str | None = None
    maturity: MaturityRule
    selection: SelectionCriterion
    stream: OptionStreamLabel

    @field_validator("cycle", mode="before")
    @classmethod
    def _blank_cycle_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class BasketLegInLite(BaseModel):
    """A leg in an *inline* basket descriptor.

    The wire shape for inline-basket legs intentionally omits
    ``collection`` (the FE does not know per-leg collections — the BE
    derives them at parse time from the declared ``asset_class``).
    ``weight`` is a signed fraction (positive = long, negative = short)
    and must be non-zero (mirrors the rule on the persisted
    ``BasketLegIn`` model).

    This model is duplicated rather than imported from
    ``tcg.core.api.persistence`` to satisfy the import-linter contract
    that keeps ``_models.py`` free of write-layer dependencies.
    """

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(..., min_length=1, max_length=128)
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
    ``asset_class`` + ``legs``, resolving each leg's host MongoDB
    collection in-place.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["basket"] = "basket"
    kind: Literal["inline"] = "inline"
    asset_class: Literal["future", "option", "index", "equity"]
    legs: list[BasketLegInLite] = Field(..., min_length=1, max_length=64)


# Inner discriminator: ``kind`` separates the two basket shapes.
BasketRef = Annotated[
    Union[BasketRefSaved, BasketRefInline],
    Field(discriminator="kind"),
]


# Outer discriminator: ``type`` separates the four series-ref shapes.
# Pydantic v2 supports a discriminated union member that is itself a
# discriminated union (nested discriminator); each branch is selected on
# the outer key first, then the inner key inside the basket branch.
SeriesRef = Annotated[
    Union[
        SpotInstrumentRef,
        ContinuousInstrumentRef,
        OptionStreamRef,
        BasketRef,
    ],
    Field(discriminator="type"),
]
