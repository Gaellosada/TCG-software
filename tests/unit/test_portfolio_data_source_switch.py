"""Per-run ``data_source`` switch: binding, propagation, cache keys, errors.

Covers the four things that can silently produce a *false comparison* between
the two warehouses:

1. the switch actually rebinds the market-data service at the route;
2. it propagates to every nested compute — a composed child at arbitrary depth
   and a signal leg — so a "v2" parent can never read v1 underneath;
3. two runs differing ONLY in ``data_source`` key differently, at BOTH cache
   layers (the on-disk result cache and the loop-global option chain cache);
4. a request v2 cannot serve fails as HTTP 400 with an actionable message,
   never a 500 and never a quietly-shorter curve.
"""

from __future__ import annotations

import contextlib
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

import tcg.core.api.portfolio as portfolio
from tcg.core.api._options_chain_cache import make_chain_bulk_key, reader_source_id
from tcg.core.api.portfolio import PortfolioRequest, _portfolio_cache_key
from tcg.data.service import DefaultMarketDataService
from tcg.types.market import PriceSeries

DATES = [20240102, 20240103, 20240104, 20240105, 20240108]


def _price_series(base: float) -> PriceSeries:
    c = np.array([base + i for i in range(len(DATES))], dtype=np.float64)
    return PriceSeries(
        dates=np.array(DATES, dtype=np.int64),
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(len(DATES), 1000.0, dtype=np.float64),
    )


def _stub_service(tag: str) -> MagicMock:
    """A ``MarketDataService``-shaped stub that records which one was used."""
    common_dates = np.array(DATES, dtype=np.int64)

    async def _aligned(legs_spec):
        return common_dates, {label: _price_series(100.0) for label in legs_spec}

    svc = MagicMock()
    svc.tag = tag
    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)
    svc.option_trade_date_coverage = AsyncMock(return_value=(None, None))
    return svc


@pytest.fixture
def services() -> dict[str, MagicMock]:
    return {"v1": _stub_service("v1"), "v2": _stub_service("v2")}


