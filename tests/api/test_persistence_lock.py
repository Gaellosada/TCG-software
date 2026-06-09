"""HTTP-layer tests for the persistence write-lock feature.

A locked Indicator / Signal / Portfolio (NOT Basket) cannot be updated,
recategorized, or archived/deleted — those mutations return **HTTP 423
(Locked)**. The doc can still be read, listed, and used. The dedicated
``PUT .../{id}/lock`` endpoint is the only way to flip the flag and it
bypasses the guard (so a locked doc can be unlocked).

These run against a FastAPI ``TestClient`` with the ``WriteRepository``
dependency overridden by an in-memory fake — no Mongo required, matching
``tests/unit/test_persistence_api.py``.

The fake repository below FAITHFULLY mirrors the real repository's lock
guard (``tcg.persistence.repository.WriteRepository``):

* ``update`` / ``archive`` read the **stored** doc's ``locked`` flag and
  raise :class:`LockedError` BEFORE mutating — they never trust the
  incoming payload's ``locked`` value. This is the critical behaviour:
  a client cannot escape the lock by POSTing ``locked: false``.
* ``set_locked`` flips ONLY ``locked`` and bypasses the guard.

Mocked dependencies: the in-memory ``_LockFakeRepo`` stands in for the
Motor-backed ``WriteRepository``. It is not a network mock — it is a
behavioural replica of the lock-guard contract, so the 423 mapping and
the bypass semantics are exercised through the real HTTP router.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.app import create_app
from tcg.persistence.repository import LockedError
from tcg.types.persistence import Category


# ---------------------------------------------------------------------------
# Fake repository that replicates the real lock guard
# ---------------------------------------------------------------------------


class _LockFakeRepo:
    """In-memory ``WriteRepository`` stand-in with a real lock guard.

    The guard reads the STORED doc's ``locked`` flag (never the incoming
    payload) and raises :class:`LockedError` from ``update`` / ``archive``
    before any mutation. ``set_locked`` is the only bypass.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    async def create(self, doc: Any) -> Any:
        key = (doc.type, doc.id)
        if key in self._store:
            import pymongo.errors

            raise pymongo.errors.DuplicateKeyError(f"duplicate {key}")
        # Mirror the server-stamping contract: store as-is (the dataclass
        # already carries locked=False from its default on create).
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

    def _raise_if_locked(self, doc_type: str, doc_id: str) -> None:
        """Replicate the real guard: read STORED locked, raise if set."""
        stored = self._store.get((doc_type, doc_id))
        if stored is not None and getattr(stored, "locked", False):
            raise LockedError(f"persistence: {doc_type} id={doc_id!r} is locked")

    async def update(
        self, doc: Any, *, expected_updated_at: datetime | None = None
    ) -> Any:
        # Guard reads STORED state, NOT the incoming doc.locked.
        self._raise_if_locked(doc.type, doc.id)
        key = (doc.type, doc.id)
        if key not in self._store:
            raise KeyError(f"no {doc.type} with id={doc.id!r}")
        self._store[key] = doc
        return doc

    async def archive(self, doc_type: str, doc_id: str) -> None:
        self._raise_if_locked(doc_type, doc_id)
        key = (doc_type, doc_id)
        if key not in self._store:
            raise KeyError(f"no {doc_type} with id={doc_id!r}")
        existing = self._store[key]
        if doc_type == "indicator":
            self._store[key] = _replace(existing, deleted=True)
        else:
            self._store[key] = _replace(existing, category=Category.ARCHIVE)

    async def set_locked(self, doc_type: str, doc_id: str, locked: bool) -> Any:
        """Bypass the guard; flip ONLY ``locked`` and return the doc."""
        if doc_type not in ("indicator", "signal", "portfolio"):
            raise ValueError(f"set_locked unsupported for {doc_type!r}")
        key = (doc_type, doc_id)
        if key not in self._store:
            raise KeyError(f"no {doc_type} with id={doc_id!r} to set lock")
        existing = self._store[key]
        updated = _replace(existing, locked=bool(locked))
        self._store[key] = updated
        return updated


@pytest.fixture
def fake_repo() -> _LockFakeRepo:
    return _LockFakeRepo()


@pytest.fixture
def client(fake_repo: _LockFakeRepo) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_write_repository] = lambda: fake_repo
    return TestClient(app)


# ---------------------------------------------------------------------------
# Seed helpers — create one of each entity via the real HTTP endpoints
# ---------------------------------------------------------------------------


