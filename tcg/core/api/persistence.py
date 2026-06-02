"""HTTP CRUD endpoints for indicators, signals, and portfolios.

The router uses Pydantic v2 models at the boundary and converts to/from
the ``tcg.types.persistence`` dataclasses at the edge. Dataclasses do
not leak through the HTTP interface — keeps the wire schema decoupled
from the internal storage shape.

Endpoint map
------------
POST   /api/persistence/indicators           create
GET    /api/persistence/indicators           list (active only)
GET    /api/persistence/indicators/{id}      get
PUT    /api/persistence/indicators/{id}      update (full replace)
DELETE /api/persistence/indicators/{id}      archive (soft delete)

POST   /api/persistence/signals              create
GET    /api/persistence/signals?category=…   list by category (required)
GET    /api/persistence/signals/{id}         get
PUT    /api/persistence/signals/{id}         update
DELETE /api/persistence/signals/{id}         archive

POST   /api/persistence/portfolios           create
GET    /api/persistence/portfolios?category= list by category (required)
GET    /api/persistence/portfolios/{id}      get
PUT    /api/persistence/portfolios/{id}      update
DELETE /api/persistence/portfolios/{id}      archive

Signal / portfolio wire shape
-----------------------------
The full editable state of a signal or portfolio is persisted by these
endpoints. For signals: ``inputs`` / ``rules`` / ``settings`` /
``description`` are carried as opaque payloads (the persistence layer
does not interpret them). For portfolios: ``legs`` / ``rebalance``
likewise. Optional fields default to empty values on the backend so
callers may omit anything that isn't set yet.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeVar, Union

_log = logging.getLogger(__name__)

import pymongo.errors
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from tcg.core.api._models_options import MaturityRule, SelectionCriterion
from tcg.core.api._persistence_wiring import get_write_repository
from tcg.persistence import (
    ConcurrentUpdateError,
    DocumentTooLargeError,
    WriteRepository,
)
from tcg.types.persistence import (
    BasketDoc,
    Category,
    DocType,
    IndicatorDoc,
    PersistenceDoc,
    PortfolioDoc,
    SignalDoc,
)


router = APIRouter(prefix="/api/persistence", tags=["persistence"])


# ---------------------------------------------------------------------------
# Validation helpers (M2: input tightening — depth / shape / pattern guards)
# ---------------------------------------------------------------------------


# Identifier regex: alphanumerics plus a small punctuation set. Forbids
# the Mongo operator prefix ``$``, leading ``.`` (used by Mongo path
# operators), and any whitespace / control characters. Length-capped
# separately by ``min_length=1, max_length=128``.
_ID_PATTERN = r"^[A-Za-z0-9_\-:.]+$"

# Depth and size caps. MongoDB BSON limits nesting at 100 levels and a
# single doc at 16 MB; we set defensive ceilings well below both so a
# malicious or buggy client surfaces a clean 422 long before Mongo's
# server-side limits trip. Together with the body-size middleware
# (B3) these turn pathological-payload scenarios into validation errors
# rather than 500s.
_MAX_PAYLOAD_DEPTH = 16
_MAX_PAYLOAD_SERIALIZED_BYTES = 1_000_000  # 1 MB per opaque payload field

# Path-parameter validation for ``doc_id`` on GET/PUT/DELETE endpoints.
# Matches the same pattern and length constraints applied to ``id`` on
# create, closing the defense-in-depth gap (B3).
DocId = Annotated[str, Path(min_length=1, max_length=128, pattern=_ID_PATTERN)]

# TypeVar for the _checked helper below.
_T = TypeVar("_T", IndicatorDoc, SignalDoc, PortfolioDoc, BasketDoc)


def _max_depth(value: Any, _depth: int = 0) -> int:
    """Return the maximum nesting depth of ``value``.

    Walks dicts and lists/tuples recursively. Scalars count as depth
    ``_depth``. Bails out early when ``_depth`` exceeds
    ``_MAX_PAYLOAD_DEPTH`` to prevent stack overflow from maliciously
    crafted deep payloads — recursion is capped at
    ``_MAX_PAYLOAD_DEPTH + 1`` levels regardless of input shape.
    """
    if _depth > _MAX_PAYLOAD_DEPTH:
        return _depth  # already over the limit — no need to recurse further
    if isinstance(value, dict):
        if not value:
            return _depth
        return max(_max_depth(v, _depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        if not value:
            return _depth
        return max(_max_depth(v, _depth + 1) for v in value)
    return _depth


def _validate_payload(value: Any, field_name: str) -> Any:
    """Reject payloads that exceed the depth or serialized-size caps.

    Used by Pydantic ``field_validator`` hooks on every opaque payload
    field (``rules`` / ``inputs`` / ``settings`` / ``definition`` /
    ``legs``). Depth is checked first since it's cheap; the serialized
    size check uses ``len(json.dumps(...).encode('utf-8'))`` so the
    measurement is the actual wire-byte count (close to the BSON byte
    count Mongo will see), not the Python repr length which over- or
    under-counted depending on the codepoint range. ``default=str`` so
    we never blow up on a non-JSON value the caller smuggled in — that
    failure mode will surface as a Pydantic type error elsewhere.
    """
    depth = _max_depth(value)
    if depth > _MAX_PAYLOAD_DEPTH:
        raise ValueError(
            f"{field_name}: nesting depth {depth} exceeds limit {_MAX_PAYLOAD_DEPTH}"
        )
    # Closer-to-truth size guard than ``repr(value)``. ``ensure_ascii=
    # False`` so multi-byte UTF-8 characters count as their actual
    # serialized byte count, not the 6-byte ``\uXXXX`` escape.
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # If we can't serialize for measurement we cannot enforce the
        # size cap; fall back to ``repr()`` for an upper-bound estimate.
        serialized = repr(value)
    size = len(serialized.encode("utf-8"))
    if size > _MAX_PAYLOAD_SERIALIZED_BYTES:
        raise ValueError(
            f"{field_name}: serialized size {size} exceeds limit "
            f"{_MAX_PAYLOAD_SERIALIZED_BYTES}"
        )
    return value


# ---------------------------------------------------------------------------
# Basket asset-class strict per-leg mapping
# ---------------------------------------------------------------------------


# Strict per-asset-class mapping from the basket envelope's declared
# ``asset_class`` to the required leg ``instrument.type``.  Mirrored
# in ``tcg.core.api._models._ASSET_CLASS_TO_INSTRUMENT_TYPE`` for the
# inline-signal-input path; redefined file-local here to honour the
# import-linter boundary (Sign 8 of the iter-3 guardrails forbids
# ``persistence.py`` importing from ``_models.py``).
_ASSET_CLASS_TO_INSTRUMENT_TYPE: dict[str, str] = {
    "equity": "spot",
    "index": "spot",
    "future": "continuous",
    "option": "option_stream",
}


def _check_basket_homogeneity(asset_class: str, legs: list["BasketLegIn"]) -> None:
    """Reject baskets whose legs don't match the declared asset class.

    Strict per-class mapping (iter-3):

    * ``asset_class="equity"`` or ``"index"`` → each leg's
      ``instrument.type`` must be ``"spot"``.
    * ``asset_class="future"`` → each leg's ``instrument.type`` must
      be ``"continuous"``.
    * ``asset_class="option"`` → each leg's ``instrument.type`` must
      be ``"option_stream"``.

    Raises ``HTTPException(400)`` with a detail naming the leg index
    and the expected ``instrument.type`` on any mismatch.  Empty
    baskets are permitted so the frontend can save partial work.
    """
    if not legs:
        return
    expected = _ASSET_CLASS_TO_INSTRUMENT_TYPE.get(asset_class)
    if expected is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"basket asset_class {asset_class!r} is not supported "
                f"(expected one of {sorted(_ASSET_CLASS_TO_INSTRUMENT_TYPE)!r})"
            ),
        )
    for i, leg in enumerate(legs):
        actual = leg.instrument.type
        if actual != expected:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"basket leg {i}: asset_class={asset_class!r} "
                    f"requires instrument.type={expected!r}, got {actual!r}"
                ),
            )


def _canonical_instrument_hash(instrument: Any) -> tuple:
    """Return a stable, hashable canonical representation of a basket
    leg's ``instrument`` sub-object.

    Used by :func:`_check_basket_no_duplicates` and by the engine's
    :func:`_instrument_identity` to discriminate legs that look the same
    on instrument_id alone but differ on adjustment / cycle / rollOffset
    / option-stream selection.  Two legs with structurally-identical
    ``instrument`` payloads return equal hashes regardless of dict
    insertion order.
    """
    payload = instrument.model_dump(mode="python")
    return _freeze(payload)


def _freeze(value: Any) -> Any:
    """Recursively turn dicts/lists into sorted tuples for hashing."""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _check_basket_no_duplicates(legs: list["BasketLegIn"]) -> None:
    """Reject duplicate legs within the same basket.

    A duplicate is two legs whose full ``instrument`` spec hashes
    identically AND share the same ``weight``.  Two legs with the
    same instrument but different weights are NOT duplicates (the
    user may want to express a directional or sizing layering).
    """
    seen: set[tuple] = set()
    for i, leg in enumerate(legs):
        key = (_canonical_instrument_hash(leg.instrument), float(leg.weight))
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=(f"basket leg {i}: duplicate (instrument, weight) pair"),
            )
        seen.add(key)


# Accepted ``rebalance`` values — mirrors ``tcg.types.portfolio.RebalanceFreq``
# (and the frontend ``REBALANCE_OPTIONS``). Using ``Literal`` here lets
# Pydantic emit a precise 422 with the allowed values rather than
# accepting any free string.
RebalanceLiteral = Literal[
    "none", "daily", "weekly", "monthly", "quarterly", "annually"
]


# ---------------------------------------------------------------------------
# Pydantic wire models
# ---------------------------------------------------------------------------


class _BaseWriteModel(BaseModel):
    """Common configuration for create/update payloads."""

    model_config = {"extra": "forbid"}


class _IndicatorFields(_BaseWriteModel):
    """Shared fields and validators for indicator create/update payloads (B7)."""

    name: str = Field(..., min_length=1, max_length=512)
    definition: dict

    @field_validator("definition")
    @classmethod
    def _check_definition(cls, v: dict) -> dict:
        return _validate_payload(v, "definition")


class IndicatorCreateIn(_IndicatorFields):
    """Create-payload for an indicator. The server stamps timestamps."""

    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)


class IndicatorUpdateIn(_IndicatorFields):
    """Update-payload. ``id`` comes from the URL path; we re-supply the
    full document body (full-replace semantics — the repository does a
    ``replace_one``).
    """

    deleted: bool = False


class _SignalFields(_BaseWriteModel):
    """Shared fields and validators for signal create/update payloads (B7)."""

    name: str = Field(..., min_length=1, max_length=512)
    category: Category
    inputs: list[dict] = Field(default_factory=list)
    rules: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)
    description: str = Field(default="", max_length=4096)

    @field_validator("inputs")
    @classmethod
    def _check_inputs(cls, v: list[dict]) -> list[dict]:
        return _validate_payload(v, "inputs")

    @field_validator("rules")
    @classmethod
    def _check_rules(cls, v: dict) -> dict:
        return _validate_payload(v, "rules")

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, v: dict) -> dict:
        return _validate_payload(v, "settings")


class SignalCreateIn(_SignalFields):
    """Create-payload for a signal.

    The interesting editable content (``inputs`` / ``rules`` /
    ``settings`` / ``description``) is OPTIONAL — a freshly created
    signal can be empty. Defaults are empty list / dict / string.
    """

    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)


class SignalUpdateIn(_SignalFields):
    """Update-payload — full replace. Same shape as create minus ``id``."""


class _PortfolioFields(_BaseWriteModel):
    """Shared fields and validators for portfolio create/update payloads (B7)."""

    name: str = Field(..., min_length=1, max_length=512)
    category: Category
    legs: list[dict] = Field(default_factory=list)
    rebalance: RebalanceLiteral = "none"

    @field_validator("legs")
    @classmethod
    def _check_legs(cls, v: list[dict]) -> list[dict]:
        return _validate_payload(v, "legs")


class PortfolioCreateIn(_PortfolioFields):
    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)


class PortfolioUpdateIn(_PortfolioFields):
    pass


# ---------------------------------------------------------------------------
# Basket wire models
# ---------------------------------------------------------------------------

# File-local copies of the three instrument-ref shapes from
# ``tcg.core.api._models``.  The import-linter contract (Sign 8 of the
# iter-3 guardrails) forbids ``persistence.py`` importing names from
# ``_models.py``; mirroring the iter-1/2 ``BasketLegInLite`` precedent,
# we redefine the shapes here.  The two copies track each other —
# any change to one MUST land here too.
#
# These are dict-literal ``model_config`` (not ``ConfigDict``) to match
# the rest of the basket models in this file (iter-2 reviewer accepted
# the dict-literal form as the file-local convention here).


_OptionStreamLabel = Literal[
    "mid",
    "iv",
    "delta",
    "gamma",
    "vega",
    "theta",
    "open_interest",
    "volume",
]


class _SpotInstrumentRefLocal(BaseModel):
    """File-local mirror of ``_models.SpotInstrumentRef``."""

    model_config = {"extra": "forbid"}

    type: Literal["spot"]
    collection: str = Field(..., min_length=1, max_length=128)
    instrument_id: str = Field(..., min_length=1, max_length=256)


class _ContinuousInstrumentRefLocal(BaseModel):
    """File-local mirror of ``_models.ContinuousInstrumentRef``."""

    model_config = {"extra": "forbid"}

    type: Literal["continuous"]
    collection: str = Field(..., min_length=1, max_length=128)
    adjustment: Literal["none", "ratio", "difference"] = "none"
    cycle: str | None = Field(default=None, max_length=16)
    rollOffset: int = 0
    strategy: Literal["front_month"] = "front_month"


class _OptionStreamRefLocal(BaseModel):
    """File-local mirror of ``_models.OptionStreamRef``."""

    model_config = {"extra": "forbid"}

    type: Literal["option_stream"]
    collection: str = Field(..., min_length=1, max_length=128)
    option_type: Literal["C", "P"]
    cycle: str | None = Field(default=None, max_length=16)
    maturity: MaturityRule
    selection: SelectionCriterion
    stream: _OptionStreamLabel

    @field_validator("cycle", mode="before")
    @classmethod
    def _blank_cycle_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class BasketLegIn(BaseModel):
    """A single leg in a basket create/update payload — polymorphic.

    Each leg carries a discriminated ``instrument`` sub-object
    (``spot`` / ``continuous`` / ``option_stream``) plus a non-zero
    signed ``weight``.  Asset-class homogeneity across the basket is
    enforced separately by :func:`_check_basket_homogeneity`, which
    reads the envelope-level ``asset_class`` and rejects any leg whose
    ``instrument.type`` doesn't match the strict per-class mapping.

    Mirrors the inline-input :class:`tcg.core.api._models.BasketLeg`.
    """

    model_config = {"extra": "forbid"}

    instrument: Annotated[
        Union[
            _SpotInstrumentRefLocal,
            _ContinuousInstrumentRefLocal,
            _OptionStreamRefLocal,
        ],
        Field(discriminator="type"),
    ]
    weight: float = Field(...)

    @field_validator("weight")
    @classmethod
    def _check_weight_nonzero(cls, v: float) -> float:
        if v == 0.0 or not math.isfinite(v):
            raise ValueError("weight must be a finite non-zero number")
        return v


# Asset class declared on the basket envelope — drives the strict
# per-class mapping enforced by ``_check_basket_homogeneity``.
BasketAssetClassLiteral = Literal["future", "option", "index", "equity"]


class BasketIn(_BaseWriteModel):
    """Create-payload for a basket."""

    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)
    name: str = Field(..., min_length=1, max_length=512)
    category: Category
    asset_class: BasketAssetClassLiteral
    legs: list[BasketLegIn] = Field(default_factory=list)

    @field_validator("legs")
    @classmethod
    def _check_legs(cls, v: list[BasketLegIn]) -> list[BasketLegIn]:
        # Reuse the shared depth/size guard. Pydantic has already validated
        # each leg's shape; here we just bound aggregate payload size.
        _validate_payload([leg.model_dump() for leg in v], "legs")
        return v


class BasketUpdateIn(_BaseWriteModel):
    """Update-payload — full replace. Same shape as create minus ``id``."""

    name: str = Field(..., min_length=1, max_length=512)
    category: Category
    asset_class: BasketAssetClassLiteral
    legs: list[BasketLegIn] = Field(default_factory=list)

    @field_validator("legs")
    @classmethod
    def _check_legs(cls, v: list[BasketLegIn]) -> list[BasketLegIn]:
        _validate_payload([leg.model_dump() for leg in v], "legs")
        return v


class BasketOut(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    type: str
    name: str
    category: Category
    asset_class: str
    created_at: datetime
    updated_at: datetime
    legs: list[dict]


class IndicatorOut(BaseModel):
    id: str
    type: str
    name: str
    definition: dict
    created_at: datetime
    updated_at: datetime
    deleted: bool


class SignalOut(BaseModel):
    id: str
    type: str
    name: str
    category: Category
    created_at: datetime
    updated_at: datetime
    inputs: list[dict]
    rules: dict
    settings: dict
    description: str


class PortfolioOut(BaseModel):
    id: str
    type: str
    name: str
    category: Category
    created_at: datetime
    updated_at: datetime
    legs: list[dict]
    rebalance: str


# ---------------------------------------------------------------------------
# Dataclass ↔ wire conversion
# ---------------------------------------------------------------------------


def _indicator_to_out(doc: IndicatorDoc) -> IndicatorOut:
    return IndicatorOut(
        id=doc.id,
        type=doc.type,
        name=doc.name,
        definition=doc.definition,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        deleted=doc.deleted,
    )


def _signal_to_out(doc: SignalDoc) -> SignalOut:
    return SignalOut(
        id=doc.id,
        type=doc.type,
        name=doc.name,
        category=doc.category,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        inputs=list(doc.inputs),
        rules=doc.rules,
        settings=doc.settings,
        description=doc.description,
    )


def _portfolio_to_out(doc: PortfolioDoc) -> PortfolioOut:
    return PortfolioOut(
        id=doc.id,
        type=doc.type,
        name=doc.name,
        category=doc.category,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        legs=list(doc.legs),
        rebalance=doc.rebalance,
    )


def _basket_to_out(doc: BasketDoc) -> BasketOut:
    return BasketOut(
        id=doc.id,
        type=doc.type,
        name=doc.name,
        category=doc.category,
        asset_class=doc.asset_class,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        legs=list(doc.legs),
    )


def _legs_to_mongo(legs: list[BasketLegIn]) -> tuple[dict, ...]:
    """Serialise wire-shape basket legs into the Mongo-bound dict form.

    Each leg dict is ``{"instrument": <polymorphic-sub-dict>,
    "weight": float}``.  ``model_dump(mode="json")`` collapses any
    nested Pydantic models on ``instrument`` into plain JSON-compatible
    dicts so the dataclass ``BasketDoc.legs`` (opaque tuples of dicts)
    can round-trip through ``to_mongo_dict`` / ``from_mongo_dict``.
    """
    return tuple(
        {
            "instrument": leg.instrument.model_dump(mode="json"),
            "weight": float(leg.weight),
        }
        for leg in legs
    )


def _checked(doc: PersistenceDoc, expected: type[_T]) -> _T:
    """Verify that ``doc`` is an instance of ``expected`` and return it.

    Replaces bare ``assert isinstance(...)`` calls throughout the
    endpoints (B5). Unlike ``assert``, this check cannot be stripped
    by ``python -O`` and produces a clean 500 with a diagnostic
    message rather than a confusing ``AttributeError``.
    """
    if not isinstance(doc, expected):
        raise HTTPException(
            status_code=500,
            detail=f"unexpected doc type: {type(doc).__name__}",
        )
    return doc  # type: ignore[return-value]


def _expect(doc: PersistenceDoc | None, kind: str, doc_id: str) -> PersistenceDoc:
    """Raise a 404 when the repository returns None on a get_by_id."""
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"{kind} not found: id={doc_id!r}",
        )
    return doc


def _now() -> datetime:
    """Placeholder timestamp passed to the dataclass constructor on
    create. The repository overwrites both timestamps server-side; this
    value never reaches Mongo."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Indicator endpoints
