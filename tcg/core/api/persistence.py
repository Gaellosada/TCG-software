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
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.persistence import WriteRepository
from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PersistenceDoc,
    PortfolioDoc,
    SignalDoc,
)


router = APIRouter(prefix="/api/persistence", tags=["persistence"])


# ---------------------------------------------------------------------------
# Pydantic wire models
# ---------------------------------------------------------------------------


class _BaseWriteModel(BaseModel):
    """Common configuration for create/update payloads."""

    model_config = {"extra": "forbid"}


class IndicatorCreateIn(_BaseWriteModel):
    """Create-payload for an indicator. The server stamps timestamps."""

    id: str = Field(..., min_length=1, max_length=256)
    name: str = Field(..., min_length=1, max_length=512)
    definition: dict


class IndicatorUpdateIn(_BaseWriteModel):
    """Update-payload. ``id`` comes from the URL path; we re-supply the
    full document body (full-replace semantics — the repository does a
    ``replace_one``).
    """

    name: str = Field(..., min_length=1, max_length=512)
    definition: dict
    deleted: bool = False


class SignalCreateIn(_BaseWriteModel):
    id: str = Field(..., min_length=1, max_length=256)
    name: str = Field(..., min_length=1, max_length=512)
    blocks: list[dict]
    category: Category


class SignalUpdateIn(_BaseWriteModel):
    name: str = Field(..., min_length=1, max_length=512)
    blocks: list[dict]
    category: Category


class PortfolioCreateIn(_BaseWriteModel):
    id: str = Field(..., min_length=1, max_length=256)
    name: str = Field(..., min_length=1, max_length=512)
    instruments: list[dict]
    rebalance: dict
    category: Category


class PortfolioUpdateIn(_BaseWriteModel):
    name: str = Field(..., min_length=1, max_length=512)
    instruments: list[dict]
    rebalance: dict
    category: Category


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
    blocks: list[dict]
    category: Category
    created_at: datetime
    updated_at: datetime


class PortfolioOut(BaseModel):
    id: str
    type: str
    name: str
    instruments: list[dict]
    rebalance: dict
    category: Category
    created_at: datetime
    updated_at: datetime


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
        blocks=doc.blocks,
        category=doc.category,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _portfolio_to_out(doc: PortfolioDoc) -> PortfolioOut:
    return PortfolioOut(
        id=doc.id,
        type=doc.type,
        name=doc.name,
        instruments=doc.instruments,
        rebalance=doc.rebalance,
        category=doc.category,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
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
        type="indicator",
        name=body.name,
        definition=body.definition,
        created_at=now,
        updated_at=now,
    )
    stored = await repo.create(doc)
    assert isinstance(stored, IndicatorDoc)
    return _indicator_to_out(stored)


@router.get("/indicators", response_model=list[IndicatorOut])
async def list_indicators(repo: RepoDep) -> list[IndicatorOut]:
    docs = await repo.list_by_type("indicator")
    return [_indicator_to_out(d) for d in docs]


@router.get("/indicators/{doc_id}", response_model=IndicatorOut)
async def get_indicator(doc_id: str, repo: RepoDep) -> IndicatorOut:
    doc = _expect(await repo.get_by_id("indicator", doc_id), "indicator", doc_id)
    assert isinstance(doc, IndicatorDoc)
    return _indicator_to_out(doc)


@router.put("/indicators/{doc_id}", response_model=IndicatorOut)
async def update_indicator(
    doc_id: str, body: IndicatorUpdateIn, repo: RepoDep
) -> IndicatorOut:
    existing = _expect(
        await repo.get_by_id("indicator", doc_id), "indicator", doc_id
    )
    assert isinstance(existing, IndicatorDoc)
    updated = IndicatorDoc(
        id=doc_id,
        type="indicator",
        name=body.name,
        definition=body.definition,
        created_at=existing.created_at,
        updated_at=existing.updated_at,  # repo bumps it
        deleted=body.deleted,
    )
    try:
        stored = await repo.update(updated)
    except KeyError as exc:
        # The earlier get_by_id succeeded but the doc was deleted in
        # the gap — surface a 404 rather than a 500.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    assert isinstance(stored, IndicatorDoc)
    return _indicator_to_out(stored)


