"""Shared Pydantic request models for instrument / series references.

Both the signals and indicators routers accept the same shape for
identifying an instrument — either a spot instrument (collection +
instrument_id), a rolled continuous futures stream (collection +
adjustment + cycle + roll offset + strategy), or an options-derived
stream (root + option_type + maturity + selection + stream).  Keeping
the schema in one place guarantees the routers can't drift apart and
lets adapters consume any shape without reimporting.

The options variant — :class:`OptionStreamRef` — REUSES the existing
discriminated unions from ``tcg.core.api._models_options``
(:data:`MaturityRule`, :data:`SelectionCriterion`) rather than
redeclaring them.  See guardrail 2 in the task brief.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

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


class BasketRef(BaseModel):
    """Reference to a persisted basket by id.

    The API layer looks up the basket in MongoDB at signal-resolution
    time, snapshots its legs, and constructs an :class:`InstrumentBasket`.
    Only the ``basket_id`` is carried over the wire — legs are resolved
    server-side so the frontend never has to mirror that state.
    """

    type: Literal["basket"]
    basket_id: str = Field(..., min_length=1, max_length=128)


SeriesRef = Annotated[
    SpotInstrumentRef | ContinuousInstrumentRef | OptionStreamRef | BasketRef,
    Field(discriminator="type"),
]