# ---------------------------------------------------------------------------


RepoDep = Annotated[WriteRepository, Depends(get_write_repository)]


@router.post("/indicators", response_model=IndicatorOut, status_code=201)
async def create_indicator(body: IndicatorCreateIn, repo: RepoDep) -> IndicatorOut:
    now = _now()
    doc = IndicatorDoc(
        id=body.id,
        type=DocType.INDICATOR.value,
        name=body.name,
        definition=body.definition,
        created_at=now,
        updated_at=now,
    )
    try:
        stored = await repo.create(doc)
    except pymongo.errors.DuplicateKeyError as exc:
        # Map the unique-index violation to 409 so retries / React 18
        # StrictMode double-mounts / racing clients see a structured
        # error rather than an unhandled 500.
        raise HTTPException(
            status_code=409, detail=f"indicator with id={body.id!r} already exists"
        ) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, IndicatorDoc)
    return _indicator_to_out(stored)


@router.get("/indicators", response_model=list[IndicatorOut])
async def list_indicators(repo: RepoDep) -> list[IndicatorOut]:
    docs = await repo.list_by_type(DocType.INDICATOR.value)
    out: list[IndicatorOut] = []
    for d in docs:
        try:
            d = _checked(d, IndicatorDoc)
            out.append(_indicator_to_out(d))
        except (ValueError, KeyError, TypeError, AttributeError, HTTPException) as exc:
            _log.warning(
                "skipping malformed indicator doc _id=%s: %s",
                getattr(d, "id", "?"),
                exc,
                exc_info=True,
            )
    return out