def _create_indicator(client: TestClient, doc_id: str = "ind-1") -> None:
    r = client.post(
        "/api/persistence/indicators",
        json={"id": doc_id, "name": "RSI", "definition": {"period": 14}},
    )
    assert r.status_code == 201, r.text
    # Freshly created docs are unlocked.
    assert r.json()["locked"] is False


def _create_signal(
    client: TestClient, doc_id: str = "sig-1", category: str = "DEV"
) -> None:
    r = client.post(
        "/api/persistence/signals",
        json={"id": doc_id, "name": "Sig", "category": category},
    )
    assert r.status_code == 201, r.text
    assert r.json()["locked"] is False


def _create_portfolio(
    client: TestClient, doc_id: str = "ptf-1", category: str = "RESEARCH"
) -> None:
    r = client.post(
        "/api/persistence/portfolios",
        json={"id": doc_id, "name": "60-40", "category": category},
    )
    assert r.status_code == 201, r.text
    assert r.json()["locked"] is False


# ---------------------------------------------------------------------------
# 1. lock then unlock flips the flag, and *Out.locked reflects it
# ---------------------------------------------------------------------------


def test_lock_then_unlock_flips_flag_indicator(client: TestClient) -> None:
    _create_indicator(client)
    r = client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    assert r.status_code == 200, r.text
    assert r.json()["locked"] is True
    # GET reflects locked state.
    g = client.get("/api/persistence/indicators/ind-1")
    assert g.json()["locked"] is True
    # Unlock.
    r2 = client.put("/api/persistence/indicators/ind-1/lock", json={"locked": False})
    assert r2.status_code == 200, r2.text
    assert r2.json()["locked"] is False


def test_lock_then_unlock_flips_flag_signal(client: TestClient) -> None:
    _create_signal(client)
    r = client.put("/api/persistence/signals/sig-1/lock", json={"locked": True})
    assert r.status_code == 200, r.text
    assert r.json()["locked"] is True
    r2 = client.put("/api/persistence/signals/sig-1/lock", json={"locked": False})
    assert r2.status_code == 200
    assert r2.json()["locked"] is False


def test_lock_then_unlock_flips_flag_portfolio(client: TestClient) -> None:
    _create_portfolio(client)
    r = client.put("/api/persistence/portfolios/ptf-1/lock", json={"locked": True})
    assert r.status_code == 200, r.text
    assert r.json()["locked"] is True
    r2 = client.put("/api/persistence/portfolios/ptf-1/lock", json={"locked": False})
    assert r2.status_code == 200
    assert r2.json()["locked"] is False


# ---------------------------------------------------------------------------
# 2. PUT update on a locked doc → 423
# ---------------------------------------------------------------------------


def test_update_locked_indicator_returns_423(client: TestClient) -> None:
    _create_indicator(client)
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    r = client.put(
        "/api/persistence/indicators/ind-1",
        json={"name": "RSI-2", "definition": {"period": 21}, "deleted": False},
    )
    assert r.status_code == 423, r.text
    assert "locked" in r.json()["detail"].lower()


def test_update_locked_signal_returns_423(client: TestClient) -> None:
    _create_signal(client)
    client.put("/api/persistence/signals/sig-1/lock", json={"locked": True})
    r = client.put(
        "/api/persistence/signals/sig-1",
        json={"name": "Sig-2", "category": "DEV"},
    )
    assert r.status_code == 423, r.text


def test_update_locked_portfolio_returns_423(client: TestClient) -> None:
    _create_portfolio(client)
    client.put("/api/persistence/portfolios/ptf-1/lock", json={"locked": True})
    r = client.put(
        "/api/persistence/portfolios/ptf-1",
        json={"name": "70-30", "category": "RESEARCH"},
    )
    assert r.status_code == 423, r.text


# ---------------------------------------------------------------------------
# 2b. CRITICAL: a client cannot escape the lock by sending locked:false
#     in the update body — the guard reads STORED state.
#     (Indicator update has a ``deleted`` field; signal/portfolio update
#     bodies have no ``locked`` field at all — extra="forbid" would 422
#     it — so the escape attempt is structurally impossible there. The
#     indicator path is the meaningful surface to assert the guard reads
#     stored state regardless of the payload.)
# ---------------------------------------------------------------------------


