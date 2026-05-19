"""Unit tests for the persistence HTTP router.

These run against a FastAPI ``TestClient`` with the ``WriteRepository``
dependency overridden by an in-memory fake. No Mongo is required.

Covers the PR-review fixes:

* **B1** — duplicate ``_id`` create returns 409, not 500.
* **B3** — oversized request body returns 413; ``DocumentTooLarge``
  from the repo returns 413.
* **M2** — Pydantic validation rejects bad ids, oversize descriptions,
  unknown rebalance values, deeply nested payloads (422 via the
  project's validation envelope, which maps to 400).
* **M4** — concurrent update returns 409 (CAS miss).
* **n19** — ``extra="forbid"`` regression.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pymongo.errors
import pytest
from fastapi.testclient import TestClient

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.app import create_app
from tcg.persistence.repository import (
    ConcurrentUpdateError,
    DocumentTooLargeError,
)
from tcg.types.persistence import (
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
)


# ---------------------------------------------------------------------------
# Fake repository
# ---------------------------------------------------------------------------


class _FakeRepo:
    """In-memory ``WriteRepository`` stand-in.

    Implements just the surface used by the router. Behaviour switches
    (raise ``DuplicateKeyError`` etc.) are toggled per-test via flags.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}
        self.raise_duplicate_on_create: bool = False
        self.raise_too_large_on_create: bool = False
        self.raise_too_large_on_update: bool = False
        self.raise_concurrent_on_update: bool = False

    async def create(self, doc: Any) -> Any:
        if self.raise_duplicate_on_create:
            raise pymongo.errors.DuplicateKeyError("duplicate _id")
        if self.raise_too_large_on_create:
            raise DocumentTooLargeError("doc exceeds 16 MB")
        key = (doc.type, doc.id)
        if key in self._store:
            raise pymongo.errors.DuplicateKeyError(f"duplicate {key}")
        self._store[key] = doc
        return doc

    async def get_by_id(self, doc_type: str, doc_id: str) -> Any:
        return self._store.get((doc_type, doc_id))

    async def list_by_type(self, doc_type: str) -> list:
        return [
            d
            for (t, _), d in self._store.items()
            if t == doc_type and not getattr(d, "deleted", False)
        ]

    async def list_by_type_and_category(
        self, doc_type: str, category: Category
    ) -> list:
        return [
            d
            for (t, _), d in self._store.items()
            if t == doc_type and getattr(d, "category", None) == category
        ]

    async def update(
        self, doc: Any, *, expected_updated_at: datetime | None = None
    ) -> Any:
        if self.raise_too_large_on_update:
            raise DocumentTooLargeError("doc exceeds 16 MB")
        if self.raise_concurrent_on_update:
            raise ConcurrentUpdateError("doc modified concurrently")
        key = (doc.type, doc.id)
        if key not in self._store:
            raise KeyError(f"no {doc.type} with id={doc.id!r}")
        self._store[key] = doc
        return doc

    async def archive(self, doc_type: str, doc_id: str) -> None:
        key = (doc_type, doc_id)
        if key not in self._store:
            raise KeyError(f"no {doc_type} with id={doc_id!r}")
        existing = self._store[key]
        # Crude soft-delete — sufficient for the router tests.
        from dataclasses import replace as _replace

        if doc_type == "indicator":
            self._store[key] = _replace(existing, deleted=True)
        else:
            self._store[key] = _replace(existing, category=Category.ARCHIVE)


@pytest.fixture
def fake_repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture
def client(fake_repo: _FakeRepo) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_write_repository] = lambda: fake_repo
    return TestClient(app)


def _now_dict() -> dict:
    return {}  # endpoints don't take timestamps


# ---------------------------------------------------------------------------
# B1 — DuplicateKeyError → 409
# ---------------------------------------------------------------------------


def test_create_indicator_duplicate_returns_409(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.raise_duplicate_on_create = True
    r = client.post(
        "/api/persistence/indicators",
        json={"id": "rsi-14", "name": "RSI 14", "definition": {"period": 14}},
    )
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]