@router.get("/indicators/{doc_id}", response_model=IndicatorOut)
async def get_indicator(doc_id: DocId, repo: RepoDep) -> IndicatorOut:
    doc = _expect(
        await repo.get_by_id(DocType.INDICATOR.value, doc_id),
        DocType.INDICATOR.value,
        doc_id,
    )
    doc = _checked(doc, IndicatorDoc)
    return _indicator_to_out(doc)


@router.put("/indicators/{doc_id}", response_model=IndicatorOut)
async def update_indicator(
    doc_id: DocId, body: IndicatorUpdateIn, repo: RepoDep
) -> IndicatorOut:
    existing = _expect(
        await repo.get_by_id(DocType.INDICATOR.value, doc_id),
        DocType.INDICATOR.value,
        doc_id,
    )
    existing = _checked(existing, IndicatorDoc)
    updated = IndicatorDoc(
        id=doc_id,
        type=DocType.INDICATOR.value,
        name=body.name,
        definition=body.definition,
        created_at=existing.created_at,
        updated_at=existing.updated_at,  # repo bumps it
        deleted=body.deleted,
    )
    try:
        stored = await repo.update(updated, expected_updated_at=existing.updated_at)
    except KeyError as exc:
        # The earlier get_by_id succeeded but the doc was deleted in
        # the gap — surface a 404 rather than a 500.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        # Optimistic CAS: another writer touched the doc between our
        # read and our replace — refuse to clobber.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, IndicatorDoc)
    return _indicator_to_out(stored)