@router.delete("/indicators/{doc_id}", status_code=204)
async def archive_indicator(doc_id: str, repo: RepoDep) -> None:
    try:
        await repo.archive("indicator", doc_id)
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
        type="signal",
        name=body.name,
        blocks=body.blocks,
        category=body.category,
        created_at=now,
        updated_at=now,
    )
    stored = await repo.create(doc)
    assert isinstance(stored, SignalDoc)
    return _signal_to_out(stored)


@router.get("/signals", response_model=list[SignalOut])
async def list_signals(
    repo: RepoDep, category: Category = Query(...)
) -> list[SignalOut]:
    docs = await repo.list_by_type_and_category("signal", category)
    out: list[SignalOut] = []
    for d in docs:
        assert isinstance(d, SignalDoc)
        out.append(_signal_to_out(d))
    return out


@router.get("/signals/{doc_id}", response_model=SignalOut)
async def get_signal(doc_id: str, repo: RepoDep) -> SignalOut:
    doc = _expect(await repo.get_by_id("signal", doc_id), "signal", doc_id)
    assert isinstance(doc, SignalDoc)
    return _signal_to_out(doc)


@router.put("/signals/{doc_id}", response_model=SignalOut)
async def update_signal(
    doc_id: str, body: SignalUpdateIn, repo: RepoDep
) -> SignalOut:
    existing = _expect(await repo.get_by_id("signal", doc_id), "signal", doc_id)
    assert isinstance(existing, SignalDoc)
    updated = SignalDoc(
        id=doc_id,
        type="signal",
        name=body.name,
        blocks=body.blocks,
        category=body.category,
        created_at=existing.created_at,
        updated_at=existing.updated_at,
    )
    try:
        stored = await repo.update(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    assert isinstance(stored, SignalDoc)
    return _signal_to_out(stored)


@router.delete("/signals/{doc_id}", status_code=204)
async def archive_signal(doc_id: str, repo: RepoDep) -> None:
    try:
        await repo.archive("signal", doc_id)
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
        type="portfolio",
        name=body.name,
        instruments=body.instruments,
        rebalance=body.rebalance,
        category=body.category,
        created_at=now,
        updated_at=now,
    )
    stored = await repo.create(doc)
    assert isinstance(stored, PortfolioDoc)
    return _portfolio_to_out(stored)


@router.get("/portfolios", response_model=list[PortfolioOut])
async def list_portfolios(
    repo: RepoDep, category: Category = Query(...)
) -> list[PortfolioOut]:
    docs = await repo.list_by_type_and_category("portfolio", category)
    out: list[PortfolioOut] = []
    for d in docs:
        assert isinstance(d, PortfolioDoc)
        out.append(_portfolio_to_out(d))
    return out


@router.get("/portfolios/{doc_id}", response_model=PortfolioOut)
async def get_portfolio(doc_id: str, repo: RepoDep) -> PortfolioOut:
    doc = _expect(
        await repo.get_by_id("portfolio", doc_id), "portfolio", doc_id
    )
    assert isinstance(doc, PortfolioDoc)
    return _portfolio_to_out(doc)


@router.put("/portfolios/{doc_id}", response_model=PortfolioOut)
async def update_portfolio(
    doc_id: str, body: PortfolioUpdateIn, repo: RepoDep
) -> PortfolioOut:
    existing = _expect(
        await repo.get_by_id("portfolio", doc_id), "portfolio", doc_id
    )
    assert isinstance(existing, PortfolioDoc)
    updated = PortfolioDoc(
        id=doc_id,
        type="portfolio",
        name=body.name,
        instruments=body.instruments,
        rebalance=body.rebalance,
        category=body.category,
        created_at=existing.created_at,
        updated_at=existing.updated_at,
    )
    try:
        stored = await repo.update(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    assert isinstance(stored, PortfolioDoc)
    return _portfolio_to_out(stored)


@router.delete("/portfolios/{doc_id}", status_code=204)
async def archive_portfolio(doc_id: str, repo: RepoDep) -> None:
    try:
        await repo.archive("portfolio", doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
