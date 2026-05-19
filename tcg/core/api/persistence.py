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
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import pymongo.errors
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

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


def _max_depth(value: Any, _depth: int = 0) -> int:
    """Return the maximum nesting depth of ``value``.

    Walks dicts and lists/tuples recursively. Scalars count as depth
    ``_depth``. The maximum value returned is bounded only by the
    caller's recursion limit — callers MUST check it against
    ``_MAX_PAYLOAD_DEPTH`` and reject before deep recursion bites.
    """
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
            f"{field_name}: nesting depth {depth} exceeds limit "
            f"{_MAX_PAYLOAD_DEPTH}"
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
# Asset-class derivation for basket validation
# ---------------------------------------------------------------------------


def _asset_class_from_collection(collection: str) -> str | None:
    """Derive an asset-class bucket from a MongoDB collection name.

    Returns one of ``'future'``, ``'index'``, ``'equity'``, or ``None``
    when the collection is unknown or hosts options (options are not
    yet supported in baskets — Phase 1 scope).

    Mirrors the bucketing logic used elsewhere (``tcg.data._mongo``) but
    re-implemented here to avoid importing from the read-only data layer
    (Sign 2 guardrail).
    """
    if collection.startswith("FUT_"):
        return "future"
    if collection == "INDEX":
        return "index"
    if collection in ("ETF", "FUND", "FOREX"):
        return "equity"
    return None


def _check_basket_homogeneity(legs: list["BasketLegIn"]) -> None:
    """Reject baskets whose legs straddle multiple asset classes.

    Raises ``HTTPException(400)`` on:

    * any leg whose ``collection`` resolves to an unknown / unsupported
      asset class (including ``OPT_*`` option collections), or
    * a mixture of asset classes across legs.

    Empty baskets are permitted so that the frontend can save partial
    work (the create dialog ships an empty basket on first save).
    """
    if not legs:
        return

    buckets: dict[str, list[int]] = {}
    for i, leg in enumerate(legs):
        ac = _asset_class_from_collection(leg.collection)
        if ac is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"basket leg {i}: collection {leg.collection!r} has no "
                    f"supported asset class (unknown or options not yet "
                    f"supported in baskets)"
                ),
            )
        buckets.setdefault(ac, []).append(i)

    if len(buckets) > 1:
        parts = "; ".join(
            f"{ac}=legs{idxs}" for ac, idxs in sorted(buckets.items())
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"basket legs must share the same asset class — "
                f"got mixed classes: {parts}"
            ),
        )


def _check_basket_no_duplicates(legs: list["BasketLegIn"]) -> None:
    """Reject duplicate ``instrument_id`` within the same basket."""
    seen: set[str] = set()
    for i, leg in enumerate(legs):
        if leg.instrument_id in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"basket leg {i}: duplicate instrument_id "
                    f"{leg.instrument_id!r}"
                ),
            )
        seen.add(leg.instrument_id)


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


class IndicatorCreateIn(_BaseWriteModel):
    """Create-payload for an indicator. The server stamps timestamps."""

    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)
    name: str = Field(..., min_length=1, max_length=512)
    definition: dict

    @field_validator("definition")
    @classmethod
    def _check_definition(cls, v: dict) -> dict:
        return _validate_payload(v, "definition")


class IndicatorUpdateIn(_BaseWriteModel):
    """Update-payload. ``id`` comes from the URL path; we re-supply the
    full document body (full-replace semantics — the repository does a
    ``replace_one``).
    """

    name: str = Field(..., min_length=1, max_length=512)
    definition: dict
    deleted: bool = False

    @field_validator("definition")
    @classmethod
    def _check_definition(cls, v: dict) -> dict:
        return _validate_payload(v, "definition")


class SignalCreateIn(_BaseWriteModel):
    """Create-payload for a signal.

    The interesting editable content (``inputs`` / ``rules`` /
    ``settings`` / ``description``) is OPTIONAL — a freshly created
    signal can be empty. Defaults are empty list / dict / string.
    """

    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)
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


class SignalUpdateIn(_BaseWriteModel):
    """Update-payload — full replace. Same shape as create minus ``id``."""

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


class PortfolioCreateIn(_BaseWriteModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)
    name: str = Field(..., min_length=1, max_length=512)
    category: Category
    legs: list[dict] = Field(default_factory=list)
    rebalance: RebalanceLiteral = "none"

    @field_validator("legs")
    @classmethod
    def _check_legs(cls, v: list[dict]) -> list[dict]:
        return _validate_payload(v, "legs")