@router.delete("/indicators/{doc_id}", status_code=204)
async def archive_indicator(doc_id: DocId, repo: RepoDep) -> None:
    try:
        await repo.archive(DocType.INDICATOR.value, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Signal endpoints
# ---------------------------------------------------------------------------


@router.post("/signals", response_model=SignalOut, status_code=201)
async def create_signal(body: SignalCreateIn, repo: RepoDep) -> SignalOut:
    now = _now()
    doc = SignalDoc(
        id=body.id,
        type=DocType.SIGNAL.value,
        name=body.name,
        category=body.category,
        created_at=now,
        updated_at=now,
        inputs=tuple(body.inputs),
        rules=body.rules,
        settings=body.settings,
        description=body.description,
    )
    try:
        stored = await repo.create(doc)
    except pymongo.errors.DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail=f"signal with id={body.id!r} already exists"
        ) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, SignalDoc)
    return _signal_to_out(stored)


@router.get("/signals", response_model=list[SignalOut])
async def list_signals(
    repo: RepoDep, category: Category = Query(...)
) -> list[SignalOut]:
    docs = await repo.list_by_type_and_category(DocType.SIGNAL.value, category)
    out: list[SignalOut] = []
    for d in docs:
        try:
            d = _checked(d, SignalDoc)
            out.append(_signal_to_out(d))
        except (ValueError, KeyError, TypeError, AttributeError, HTTPException) as exc:
            _log.warning(
                "skipping malformed signal doc _id=%s: %s",
                getattr(d, "id", "?"),
                exc,
                exc_info=True,
            )
    return out