@pytest.fixture
def mock_app(services):
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.types.errors import TCGError

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = services["v1"]
    app.state.market_data_v2_compat = services["v2"]
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def client(mock_app):
    transport = ASGITransport(app=mock_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _body(**overrides) -> dict:
    body = {
        "legs": {"a": {"type": "instrument", "collection": "INDEX", "symbol": "SPX"}},
        "weights": {"a": 100.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# 1. Binding: the route picks the service named by the body                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides, expected_used, expected_idle",
    [
        ({}, "v1", "v2"),  # field ABSENT ⇒ exactly today's behaviour
        ({"data_source": "v1"}, "v1", "v2"),
        ({"data_source": "v2"}, "v2", "v1"),
    ],
)
async def test_route_binds_the_named_service(
    client, services, overrides, expected_used, expected_idle
):
    resp = await client.post("/api/portfolio/compute", json=_body(**overrides))
    assert resp.status_code == 200, resp.text
    assert services[expected_used].get_aligned_prices.await_count == 1
    assert services[expected_idle].get_aligned_prices.await_count == 0


async def test_data_source_defaults_to_v1_on_the_model():
    assert PortfolioRequest(**_body()).data_source == "v1"


async def test_unknown_data_source_is_rejected_at_the_boundary(client):
    resp = await client.post("/api/portfolio/compute", json=_body(data_source="v3"))
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 2. Propagation: nested composed children and signal legs                     #
# --------------------------------------------------------------------------- #


def _nest(depth: int) -> dict:
    """A composed body nested ``depth`` levels deep."""
    body = _body()
    for _ in range(depth):
        body = {
            "legs": {"child": {"type": "portfolio", "portfolio": body}},
            "weights": {"child": 100.0},
            "rebalance": "none",
            "return_type": "normal",
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
    return body


@pytest.mark.parametrize("depth", [1, 2, 5])
def test_child_request_forces_the_parent_source_at_any_depth(depth):
    """``_child_request`` OVERRIDES the child's own ``data_source``.

    The child is computed with the parent's already-bound service, so a child
    body that kept its own (stale, or v1-by-default) value would key a v2-read
    result under a v1 key. Depth is exercised because ``model_copy`` is shallow:
    each level must be rewritten as the recursion descends, not once at the top.
    """
    parent = PortfolioRequest(**_nest(depth))
    parent = parent.model_copy(update={"data_source": "v2"})

    node = parent
    for _ in range(depth):
        child = node.legs["child"].portfolio
        assert child is not None
        # Every child arrives with the default "v1" ...
        assert child.data_source == "v1"
        rewritten = portfolio._child_request(child, True, node.data_source)
        # ... and leaves carrying the parent's source.
        assert rewritten.data_source == "v2"
        node = rewritten


async def test_composed_child_is_computed_on_the_parent_source(client, services):
    """Depth-1 composed leg: the child must NOT fall back to v1."""
    resp = await client.post(
        "/api/portfolio/compute", json=_nest(1) | {"data_source": "v2"}
    )
    assert resp.status_code == 200, resp.text
    assert services["v2"].get_aligned_prices.await_count == 1
    assert services["v1"].get_aligned_prices.await_count == 0


async def test_signal_leg_is_evaluated_on_the_parent_source(monkeypatch, client):
    """A signal leg receives the parent's bound service, not ``app.state``.

    The single worst failure mode: a v2 parent whose signal leg silently reads
    v1 produces a curve attributable to neither warehouse.
    """
    seen: list[str] = []

    async def _fake_signal_leg(label, leg, svc, start, end, repo, cost_config=None):
        seen.append(svc.tag)
        raise AssertionError("stop after capturing the service")

    monkeypatch.setattr(portfolio, "_evaluate_signal_leg", _fake_signal_leg)

    body = _body(data_source="v2")
    body["legs"] = {
        "s": {
            "type": "signal",
            "signal_spec": {"spec": {"id": "s", "inputs": [], "blocks": []}},
        }
    }
    body["weights"] = {"s": 100.0}
    with pytest.raises(AssertionError, match="stop after capturing"):
        await client.post("/api/portfolio/compute", json=body)
    assert seen == ["v2"]


# --------------------------------------------------------------------------- #
# 3. Cache keys must diverge across sources (guardrail Sign 2)                 #
# --------------------------------------------------------------------------- #


def test_result_cache_key_differs_on_data_source_alone():
    v1 = PortfolioRequest(**_body(data_source="v1"))
    v2 = PortfolioRequest(**_body(data_source="v2"))
    assert _portfolio_cache_key(v1) != _portfolio_cache_key(v2)


def test_result_cache_key_ignores_use_cache_but_not_data_source():
    """``_strip_use_cache`` must never learn about ``data_source``.

    ``use_cache`` selects WHETHER to use the cache; ``data_source`` selects
    WHICH result the body maps to. Conflating them would serve a v1 curve for a
    v2 request.
    """
    a = PortfolioRequest(**_body(data_source="v2", use_cache=True))
    b = PortfolioRequest(**_body(data_source="v2", use_cache=False))
    assert _portfolio_cache_key(a) == _portfolio_cache_key(b)

    stripped = portfolio._strip_use_cache(a.model_dump(mode="json"))
    assert "use_cache" not in stripped
    assert stripped["data_source"] == "v2"


_CHAIN_ARGS = dict(
    root="OPT_SP_500",
    dates=[date(2022, 3, 1), date(2022, 3, 2)],
    type="P",
    expiration_min=date(2022, 3, 1),
    expiration_max=date(2022, 4, 1),
    strike_min=1000.0,
    strike_max=4000.0,
    expiration_cycle="W3 Friday",
)


def test_chain_bulk_key_differs_across_sources():
    """The chain cache is LOOP-global, so identical args from the two readers
    would collide without the source discriminator."""
    k_v1 = make_chain_bulk_key(source="v1-reader", **_CHAIN_ARGS)
    k_v2 = make_chain_bulk_key(source="v2-reader", **_CHAIN_ARGS)
    assert k_v1 != k_v2


def test_chain_bulk_reader_source_id_separates_the_two_readers():
    """The discriminator is derived, not passed — it cannot be forgotten."""
    from tcg.data._sql.options import SqlOptionsDataReader
    from tcg.data._v2_compat.options_reader import V2OptionsDataReader

    pool = MagicMock()
    v1_id = reader_source_id(SqlOptionsDataReader(pool))
    v2_id = reader_source_id(V2OptionsDataReader(pool))
    assert v1_id != v2_id
    assert make_chain_bulk_key(source=v1_id, **_CHAIN_ARGS) != make_chain_bulk_key(
        source=v2_id, **_CHAIN_ARGS
    )


async def test_cached_bulk_reader_does_not_serve_across_sources():
    """End-to-end at the proxy layer: two readers, one cache, no cross-serve."""
    from tcg.core.api._options_chain_cache import ChainBulkCache
    from tcg.core.api._options_wiring import CachedBulkChainReader

    class ReaderA:
        def __init__(self) -> None:
            self.calls = 0

        async def query_chain_bulk(self, **kwargs):
            self.calls += 1
            return {d: [] for d in kwargs["dates"]}

    class ReaderB(ReaderA):
        pass

    cache = ChainBulkCache()
    a, b = ReaderA(), ReaderB()
    await CachedBulkChainReader(a, cache).query_chain_bulk(**_CHAIN_ARGS)
    await CachedBulkChainReader(b, cache).query_chain_bulk(**_CHAIN_ARGS)
    # B must have gone to the DB rather than serving A's rows.
    assert (a.calls, b.calls) == (1, 1)
    # ... while a second A resolve still hits the cache (no regression).
    await CachedBulkChainReader(a, cache).query_chain_bulk(**_CHAIN_ARGS)
    assert a.calls == 1


# --------------------------------------------------------------------------- #
# 4. Error surfacing — every v2 shortfall is a 400 (SC8)                       #
# --------------------------------------------------------------------------- #


@pytest.fixture
def real_v2_app(services):
    """App whose v2 service is the REAL adapter over a mock pool.

    The guards under test all fire before any SQL is issued, so a mock pool is
    enough — and using the real adapter means the test pins the real message.
    """
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.portfolio import router as portfolio_router
    from tcg.data._v2_compat.adapter import V2MarketDataAdapter
    from tcg.data._v2_compat.options_reader import V2OptionsDataReader
    from tcg.types.errors import TCGError

    class _StubbedCoverage(V2MarketDataAdapter):
        """Real adapter, except coverage — the ONLY method that needs the DB
        before the guards under test fire (the E7 floor check runs first, and
        an option leg requires an explicit range). Everything the assertions
        touch is the real implementation."""

        async def option_trade_date_coverage(self, root):
            return date(2011, 3, 21), date(2026, 7, 21)

    pool = MagicMock()
    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(portfolio_router)
    app.state.market_data = services["v1"]
    app.state.market_data_v2_compat = _StubbedCoverage(
        pool, options_reader=V2OptionsDataReader(pool)
    )
    app.state.app_db_repo = object()
    return app


@pytest.fixture
async def real_v2_client(real_v2_app):
    transport = ASGITransport(app=real_v2_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_unsupported_collection_is_400(real_v2_client):
    body = _body(data_source="v2")
    body["legs"] = {
        "a": {
            "type": "continuous",
            "collection": "FUT_VIX",
            "strategy": "front_month",
        }
    }
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert "FUT_VIX" in message
    # Actionable: it must name the way out, not just the failure.
    assert "v1" in message


def _option_leg(**overrides) -> dict:
    leg = {
        "type": "option_stream",
        "collection": "OPT_SP_500",
        "option_type": "P",
        "maturity": {"kind": "end_of_month", "offset_months": 0},
        "selection": {"kind": "by_delta", "target": -0.10, "tolerance": 0.20},
        "stream": "close",
        "hold_between_rolls": True,
    }
    leg.update(overrides)
    return leg


@pytest.mark.parametrize(
    "cycle, expected_fragment",
    [
        ("M", "monthly"),  # E3 — v2 has no 3rd-Friday monthlies
        (None, "weekly"),  # E4 — no cycle filter is not comparable to v1
    ],
)
async def test_unserviceable_option_cycle_is_400(
    real_v2_client, cycle, expected_fragment
):
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle=cycle)}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert expected_fragment in message
    assert "v1" in message


async def test_start_before_v2_option_floor_is_400(client, services):
    """E7 — the reader never sees the run window, so the floor is checked here.

    Without this the run returns a SHORTER curve than the same spec on v1, which
    reads as a strategy difference rather than a data gap.
    """
    services["v2"].option_trade_date_coverage = AsyncMock(
        return_value=(date(2011, 3, 21), date(2026, 7, 21))
    )
    body = _body(data_source="v2", start="2005-01-01")
    body["legs"] = {"o": _option_leg(cycle="W3 Friday")}
    body["weights"] = {"o": 100.0}
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert "2011-03-21" in message  # names the floor
    assert "2005-01-01" in message  # and what was asked for


async def test_v1_run_never_consults_the_v2_coverage_floor(client, services):
    """Sign 1: the E7 check must be entirely inert on the default path."""
    body = _body(start="2005-01-01")
    body["legs"] = {"o": _option_leg(cycle="W3 Friday")}
    body["weights"] = {"o": 100.0}
    with contextlib.suppress(Exception):
        await client.post("/api/portfolio/compute", json=body)
    assert services["v1"].option_trade_date_coverage.await_count == 0
