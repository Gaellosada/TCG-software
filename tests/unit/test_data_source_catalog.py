"""Source-aware CATALOG endpoints for the instrument picker.

The picker must offer ONLY what the selected warehouse actually serves. These
tests pin the routing added to the catalog GETs on ``/api/data`` and
``/api/options``:

* ``?data_source=v2`` binds the v2-compat service; the offered list is v2's
  restricted set (e.g. NO ``FUT_VIX``, NO ``OPT_VIX``);
* ``?data_source=v1`` and an OMITTED param are byte-identical — the v1 service,
  the full catalog, the v2 service never consulted;
* an unknown value is rejected at the boundary (422).

The list is WAREHOUSE-DRIVEN: nothing is hardcoded to "S&P 500 only". The final
test proves the REAL v2 adapter reports the restricted collection set with no DB
access, so the picker auto-corrects when v2 coverage later widens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from tcg.types.common import PaginatedResult
from tcg.types.market import AssetClass, InstrumentId
from tcg.types.options import OptionRootInfo

# v1 serves the full legacy catalog; v2 serves only the S&P 500 family today.
_V1_COLLECTIONS = ["INDEX", "ETF", "FOREX", "FUT_SP_500", "FUT_VIX", "FUT_GOLD"]
_V2_COLLECTIONS = ["INDEX", "FUT_SP_500"]


def _root(collection: str) -> OptionRootInfo:
    return OptionRootInfo(
        collection=collection,
        name=collection,
        has_greeks=True,
        providers=("DATABENTO",),
        expiration_first="2011-03-21",
        expiration_last="2026-07-21",
        doc_count_estimated=1000,
        strike_factor_verified=True,
    )


def _instruments(collection: str, symbols: list[str]) -> PaginatedResult:
    items = tuple(
        InstrumentId(symbol=s, asset_class=AssetClass.INDEX, collection=collection)
        for s in symbols
    )
    return PaginatedResult(items=items, total=len(items), skip=0, limit=500)


def _stub(tag: str, collections: list[str], roots: list[str]) -> MagicMock:
    svc = MagicMock()
    svc.tag = tag
    svc.list_collections = AsyncMock(
        side_effect=lambda ac=None: list(collections)
    )
    svc.list_instruments = AsyncMock(
        side_effect=lambda collection, *, skip=0, limit=50: _instruments(
            collection, [f"{tag}_{collection}_A"]
        )
    )
    svc.get_available_cycles = AsyncMock(return_value=([""] if tag == "v2" else ["H", "M", "U", "Z"]))
    svc.list_option_roots = AsyncMock(return_value=[_root(c) for c in roots])
    return svc


@pytest.fixture
def services() -> dict[str, MagicMock]:
    return {
        "v1": _stub("v1", _V1_COLLECTIONS, ["OPT_SP_500", "OPT_VIX"]),
        "v2": _stub("v2", _V2_COLLECTIONS, ["OPT_SP_500"]),
    }


@pytest.fixture
async def client(services):
    from fastapi import FastAPI

    from tcg.core.api.data import router as data_router
    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.options import router as options_router
    from tcg.types.errors import TCGError

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(data_router)
    app.include_router(options_router)
    app.state.market_data = services["v1"]
    app.state.market_data_v2_compat = services["v2"]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------- #
# /api/data/collections                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("query", ["", "?data_source=v1"])
async def test_collections_default_and_v1_are_the_full_catalog(client, services, query):
    resp = await client.get(f"/api/data/collections{query}")
    assert resp.status_code == 200, resp.text
    cols = resp.json()["collections"]
    assert cols == _V1_COLLECTIONS  # full set, incl FUT_VIX / FUT_GOLD / FOREX
    # v1 consulted, v2 never touched (byte-identity of the default path).
    assert services["v1"].list_collections.await_count == 1
    assert services["v2"].list_collections.await_count == 0


async def test_collections_v2_excludes_vix_and_uses_v2_service(client, services):
    resp = await client.get("/api/data/collections?data_source=v2")
    assert resp.status_code == 200, resp.text
    cols = resp.json()["collections"]
    assert cols == _V2_COLLECTIONS
    # The exact class of bug this task fixes: VIX (and the rest) must be gone.
    assert "FUT_VIX" not in cols
    assert "FOREX" not in cols
    assert "FUT_GOLD" not in cols
    assert services["v2"].list_collections.await_count == 1
    assert services["v1"].list_collections.await_count == 0


async def test_unknown_data_source_is_422(client):
    resp = await client.get("/api/data/collections?data_source=v3")
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# /api/data/{collection}  (list_instruments)                                   #
# --------------------------------------------------------------------------- #


async def test_instruments_v2_binds_v2_service(client, services):
    resp = await client.get("/api/data/INDEX?data_source=v2")
    assert resp.status_code == 200, resp.text
    symbols = [i["symbol"] for i in resp.json()["items"]]
    assert symbols == ["v2_INDEX_A"]
    assert services["v2"].list_instruments.await_count == 1
    assert services["v1"].list_instruments.await_count == 0


async def test_instruments_default_binds_v1_service(client, services):
    resp = await client.get("/api/data/INDEX")
    assert resp.status_code == 200, resp.text
    assert [i["symbol"] for i in resp.json()["items"]] == ["v1_INDEX_A"]
    assert services["v1"].list_instruments.await_count == 1
    assert services["v2"].list_instruments.await_count == 0


# --------------------------------------------------------------------------- #
# /api/data/continuous/{collection}/cycles                                     #
# --------------------------------------------------------------------------- #


async def test_cycles_v2_binds_v2_service(client, services):
    resp = await client.get("/api/data/continuous/FUT_SP_500/cycles?data_source=v2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cycles"] == [""]
    assert services["v2"].get_available_cycles.await_count == 1
    assert services["v1"].get_available_cycles.await_count == 0


async def test_cycles_default_binds_v1_service(client, services):
    resp = await client.get("/api/data/continuous/FUT_SP_500/cycles")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cycles"] == ["H", "M", "U", "Z"]
    assert services["v1"].get_available_cycles.await_count == 1


# --------------------------------------------------------------------------- #
# /api/options/roots                                                           #
# --------------------------------------------------------------------------- #


async def test_option_roots_v2_excludes_vix_and_uses_v2_service(client, services):
    resp = await client.get("/api/options/roots?data_source=v2")
    assert resp.status_code == 200, resp.text
    collections = [r["collection"] for r in resp.json()["roots"]]
    assert collections == ["OPT_SP_500"]
    assert "OPT_VIX" not in collections
    assert services["v2"].list_option_roots.await_count == 1
    assert services["v1"].list_option_roots.await_count == 0


async def test_option_roots_default_is_the_full_v1_set(client, services):
    resp = await client.get("/api/options/roots")
    assert resp.status_code == 200, resp.text
    collections = [r["collection"] for r in resp.json()["roots"]]
    assert collections == ["OPT_SP_500", "OPT_VIX"]
    assert services["v1"].list_option_roots.await_count == 1
    assert services["v2"].list_option_roots.await_count == 0


# --------------------------------------------------------------------------- #
# Ground truth: the REAL v2 adapter reports the restricted set (no hardcode).  #
# --------------------------------------------------------------------------- #


async def test_real_v2_adapter_collections_exclude_vix_without_db():
    """``list_collections`` on the real adapter touches no DB and reports only
    the collections v2 serves — so the routing above surfaces the genuine,
    warehouse-driven set (it will widen automatically when v2 does)."""
    from tcg.data._v2_compat.adapter import V2MarketDataAdapter

    adapter = V2MarketDataAdapter(MagicMock())
    cols = await adapter.list_collections()
    assert "FUT_VIX" not in cols
    assert "FUT_SP_500" in cols
    assert "INDEX" in cols