@router.get("/signals/{doc_id}", response_model=SignalOut)
async def get_signal(doc_id: DocId, repo: RepoDep) -> SignalOut:
    doc = _expect(
        await repo.get_by_id(DocType.SIGNAL.value, doc_id),
        DocType.SIGNAL.value,
        doc_id,
    )
    doc = _checked(doc, SignalDoc)
    return _signal_to_out(doc)


@router.put("/signals/{doc_id}", response_model=SignalOut)
async def update_signal(
    doc_id: DocId, body: SignalUpdateIn, repo: RepoDep
) -> SignalOut:
    existing = _expect(
        await repo.get_by_id(DocType.SIGNAL.value, doc_id),
        DocType.SIGNAL.value,
        doc_id,
    )
    existing = _checked(existing, SignalDoc)
    updated = SignalDoc(
        id=doc_id,
        type=DocType.SIGNAL.value,
        name=body.name,
        category=body.category,
        created_at=existing.created_at,
        updated_at=existing.updated_at,
        inputs=tuple(body.inputs),
        rules=body.rules,
        settings=body.settings,
        description=body.description,
    )
    try:
        stored = await repo.update(updated, expected_updated_at=existing.updated_at)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, SignalDoc)
    return _signal_to_out(stored)


@router.delete("/signals/{doc_id}", status_code=204)
async def archive_signal(doc_id: DocId, repo: RepoDep) -> None:
    try:
        await repo.archive(DocType.SIGNAL.value, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Portfolio endpoints
# ---------------------------------------------------------------------------


@router.post("/portfolios", response_model=PortfolioOut, status_code=201)
async def create_portfolio(body: PortfolioCreateIn, repo: RepoDep) -> PortfolioOut:
    now = _now()
    doc = PortfolioDoc(
        id=body.id,
        type=DocType.PORTFOLIO.value,
        name=body.name,
        category=body.category,
        created_at=now,
        updated_at=now,
        legs=tuple(body.legs),
        rebalance=body.rebalance,
    )
    try:
        stored = await repo.create(doc)
    except pymongo.errors.DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail=f"portfolio with id={body.id!r} already exists"
        ) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, PortfolioDoc)
    return _portfolio_to_out(stored)