def test_create_signal_duplicate_returns_409(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.raise_duplicate_on_create = True
    r = client.post(
        "/api/persistence/signals",
        json={"id": "sig-1", "name": "Sig 1", "category": "DEV"},
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_create_portfolio_duplicate_returns_409(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.raise_duplicate_on_create = True
    r = client.post(
        "/api/persistence/portfolios",
        json={"id": "ptf-1", "name": "60-40", "category": "RESEARCH"},
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_create_then_recreate_same_id_returns_409_via_fake(
    client: TestClient,
) -> None:
    """End-to-end through the fake: two successive POSTs with the same
    id surface as 409 from the in-memory duplicate detection (not from
    the ``raise_duplicate_on_create`` flag)."""
    r1 = client.post(
        "/api/persistence/signals",
        json={"id": "sig-dupe", "name": "Sig", "category": "DEV"},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/persistence/signals",
        json={"id": "sig-dupe", "name": "Sig", "category": "DEV"},
    )
    assert r2.status_code == 409, r2.text


# ---------------------------------------------------------------------------
# B3 — body-size middleware + DocumentTooLarge → 413
# ---------------------------------------------------------------------------


def test_oversized_body_returns_413(client: TestClient) -> None:
    """A request whose ``Content-Length`` header advertises > 4 MB is
    rejected at the middleware layer with 413."""
    # We fabricate the body — middleware uses Content-Length so we don't
    # actually need to send the full payload.
    big = b"x" * (5 * 1024 * 1024)  # 5 MB
    r = client.post(
        "/api/persistence/indicators",
        content=big,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["error_type"] == "request_too_large"


def test_repo_document_too_large_on_create_returns_413(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    fake_repo.raise_too_large_on_create = True
    r = client.post(
        "/api/persistence/indicators",
        json={"id": "i", "name": "n", "definition": {"k": "v"}},
    )
    assert r.status_code == 413, r.text


def test_repo_document_too_large_on_update_returns_413(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    # Seed a doc first.
    r0 = client.post(
        "/api/persistence/indicators",
        json={"id": "i", "name": "n", "definition": {"k": "v"}},
    )
    assert r0.status_code == 201
    fake_repo.raise_too_large_on_update = True
    r = client.put(
        "/api/persistence/indicators/i",
        json={"name": "n2", "definition": {"k": "v"}, "deleted": False},
    )
    assert r.status_code == 413, r.text


# ---------------------------------------------------------------------------
# M2 — Pydantic validation tightening
# ---------------------------------------------------------------------------


def _expect_validation(r) -> None:
    """The app re-maps Pydantic 422 to a 400 envelope; both shapes count
    as a validation rejection."""
    assert r.status_code in (400, 422), r.text


def test_create_indicator_bad_id_pattern_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/persistence/indicators",
        json={"id": "$bad", "name": "n", "definition": {}},
    )
    _expect_validation(r)


def test_create_signal_oversize_description_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/persistence/signals",
        json={
            "id": "sig-1",
            "name": "n",
            "category": "DEV",
            "description": "x" * 4097,
        },
    )
    _expect_validation(r)


def test_create_portfolio_bad_rebalance_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/persistence/portfolios",
        json={
            "id": "ptf-1",
            "name": "n",
            "category": "RESEARCH",
            "rebalance": "hourly",  # not a Literal member
        },
    )
    _expect_validation(r)


def test_create_signal_deeply_nested_rules_rejected(client: TestClient) -> None:
    """Build a 20-level deep nested dict — beyond the depth guard."""
    nested: Any = "leaf"
    for _ in range(20):
        nested = {"k": nested}
    r = client.post(
        "/api/persistence/signals",
        json={
            "id": "sig-1",
            "name": "n",
            "category": "DEV",
            "rules": nested,
        },
    )
    _expect_validation(r)


def test_create_signal_extra_field_rejected(client: TestClient) -> None:
    """Regression for ``extra='forbid'`` — unknown fields fail loud."""
    r = client.post(
        "/api/persistence/signals",
        json={
            "id": "sig-1",
            "name": "n",
            "category": "DEV",
            "totally_unknown_field": 1,
        },
    )
    _expect_validation(r)


def test_create_signal_with_long_id_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/persistence/signals",
        json={
            "id": "a" * 129,  # max_length=128
            "name": "n",
            "category": "DEV",
        },
    )
    _expect_validation(r)


def test_create_signal_with_dollar_prefixed_id_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/persistence/signals",
        json={"id": "$evil", "name": "n", "category": "DEV"},
    )
    _expect_validation(r)


# ---------------------------------------------------------------------------
# M4 — concurrent update → 409
# ---------------------------------------------------------------------------


def test_concurrent_update_returns_409(
    client: TestClient, fake_repo: _FakeRepo
) -> None:
    """PUT raises ``ConcurrentUpdateError`` → 409."""
    r0 = client.post(
        "/api/persistence/signals",
        json={"id": "s1", "name": "n", "category": "DEV"},
    )
    assert r0.status_code == 201
    fake_repo.raise_concurrent_on_update = True
    r = client.put(
        "/api/persistence/signals/s1",
        json={"name": "n2", "category": "DEV"},
    )
    assert r.status_code == 409, r.text
    assert "concurrent" in r.json()["detail"].lower()