class PortfolioUpdateIn(_BaseWriteModel):
    name: str = Field(..., min_length=1, max_length=512)
    category: Category
    legs: list[dict] = Field(default_factory=list)
    rebalance: RebalanceLiteral = "none"

    @field_validator("legs")
    @classmethod
    def _check_legs(cls, v: list[dict]) -> list[dict]:
        return _validate_payload(v, "legs")


# ---------------------------------------------------------------------------
# Basket wire models
# ---------------------------------------------------------------------------


class BasketLegIn(BaseModel):
    """A single leg in a basket create/update payload.

    ``weight`` is a signed fraction: positive = long, negative = short.
    The API does NOT validate that weights sum to 1.0 — callers may save
    partial work. Zero weight is rejected (meaningless leg).
    ``collection`` identifies the MongoDB collection that hosts the
    instrument so asset class can be derived without a catalogue lookup.
    """

    model_config = {"extra": "forbid"}

    instrument_id: str = Field(..., min_length=1, max_length=256)
    collection: str = Field(..., min_length=1, max_length=128)
    weight: float = Field(...)

    @field_validator("weight")
    @classmethod
    def _check_weight_nonzero(cls, v: float) -> float:
        if v == 0.0:
            raise ValueError("weight must be non-zero")
        return v


class BasketIn(_BaseWriteModel):
    """Create-payload for a basket."""

    id: str = Field(..., min_length=1, max_length=128, pattern=_ID_PATTERN)
    name: str = Field(..., min_length=1, max_length=512)
    category: Category
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
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        legs=list(doc.legs),
    )


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
    assert isinstance(stored, IndicatorDoc)
    return _indicator_to_out(stored)


@router.get("/indicators", response_model=list[IndicatorOut])
async def list_indicators(repo: RepoDep) -> list[IndicatorOut]:
    docs = await repo.list_by_type(DocType.INDICATOR.value)
    return [_indicator_to_out(d) for d in docs]


@router.get("/indicators/{doc_id}", response_model=IndicatorOut)
async def get_indicator(doc_id: str, repo: RepoDep) -> IndicatorOut:
    doc = _expect(
        await repo.get_by_id(DocType.INDICATOR.value, doc_id),
        DocType.INDICATOR.value,
        doc_id,
    )
    assert isinstance(doc, IndicatorDoc)
    return _indicator_to_out(doc)


@router.put("/indicators/{doc_id}", response_model=IndicatorOut)
async def update_indicator(
    doc_id: str, body: IndicatorUpdateIn, repo: RepoDep
) -> IndicatorOut:
    existing = _expect(
        await repo.get_by_id(DocType.INDICATOR.value, doc_id),
        DocType.INDICATOR.value,
        doc_id,
    )
    assert isinstance(existing, IndicatorDoc)
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
        stored = await repo.update(
            updated, expected_updated_at=existing.updated_at
        )
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
    assert isinstance(stored, IndicatorDoc)
    return _indicator_to_out(stored)


@router.delete("/indicators/{doc_id}", status_code=204)
async def archive_indicator(doc_id: str, repo: RepoDep) -> None:
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
    assert isinstance(stored, SignalDoc)
    return _signal_to_out(stored)


@router.get("/signals", response_model=list[SignalOut])
async def list_signals(
    repo: RepoDep, category: Category = Query(...)
) -> list[SignalOut]:
    docs = await repo.list_by_type_and_category(DocType.SIGNAL.value, category)
    out: list[SignalOut] = []
    for d in docs:
        assert isinstance(d, SignalDoc)
        out.append(_signal_to_out(d))
    return out


@router.get("/signals/{doc_id}", response_model=SignalOut)
async def get_signal(doc_id: str, repo: RepoDep) -> SignalOut:
    doc = _expect(
        await repo.get_by_id(DocType.SIGNAL.value, doc_id),
        DocType.SIGNAL.value,
        doc_id,
    )
    assert isinstance(doc, SignalDoc)
    return _signal_to_out(doc)