@router.get("/portfolios", response_model=list[PortfolioOut])
async def list_portfolios(
    repo: RepoDep, category: Category = Query(...)
) -> list[PortfolioOut]:
    docs = await repo.list_by_type_and_category(DocType.PORTFOLIO.value, category)
    out: list[PortfolioOut] = []
    for d in docs:
        try:
            d = _checked(d, PortfolioDoc)
            out.append(_portfolio_to_out(d))
        except (ValueError, KeyError, TypeError, AttributeError, HTTPException) as exc:
            _log.warning(
                "skipping malformed portfolio doc _id=%s: %s",
                getattr(d, "id", "?"),
                exc,
                exc_info=True,
            )
    return out


@router.get("/portfolios/{doc_id}", response_model=PortfolioOut)
async def get_portfolio(doc_id: DocId, repo: RepoDep) -> PortfolioOut:
    doc = _expect(
        await repo.get_by_id(DocType.PORTFOLIO.value, doc_id),
        DocType.PORTFOLIO.value,
        doc_id,
    )
    doc = _checked(doc, PortfolioDoc)
    return _portfolio_to_out(doc)


@router.put("/portfolios/{doc_id}", response_model=PortfolioOut)
async def update_portfolio(
    doc_id: DocId, body: PortfolioUpdateIn, repo: RepoDep
) -> PortfolioOut:
    existing = _expect(
        await repo.get_by_id(DocType.PORTFOLIO.value, doc_id),
        DocType.PORTFOLIO.value,
        doc_id,
    )
    existing = _checked(existing, PortfolioDoc)
    updated = PortfolioDoc(
        id=doc_id,
        type=DocType.PORTFOLIO.value,
        name=body.name,
        category=body.category,
        created_at=existing.created_at,
        updated_at=existing.updated_at,
        legs=tuple(body.legs),
        rebalance=body.rebalance,
    )
    try:
        stored = await repo.update(updated, expected_updated_at=existing.updated_at)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, PortfolioDoc)
    return _portfolio_to_out(stored)


