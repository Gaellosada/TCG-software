"""Shared Pydantic request models for instrument / series references.

Both the signals and indicators routers accept the same shape for
identifying an instrument — either a spot instrument (collection +
instrument_id) or a rolled continuous futures stream (collection +
adjustment + cycle + roll offset + strategy). Keeping the schema in
one place guarantees the two routers can't drift apart and lets
adapters (roll-config builder, fetcher factory) consume either shape
without reimporting.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class SpotInstrumentRef(BaseModel):
    type: Literal["spot"]
    collection: str
    instrument_id: str


class ContinuousInstrumentRef(BaseModel):
    type: Literal["continuous"]
    collection: str
    adjustment: Literal["none", "proportional", "difference"] = "none"
    cycle: str | None = None
    # Accept camelCase from the frontend.
    rollOffset: int = 0
    strategy: Literal["front_month"] = "front_month"


SeriesRef = Annotated[
    SpotInstrumentRef | ContinuousInstrumentRef, Field(discriminator="type")
]
