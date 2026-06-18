"""Unit tests for the PostgreSQL row helpers ``to_pg_row`` / ``from_pg_row``.

These are the JSONB persistence mapping for ``tcg_app_data``:

* ``to_pg_row(doc)`` → ``(id, type, category, locked, payload, created_at, updated_at)``
  where ``payload`` is the full document dict (the single source of truth) and the
  other columns are indexable projections.
* ``from_pg_row(row)`` reconstructs the dataclass FROM ``payload`` so the payload is
  authoritative; the projection columns are only used by SQL filters.

Pure in-memory — no database.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tcg.types.persistence import (
    BasketDoc,
    Category,
    IndicatorDoc,
    PortfolioDoc,
    SignalDoc,
    from_pg_row,
    to_pg_row,
)

NOW = datetime(2026, 1, 1, 12, 30, 45, tzinfo=timezone.utc)


def _indicator(**kw) -> IndicatorDoc:
    base = dict(
        id="rsi-14",
        type="indicator",
        name="RSI 14",
        definition={"period": 14},
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kw)
    return IndicatorDoc(**base)


def _signal(**kw) -> SignalDoc:
    base = dict(
        id="sig-1",
        type="signal",
        name="Sig",
        category=Category.DEV,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kw)
    return SignalDoc(**base)


def _portfolio(**kw) -> PortfolioDoc:
    base = dict(
        id="ptf-1",
        type="portfolio",
        name="60-40",
        category=Category.RESEARCH,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kw)
    return PortfolioDoc(**base)


def _basket(**kw) -> BasketDoc:
    base = dict(
        id="bkt-1",
        type="basket",
        name="B",
        category=Category.RESEARCH,
        asset_class="equity",
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kw)
    return BasketDoc(**base)


# ---------------------------------------------------------------------------
# Column tuple shape
# ---------------------------------------------------------------------------


def test_to_pg_row_returns_seven_column_tuple_for_signal() -> None:
    cols = to_pg_row(_signal())
    assert len(cols) == 7
    doc_id, doc_type, category, locked, payload, created_at, updated_at = cols
    assert doc_id == "sig-1"
    assert doc_type == "signal"
    assert category == "DEV"  # plain string, not the enum
    assert locked is False
    assert isinstance(payload, dict)
    assert created_at == NOW
    assert updated_at == NOW


def test_to_pg_row_payload_is_full_doc_with_plain_id_key() -> None:
    """The JSONB payload carries the full document and uses ``id`` (not
    ``_id`` — the PG primary key column is ``id``)."""
    _, _, _, _, payload, _, _ = to_pg_row(_signal())
    assert payload["id"] == "sig-1"
    assert "_id" not in payload
    assert payload["type"] == "signal"
    assert payload["category"] == "DEV"
    assert payload["name"] == "Sig"


def test_to_pg_row_indicator_locked_projection() -> None:
    cols = to_pg_row(_indicator(locked=True))
    _, doc_type, category, locked, _, _, _ = cols
    assert doc_type == "indicator"
    assert locked is True
    # Active indicator → category projection is None (NULL in PG).
    assert category is None


def test_to_pg_row_basket_has_no_locked_projection() -> None:
    """Baskets are not lockable — the locked projection column is None."""
    cols = to_pg_row(_basket())
    _, doc_type, category, locked, _, _, _ = cols
    assert doc_type == "basket"
    assert category == "RESEARCH"
    assert locked is None


# ---------------------------------------------------------------------------
# Round-trip via payload (payload is authoritative)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        _indicator(),
        _indicator(locked=True),
        _signal(
            inputs=({"k": "v"},), rules={"a": 1}, settings={"x": True}, description="d"
        ),
        _signal(locked=True),
        _portfolio(legs=({"label": "SPY", "weight": 60},), rebalance="monthly"),
        _portfolio(locked=True),
        _basket(legs=({"instrument": {"type": "spot"}, "weight": 0.5},)),
    ],
)
def test_from_pg_row_reconstructs_from_payload(doc) -> None:
    """``from_pg_row(to_pg_row(doc)) == doc`` for every kind."""
    row_tuple = to_pg_row(doc)
    # Emulate a psycopg dict_row read: payload + projection columns by name.
    doc_id, doc_type, category, locked, payload, created_at, updated_at = row_tuple
    row = {
        "id": doc_id,
        "type": doc_type,
        "category": category,
        "locked": locked,
        "payload": payload,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    assert from_pg_row(row) == doc


def test_from_pg_row_uses_payload_not_projection_for_content() -> None:
    """The payload is the single source of truth; the projection columns
    are only SQL filter helpers. A divergent projection must NOT override
    the payload content (we reconstruct from payload)."""
    doc = _signal(category=Category.DEV)
    _, _, _, _, payload, _, _ = to_pg_row(doc)
    row = {
        "id": "sig-1",
        "type": "signal",
        "category": "PROD",  # divergent projection — must be ignored
        "locked": False,
        "payload": payload,
        "created_at": NOW,
        "updated_at": NOW,
    }
    restored = from_pg_row(row)
    # Reconstructed from payload → category DEV, not the divergent projection.
    assert restored.category == Category.DEV


def test_to_pg_row_payload_excludes_timestamps() -> None:
    """Timestamps live in dedicated ``timestamptz`` columns, not the JSONB
    payload (JSON has no datetime). The stored payload must omit them so
    psycopg can serialize it; ``from_pg_row`` re-injects from the columns."""
    _, _, _, _, payload, created_at, updated_at = to_pg_row(_signal())
    assert "created_at" not in payload
    assert "updated_at" not in payload
    assert created_at == NOW
    assert updated_at == NOW


def test_from_pg_row_round_trips_deleted_indicator() -> None:
    """An archived indicator stores deleted=True in its payload and is
    reconstructed faithfully (deleted bool preserved)."""
    doc = _indicator(deleted=True)
    _, _, _, _, payload, _, _ = to_pg_row(doc)
    row = {
        "id": "rsi-14",
        "type": "indicator",
        "category": "DELETED",
        "locked": False,
        "payload": payload,
        "created_at": NOW,
        "updated_at": NOW,
    }
    restored = from_pg_row(row)
    assert isinstance(restored, IndicatorDoc)
    assert restored.deleted is True