@router.delete("/portfolios/{doc_id}", status_code=204)
async def archive_portfolio(doc_id: DocId, repo: RepoDep) -> None:
    try:
        await repo.archive(DocType.PORTFOLIO.value, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Basket endpoints
# ---------------------------------------------------------------------------


@router.post("/baskets", response_model=BasketOut, status_code=201)
async def create_basket(body: BasketIn, repo: RepoDep) -> BasketOut:
    _check_basket_homogeneity(body.asset_class, body.legs)
    _check_basket_no_duplicates(body.legs)
    now = _now()
    raw_legs = _legs_to_mongo(body.legs)
    doc = BasketDoc(
        id=body.id,
        type=DocType.BASKET.value,
        name=body.name,
        category=body.category,
        asset_class=body.asset_class,
        created_at=now,
        updated_at=now,
        legs=raw_legs,
    )
    try:
        stored = await repo.create(doc)
    except pymongo.errors.DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail=f"basket with id={body.id!r} already exists"
        ) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, BasketDoc)
    return _basket_to_out(stored)


@router.get("/baskets", response_model=list[BasketOut])
async def list_baskets(
    repo: RepoDep, category: Category = Query(...)
) -> list[BasketOut]:
    docs = await repo.list_by_type_and_category(DocType.BASKET.value, category)
    out: list[BasketOut] = []
    for d in docs:
        try:
            d = _checked(d, BasketDoc)
            out.append(_basket_to_out(d))
        except (ValueError, KeyError, TypeError, AttributeError, HTTPException) as exc:
            _log.warning(
                "skipping malformed basket doc _id=%s: %s",
                getattr(d, "id", "?"),
                exc,
                exc_info=True,
            )
    return out