def test_update_does_not_trust_payload_to_escape_lock(client: TestClient) -> None:
    _create_indicator(client)
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    # The indicator update body cannot carry ``locked`` (extra='forbid'),
    # so we cannot even *try* to smuggle locked:false. Confirm that and
    # that the stored-state guard still fires.
    r_smuggle = client.put(
        "/api/persistence/indicators/ind-1",
        json={
            "name": "x",
            "definition": {},
            "deleted": False,
            "locked": False,  # not an accepted field — must be rejected
        },
    )
    assert r_smuggle.status_code in (400, 422), r_smuggle.text
    # And a well-formed update still hits the stored-state lock guard.
    r = client.put(
        "/api/persistence/indicators/ind-1",
        json={"name": "x", "definition": {}, "deleted": False},
    )
    assert r.status_code == 423, r.text


# ---------------------------------------------------------------------------
# 3. DELETE / archive on a locked doc → 423
# ---------------------------------------------------------------------------


def test_archive_locked_indicator_returns_423(client: TestClient) -> None:
    _create_indicator(client)
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    r = client.delete("/api/persistence/indicators/ind-1")
    assert r.status_code == 423, r.text


def test_archive_locked_signal_returns_423(client: TestClient) -> None:
    _create_signal(client)
    client.put("/api/persistence/signals/sig-1/lock", json={"locked": True})
    r = client.delete("/api/persistence/signals/sig-1")
    assert r.status_code == 423, r.text


def test_archive_locked_portfolio_returns_423(client: TestClient) -> None:
    _create_portfolio(client)
    client.put("/api/persistence/portfolios/ptf-1/lock", json={"locked": True})
    r = client.delete("/api/persistence/portfolios/ptf-1")
    assert r.status_code == 423, r.text


# ---------------------------------------------------------------------------
# 4. Category change on a locked doc (signal & portfolio) → 423
#    Category change flows through PUT update, so it's covered by the
#    update guard — assert specifically with a *different* category.
# ---------------------------------------------------------------------------


def test_recategorize_locked_signal_returns_423(client: TestClient) -> None:
    _create_signal(client, category="DEV")
    client.put("/api/persistence/signals/sig-1/lock", json={"locked": True})
    # Attempt to move DEV -> PROD on a locked signal.
    r = client.put(
        "/api/persistence/signals/sig-1",
        json={"name": "Sig", "category": "PROD"},
    )
    assert r.status_code == 423, r.text


def test_recategorize_locked_portfolio_returns_423(client: TestClient) -> None:
    _create_portfolio(client, category="RESEARCH")
    client.put("/api/persistence/portfolios/ptf-1/lock", json={"locked": True})
    r = client.put(
        "/api/persistence/portfolios/ptf-1",
        json={"name": "60-40", "category": "PROD"},
    )
    assert r.status_code == 423, r.text


# ---------------------------------------------------------------------------
# 5. After unlock, update succeeds (and recategorization succeeds)
# ---------------------------------------------------------------------------


