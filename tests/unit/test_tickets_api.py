"""Unit tests for the SELF-CONTAINED ticket HTTP endpoints.

Run against a FastAPI ``TestClient`` with the ``WriteRepository``
dependency overridden by an in-memory fake — no live PostgreSQL / table
required. Mirrors ``test_persistence_api.py``'s faking style.

Validation note (IMPORTANT)
---------------------------
The brief asked for "422 on violation". This app installs a global
``RequestValidationError`` handler (``tcg.core.app``) that maps EVERY
Pydantic body/query validation failure to HTTP **400** with the envelope
``{"error_type": "validation_error", "message": ...}`` so the frontend
reads a uniform ``body.message``. Every existing persistence endpoint
relies on this (see the 400 assertions in ``test_persistence_api.py``).
These tests therefore assert **400** for empty / whitespace / too-long
text — that is the real wire contract for the "422-class" Pydantic
violation in this codebase. The underlying constraint is still a
Pydantic ``Field`` / ``field_validator`` failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.app import create_app
from tcg.types.persistence import TicketDoc


# ---------------------------------------------------------------------------
# Fake repository (ticket surface only)
# ---------------------------------------------------------------------------


class _FakeTicketRepo:
    """In-memory stand-in for the ticket methods of ``WriteRepository``.

    Stores ``TicketDoc`` by id. ``created_at`` is stamped with a strictly
    increasing clock so the newest-first ordering is deterministic even
    when several tickets are created in the same wall-clock microsecond.
    ``delete``/``update`` raise ``KeyError`` on a miss, exactly like the
    real repo (the router maps that to 404).
    """

    def __init__(self) -> None:
        self._store: dict[str, TicketDoc] = {}
        self._seq = 0
        self._base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _next_created_at(self) -> datetime:
        self._seq += 1
        return self._base + timedelta(seconds=self._seq)

    async def create_ticket(self, text: str) -> TicketDoc:
        # Mirror the real server-side id generation (uuid4 hex). Use a
        # counter-suffixed deterministic id so assertions stay simple while
        # remaining unique + pattern-valid (hex/alnum).
        ticket_id = f"{self._seq + 1:032x}"
        doc = TicketDoc(id=ticket_id, text=text, created_at=self._next_created_at())
        self._store[ticket_id] = doc
        return doc

    async def list_tickets(self) -> list[TicketDoc]:
        return sorted(self._store.values(), key=lambda d: d.created_at, reverse=True)

    async def update_ticket(self, ticket_id: str, text: str) -> TicketDoc:
        existing = self._store.get(ticket_id)
        if existing is None:
            raise KeyError(f"no ticket with id={ticket_id!r}")
        # In-place text replacement; created_at preserved (no updated_at).
        updated = TicketDoc(id=ticket_id, text=text, created_at=existing.created_at)
        self._store[ticket_id] = updated
        return updated

    async def delete_ticket(self, ticket_id: str) -> None:
        if ticket_id not in self._store:
            raise KeyError(f"no ticket with id={ticket_id!r}")
        del self._store[ticket_id]  # HARD delete


@pytest.fixture
def fake_repo() -> _FakeTicketRepo:
    return _FakeTicketRepo()


@pytest.fixture
def client(fake_repo: _FakeTicketRepo) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_write_repository] = lambda: fake_repo
    return TestClient(app)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_ticket_returns_201_with_id_and_created_at(
    client: TestClient,
) -> None:
    r = client.post("/api/persistence/tickets", json={"text": "data feed gap"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["text"] == "data feed gap"
    assert body["id"], "server must generate a non-empty id"
    assert body["created_at"], "server must stamp created_at"
    # created_at must parse as an ISO-8601 timestamp.
    datetime.fromisoformat(body["created_at"])
    # extra='forbid' on the OUT model means exactly these three keys.
    assert set(body) == {"id", "text", "created_at"}


def test_create_ticket_trims_whitespace(client: TestClient) -> None:
    """Leading/trailing whitespace is stripped before storage."""
    r = client.post("/api/persistence/tickets", json={"text": "  trimmed  "})
    assert r.status_code == 201, r.text
    assert r.json()["text"] == "trimmed"


def test_create_ticket_empty_text_returns_400(client: TestClient) -> None:
    """Empty string fails ``min_length=1`` (Pydantic 422 → project 400)."""
    r = client.post("/api/persistence/tickets", json={"text": ""})
    assert r.status_code == 400, r.text


def test_create_ticket_whitespace_only_text_returns_400(client: TestClient) -> None:
    """Whitespace-only passes min_length but fails the strip validator."""
    r = client.post("/api/persistence/tickets", json={"text": "    "})
    assert r.status_code == 400, r.text


def test_create_ticket_too_long_text_returns_400(client: TestClient) -> None:
    """> 10000 chars fails ``max_length`` (Pydantic 422 → project 400)."""
    r = client.post("/api/persistence/tickets", json={"text": "x" * 10_001})
    assert r.status_code == 400, r.text


def test_create_ticket_exactly_max_length_ok(client: TestClient) -> None:
    """Exactly 10000 chars is accepted (boundary)."""
    r = client.post("/api/persistence/tickets", json={"text": "x" * 10_000})
    assert r.status_code == 201, r.text


def test_create_ticket_extra_field_returns_400(client: TestClient) -> None:
    """``extra='forbid'`` rejects any field beyond ``text``."""
    r = client.post(
        "/api/persistence/tickets", json={"text": "ok", "id": "client-supplied"}
    )
    assert r.status_code == 400, r.text


def test_create_ticket_missing_text_returns_400(client: TestClient) -> None:
    r = client.post("/api/persistence/tickets", json={})
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# List (newest-first)
# ---------------------------------------------------------------------------


def test_list_tickets_empty(client: TestClient) -> None:
    r = client.get("/api/persistence/tickets")
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_list_tickets_newest_first(client: TestClient) -> None:
    """List is ordered created_at DESC — most recently created first."""
    for text in ("first", "second", "third"):
        assert (
            client.post("/api/persistence/tickets", json={"text": text}).status_code
            == 201
        )
    r = client.get("/api/persistence/tickets")
    assert r.status_code == 200, r.text
    texts = [t["text"] for t in r.json()]
    assert texts == ["third", "second", "first"], texts


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_ticket_returns_200_with_new_text(client: TestClient) -> None:
    created = client.post("/api/persistence/tickets", json={"text": "before"}).json()
    r = client.put(f"/api/persistence/tickets/{created['id']}", json={"text": "after"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "after"
    assert body["id"] == created["id"]
    # created_at is preserved across an edit (no updated_at).
    assert body["created_at"] == created["created_at"]


def test_update_ticket_missing_returns_404(client: TestClient) -> None:
    r = client.put("/api/persistence/tickets/does-not-exist", json={"text": "x"})
    assert r.status_code == 404, r.text


def test_update_ticket_empty_text_returns_400(client: TestClient) -> None:
    created = client.post("/api/persistence/tickets", json={"text": "before"}).json()
    r = client.put(f"/api/persistence/tickets/{created['id']}", json={"text": "   "})
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Delete (HARD)
# ---------------------------------------------------------------------------


def test_delete_ticket_returns_204(client: TestClient) -> None:
    created = client.post("/api/persistence/tickets", json={"text": "to delete"}).json()
    r = client.delete(f"/api/persistence/tickets/{created['id']}")
    assert r.status_code == 204, r.text
    # Gone from the list (hard delete — not soft-hidden, but the list
    # contract is the same observable: absent).
    listing = client.get("/api/persistence/tickets").json()
    assert all(t["id"] != created["id"] for t in listing)


def test_delete_ticket_missing_returns_404(client: TestClient) -> None:
    r = client.delete("/api/persistence/tickets/never-existed")
    assert r.status_code == 404, r.text


def test_delete_ticket_twice_second_is_404(client: TestClient) -> None:
    """HARD delete: the row is physically gone, so a second delete 404s."""
    created = client.post("/api/persistence/tickets", json={"text": "once"}).json()
    assert client.delete(f"/api/persistence/tickets/{created['id']}").status_code == 204
    assert client.delete(f"/api/persistence/tickets/{created['id']}").status_code == 404