@router.get("/baskets/{doc_id}", response_model=BasketOut)
async def get_basket(doc_id: DocId, repo: RepoDep) -> BasketOut:
    doc = _expect(
        await repo.get_by_id(DocType.BASKET.value, doc_id),
        DocType.BASKET.value,
        doc_id,
    )
    doc = _checked(doc, BasketDoc)
    return _basket_to_out(doc)


@router.put("/baskets/{doc_id}", response_model=BasketOut)
async def update_basket(
    doc_id: DocId, body: BasketUpdateIn, repo: RepoDep
) -> BasketOut:
    _check_basket_homogeneity(body.asset_class, body.legs)
    _check_basket_no_duplicates(body.legs)
    existing = _expect(
        await repo.get_by_id(DocType.BASKET.value, doc_id),
        DocType.BASKET.value,
        doc_id,
    )
    existing = _checked(existing, BasketDoc)
    raw_legs = _legs_to_mongo(body.legs)
    updated = BasketDoc(
        id=doc_id,
        type=DocType.BASKET.value,
        name=body.name,
        category=body.category,
        asset_class=body.asset_class,
        created_at=existing.created_at,
        updated_at=existing.updated_at,  # repo bumps it
        legs=raw_legs,
    )
    try:
        stored = await repo.update(updated, expected_updated_at=existing.updated_at)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    stored = _checked(stored, BasketDoc)
    return _basket_to_out(stored)


@router.delete("/baskets/{doc_id}", status_code=204)
async def archive_basket(doc_id: DocId, repo: RepoDep) -> None:
    try:
        await repo.archive(DocType.BASKET.value, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