def test_update_succeeds_after_unlock_indicator(client: TestClient) -> None:
    _create_indicator(client)
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    assert (
        client.put(
            "/api/persistence/indicators/ind-1",
            json={"name": "x", "definition": {}, "deleted": False},
        ).status_code
        == 423
    )
    # Unlock, then the same update succeeds.
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": False})
    r = client.put(
        "/api/persistence/indicators/ind-1",
        json={"name": "RSI-new", "definition": {"period": 9}, "deleted": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "RSI-new"
    assert r.json()["locked"] is False


def test_recategorize_succeeds_after_unlock_signal(client: TestClient) -> None:
    _create_signal(client, category="DEV")
    client.put("/api/persistence/signals/sig-1/lock", json={"locked": True})
    client.put("/api/persistence/signals/sig-1/lock", json={"locked": False})
    r = client.put(
        "/api/persistence/signals/sig-1",
        json={"name": "Sig", "category": "PROD"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "PROD"
    assert r.json()["locked"] is False


# ---------------------------------------------------------------------------
# 6. GET and LIST still return a locked doc (read/use is unaffected)
# ---------------------------------------------------------------------------


def test_get_and_list_return_locked_signal(client: TestClient) -> None:
    _create_signal(client, category="DEV")
    client.put("/api/persistence/signals/sig-1/lock", json={"locked": True})
    # GET works and shows locked.
    g = client.get("/api/persistence/signals/sig-1")
    assert g.status_code == 200, g.text
    assert g.json()["locked"] is True
    # LIST includes the locked doc.
    lst = client.get("/api/persistence/signals?category=DEV")
    assert lst.status_code == 200, lst.text
    ids = [d["id"] for d in lst.json()]
    assert "sig-1" in ids
    locked_flags = {d["id"]: d["locked"] for d in lst.json()}
    assert locked_flags["sig-1"] is True


def test_list_indicators_includes_locked(client: TestClient) -> None:
    _create_indicator(client)
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    lst = client.get("/api/persistence/indicators")
    assert lst.status_code == 200, lst.text
    flags = {d["id"]: d["locked"] for d in lst.json()}
    assert flags.get("ind-1") is True


# ---------------------------------------------------------------------------
# 7. Indicator (no category): lock blocks both update and archive
#    (covered above individually; this asserts both in one flow on a
#    single locked indicator to match the brief's explicit item.)
# ---------------------------------------------------------------------------


def test_locked_indicator_blocks_update_and_archive(client: TestClient) -> None:
    _create_indicator(client)
    client.put("/api/persistence/indicators/ind-1/lock", json={"locked": True})
    upd = client.put(
        "/api/persistence/indicators/ind-1",
        json={"name": "x", "definition": {}, "deleted": False},
    )
    assert upd.status_code == 423, upd.text
    arc = client.delete("/api/persistence/indicators/ind-1")
    assert arc.status_code == 423, arc.text


# ---------------------------------------------------------------------------
# 8. /lock on a missing id → 404
# ---------------------------------------------------------------------------


def test_lock_missing_indicator_returns_404(client: TestClient) -> None:
    r = client.put(
        "/api/persistence/indicators/does-not-exist/lock", json={"locked": True}
    )
    assert r.status_code == 404, r.text


def test_lock_missing_signal_returns_404(client: TestClient) -> None:
    r = client.put("/api/persistence/signals/nope/lock", json={"locked": True})
    assert r.status_code == 404, r.text


def test_lock_missing_portfolio_returns_404(client: TestClient) -> None:
    r = client.put("/api/persistence/portfolios/nope/lock", json={"locked": False})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 9. Lock endpoint mutates ONLY ``locked`` (does not touch other fields)
#    and rejects extra fields in the body (extra='forbid').
# ---------------------------------------------------------------------------


def test_lock_endpoint_preserves_other_fields(client: TestClient) -> None:
    client.post(
        "/api/persistence/signals",
        json={
            "id": "sig-keep",
            "name": "Keep Me",
            "category": "DEV",
            "description": "important",
            "rules": {"a": 1},
        },
    )
    r = client.put("/api/persistence/signals/sig-keep/lock", json={"locked": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] is True
    # Other fields untouched by the lock op.
    assert body["name"] == "Keep Me"
    assert body["category"] == "DEV"
    assert body["description"] == "important"
    assert body["rules"] == {"a": 1}


def test_lock_endpoint_rejects_extra_field(client: TestClient) -> None:
    _create_signal(client)
    r = client.put(
        "/api/persistence/signals/sig-1/lock",
        json={"locked": True, "category": "PROD"},  # category not allowed here
    )
    assert r.status_code in (400, 422), r.text


def test_lock_endpoint_requires_locked_field(client: TestClient) -> None:
    _create_signal(client)
    r = client.put("/api/persistence/signals/sig-1/lock", json={})
    assert r.status_code in (400, 422), r.text


# ---------------------------------------------------------------------------
# 10. Baskets are intentionally NOT lockable — no /lock route exists.
#     (Guardrail Sign 1: do NOT add lock to Baskets.)
# ---------------------------------------------------------------------------


def test_no_basket_lock_route(client: TestClient) -> None:
    client.post(
        "/api/persistence/baskets",
        json={
            "id": "bkt-1",
            "name": "B",
            "category": "RESEARCH",
            "asset_class": "equity",
            "legs": [],
        },
    )
    r = client.put("/api/persistence/baskets/bkt-1/lock", json={"locked": True})
    # No such route — FastAPI returns 404 (route not found) or 405.
    assert r.status_code in (404, 405), r.text


# ---------------------------------------------------------------------------
# 11. Out-model carries ``locked`` for a freshly-created (unlocked) doc.
#     Regression: the *Out response models must expose the flag so the
#     frontend can render lock state.
# ---------------------------------------------------------------------------


def test_out_models_expose_locked_field() -> None:
    """The three *Out models must declare ``locked``."""
    from tcg.core.api.persistence import IndicatorOut, PortfolioOut, SignalOut

    assert "locked" in IndicatorOut.model_fields
    assert "locked" in SignalOut.model_fields
    assert "locked" in PortfolioOut.model_fields
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Construct each with locked=True to confirm it is a real field.
    IndicatorOut(
        id="i",
        type="indicator",
        name="n",
        definition={},
        created_at=now,
        updated_at=now,
        deleted=False,
        locked=True,
    )
    SignalOut(
        id="s",
        type="signal",
        name="n",
        category=Category.DEV,
        created_at=now,
        updated_at=now,
        inputs=[],
        rules={},
        settings={},
        description="",
        locked=True,
    )
    PortfolioOut(
        id="p",
        type="portfolio",
        name="n",
        category=Category.RESEARCH,
        created_at=now,
        updated_at=now,
        legs=[],
        rebalance="none",
        locked=True,
    )


# ---------------------------------------------------------------------------
# 12. ``locked`` round-trips through the Mongo serializer (contract #1), and
#     a stored doc that PREDATES the field deserializes to locked=False
#     (forward-compatibility — existing production docs have no ``locked``).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locked_value", [True, False])
def test_locked_round_trips_through_mongo_serializer(locked_value: bool) -> None:
    from tcg.types.persistence import (
        IndicatorDoc,
        PortfolioDoc,
        SignalDoc,
        from_mongo_dict,
        to_mongo_dict,
    )

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    docs = [
        IndicatorDoc(
            id="i",
            type="indicator",
            name="n",
            definition={"period": 14},
            created_at=now,
            updated_at=now,
            locked=locked_value,
        ),
        SignalDoc(
            id="s",
            type="signal",
            name="n",
            category=Category.DEV,
            created_at=now,
            updated_at=now,
            locked=locked_value,
        ),
        PortfolioDoc(
            id="p",
            type="portfolio",
            name="n",
            category=Category.RESEARCH,
            created_at=now,
            updated_at=now,
            locked=locked_value,
        ),
    ]
    for doc in docs:
        as_mongo = to_mongo_dict(doc)
        assert as_mongo["locked"] is locked_value
        restored = from_mongo_dict(as_mongo)
        assert restored == doc
        assert restored.locked is locked_value


def test_legacy_doc_without_locked_field_defaults_to_false() -> None:
    """A stored doc predating the ``locked`` field deserializes with
    locked=False (forward-compat for existing production data)."""
    from tcg.types.persistence import from_mongo_dict

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy_raw = {
        "_id": "legacy-sig",
        "type": "signal",
        "name": "Legacy",
        "category": "DEV",
        "created_at": now,
        "updated_at": now,
        "inputs": [],
        "rules": {},
        "settings": {},
        "description": "",
        # NOTE: no ``locked`` key — simulates a pre-feature document.
    }
    restored = from_mongo_dict(legacy_raw)
    assert restored.locked is False


# ---------------------------------------------------------------------------
# R2 — archive TOCTOU: the REAL WriteRepository.archive must fold
# ``locked: {$ne: True}`` into its write filter so a /lock that lands in
# the window between the pre-read and the write cannot archive a doc that
# just locked. On a zero-match it disambiguates not-found (KeyError → 404)
# from just-locked (LockedError → 423).
#
# The fake-repo above is a behavioural replica and never runs the real
# archive code, so these tests drive the REAL repository against a tiny
# fake async Mongo collection. We bind it through ``object.__setattr__``
# (the only legitimate way past the repo's immutability guard).
# ---------------------------------------------------------------------------


class _UpdateResult:
    def __init__(self, matched: int) -> None:
        self.matched_count = matched


def _matches(doc: dict, filt: dict) -> bool:
    """Minimal Mongo-filter matcher: equality + the single ``$ne`` we use."""
    for key, cond in filt.items():
        actual = doc.get(key)
        if isinstance(cond, dict) and "$ne" in cond:
            if actual == cond["$ne"]:
                return False
        elif actual != cond:
            return False
    return True


class _RaceColl:
    """Fake async collection that simulates a concurrent /lock landing in
    the archive TOCTOU window.

    The stored doc starts unlocked. The FIRST ``find_one`` (the archive
    pre-read in ``_raise_if_locked``) observes it unlocked AND flips it to
    locked — modelling a ``set_locked(True)`` that commits right after the
    pre-read returns. By the time ``update_one`` runs, the doc is locked,
    so the ``locked: {$ne: True}`` write filter excludes it (matched 0).
    The disambiguation re-read then sees it locked → LockedError.
    """

    def __init__(self, doc: dict | None) -> None:
        self._doc = doc
        self._find_one_calls = 0
        self.update_one_calls = 0

    async def find_one(self, filt: dict, projection: dict | None = None) -> dict | None:
        self._find_one_calls += 1
        doc = self._doc
        if doc is None or not _matches(doc, filt):
            return None
        snapshot = dict(doc)
        # Race injection: the concurrent lock commits right after this
        # first (pre-read) observation returns the still-unlocked state.
        if self._find_one_calls == 1:
            self._doc = {**doc, "locked": True}
        return snapshot

    async def update_one(self, filt: dict, update: dict) -> _UpdateResult:
        self.update_one_calls += 1
        if self._doc is not None and _matches(self._doc, filt):
            self._doc = {**self._doc, **update.get("$set", {})}
            return _UpdateResult(1)
        return _UpdateResult(0)


class _SimpleColl:
    """Fake async collection with NO race — faithful equality/``$ne``
    matching against a single stored doc (or none)."""

    def __init__(self, doc: dict | None) -> None:
        self._doc = doc
        self.update_one_calls = 0

    async def find_one(self, filt: dict, projection: dict | None = None) -> dict | None:
        if self._doc is not None and _matches(self._doc, filt):
            return dict(self._doc)
        return None

    async def update_one(self, filt: dict, update: dict) -> _UpdateResult:
        self.update_one_calls += 1
        if self._doc is not None and _matches(self._doc, filt):
            self._doc = {**self._doc, **update.get("$set", {})}
            return _UpdateResult(1)
        return _UpdateResult(0)

    @property
    def doc(self) -> dict | None:
        return self._doc


def _repo_with_coll(coll: object):
    """Build a real WriteRepository bound to ``coll`` (bypassing the
    immutability guard via ``object.__setattr__``, exactly as __init__
    does for the single legitimate construction write)."""
    from tcg.persistence.repository import WriteRepository

    repo = WriteRepository.__new__(WriteRepository)
    object.__setattr__(repo, "_coll", coll)
    return repo


@pytest.mark.asyncio
async def test_archive_loses_toctou_race_raises_locked_not_archived() -> None:
    """A /lock landing in the archive race window makes the write match
    zero docs; the repo raises LockedError (423) — it does NOT archive a
    locked doc, and the stored category stays unchanged."""
    from tcg.persistence.repository import LockedError

    coll = _RaceColl(
        {"_id": "sig-1", "type": "signal", "category": "DEV", "locked": False}
    )
    repo = _repo_with_coll(coll)
    with pytest.raises(LockedError):
        await repo.archive("signal", "sig-1")
    # The write was issued (proving we reached the filtered update) but it
    # matched nothing, so the doc was NOT recategorized to ARCHIVE.
    assert coll.update_one_calls == 1
    assert coll._doc is not None and coll._doc["category"] == "DEV"


@pytest.mark.asyncio
async def test_archive_missing_doc_still_raises_keyerror() -> None:
    """No doc at all → the filtered write matches zero AND the
    disambiguation re-read finds nothing → KeyError (404), unchanged."""
    coll = _SimpleColl(None)
    repo = _repo_with_coll(coll)
    with pytest.raises(KeyError):
        await repo.archive("signal", "missing")


@pytest.mark.asyncio
async def test_archive_unlocked_doc_happy_path_unchanged() -> None:
    """Normal path (no race, unlocked): the doc is recategorized to
    ARCHIVE exactly as before the TOCTOU hardening."""
    coll = _SimpleColl(
        {"_id": "sig-1", "type": "signal", "category": "DEV", "locked": False}
    )
    repo = _repo_with_coll(coll)
    await repo.archive("signal", "sig-1")
    assert coll.doc is not None
    assert coll.doc["category"] == Category.ARCHIVE.value
    assert coll.update_one_calls == 1


@pytest.mark.asyncio
async def test_archive_indicator_unlocked_sets_deleted_unchanged() -> None:
    """Indicator happy path: archive sets ``deleted = True`` (no race)."""
    coll = _SimpleColl(
        {"_id": "ind-1", "type": "indicator", "deleted": False, "locked": False}
    )
    repo = _repo_with_coll(coll)
    await repo.archive("indicator", "ind-1")
    assert coll.doc is not None and coll.doc["deleted"] is True