@router.put("/signals/{doc_id}", response_model=SignalOut)
async def update_signal(
    doc_id: str, body: SignalUpdateIn, repo: RepoDep
) -> SignalOut:
    existing = _expect(
        await repo.get_by_id(DocType.SIGNAL.value, doc_id),
        DocType.SIGNAL.value,
        doc_id,
    )
    assert isinstance(existing, SignalDoc)
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
        stored = await repo.update(
            updated, expected_updated_at=existing.updated_at
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    assert isinstance(stored, SignalDoc)
    return _signal_to_out(stored)


@router.delete("/signals/{doc_id}", status_code=204)
async def archive_signal(doc_id: str, repo: RepoDep) -> None:
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
    assert isinstance(stored, PortfolioDoc)
    return _portfolio_to_out(stored)


@router.get("/portfolios", response_model=list[PortfolioOut])
async def list_portfolios(
    repo: RepoDep, category: Category = Query(...)
) -> list[PortfolioOut]:
    docs = await repo.list_by_type_and_category(DocType.PORTFOLIO.value, category)
    out: list[PortfolioOut] = []
    for d in docs:
        assert isinstance(d, PortfolioDoc)
        out.append(_portfolio_to_out(d))
    return out


@router.get("/portfolios/{doc_id}", response_model=PortfolioOut)
async def get_portfolio(doc_id: str, repo: RepoDep) -> PortfolioOut:
    doc = _expect(
        await repo.get_by_id(DocType.PORTFOLIO.value, doc_id),
        DocType.PORTFOLIO.value,
        doc_id,
    )
    assert isinstance(doc, PortfolioDoc)
    return _portfolio_to_out(doc)


@router.put("/portfolios/{doc_id}", response_model=PortfolioOut)
async def update_portfolio(
    doc_id: str, body: PortfolioUpdateIn, repo: RepoDep
) -> PortfolioOut:
    existing = _expect(
        await repo.get_by_id(DocType.PORTFOLIO.value, doc_id),
        DocType.PORTFOLIO.value,
        doc_id,
    )
    assert isinstance(existing, PortfolioDoc)
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
        stored = await repo.update(
            updated, expected_updated_at=existing.updated_at
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    assert isinstance(stored, PortfolioDoc)
    return _portfolio_to_out(stored)


@router.delete("/portfolios/{doc_id}", status_code=204)
async def archive_portfolio(doc_id: str, repo: RepoDep) -> None:
    try:
        await repo.archive(DocType.PORTFOLIO.value, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Basket endpoints
# ---------------------------------------------------------------------------


@router.post("/baskets", response_model=BasketOut, status_code=201)
async def create_basket(body: BasketIn, repo: RepoDep) -> BasketOut:
    _check_basket_no_duplicates(body.legs)
    _check_basket_homogeneity(body.legs)
    now = _now()
    raw_legs = tuple(
        {
            "instrument_id": leg.instrument_id,
            "collection": leg.collection,
            "weight": leg.weight,
        }
        for leg in body.legs
    )
    doc = BasketDoc(
        id=body.id,
        type=DocType.BASKET.value,
        name=body.name,
        category=body.category,
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
    assert isinstance(stored, BasketDoc)
    return _basket_to_out(stored)


@router.get("/baskets", response_model=list[BasketOut])
async def list_baskets(
    repo: RepoDep, category: Category = Query(...)
) -> list[BasketOut]:
    docs = await repo.list_by_type_and_category(DocType.BASKET.value, category)
    out: list[BasketOut] = []
    for d in docs:
        assert isinstance(d, BasketDoc)
        out.append(_basket_to_out(d))
    return out


@router.get("/baskets/{doc_id}", response_model=BasketOut)
async def get_basket(doc_id: str, repo: RepoDep) -> BasketOut:
    doc = _expect(
        await repo.get_by_id(DocType.BASKET.value, doc_id),
        DocType.BASKET.value,
        doc_id,
    )
    assert isinstance(doc, BasketDoc)
    return _basket_to_out(doc)


@router.put("/baskets/{doc_id}", response_model=BasketOut)
async def update_basket(
    doc_id: str, body: BasketUpdateIn, repo: RepoDep
) -> BasketOut:
    _check_basket_no_duplicates(body.legs)
    _check_basket_homogeneity(body.legs)
    existing = _expect(
        await repo.get_by_id(DocType.BASKET.value, doc_id),
        DocType.BASKET.value,
        doc_id,
    )
    assert isinstance(existing, BasketDoc)
    raw_legs = tuple(
        {
            "instrument_id": leg.instrument_id,
            "collection": leg.collection,
            "weight": leg.weight,
        }
        for leg in body.legs
    )
    updated = BasketDoc(
        id=doc_id,
        type=DocType.BASKET.value,
        name=body.name,
        category=body.category,
        created_at=existing.created_at,
        updated_at=existing.updated_at,  # repo bumps it
        legs=raw_legs,
    )
    try:
        stored = await repo.update(
            updated, expected_updated_at=existing.updated_at
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    assert isinstance(stored, BasketDoc)
    return _basket_to_out(stored)


@router.delete("/baskets/{doc_id}", status_code=204)
async def archive_basket(doc_id: str, repo: RepoDep) -> None:
    try:
        await repo.archive(DocType.BASKET.value, doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
