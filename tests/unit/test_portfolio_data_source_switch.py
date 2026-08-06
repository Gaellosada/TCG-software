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
from types import SimpleNamespace
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
    """A ``MarketDataService``-shaped stub that records which one was used.

    Per-instrument selection is proved by RECORDING what each service was asked
    for: ``seen_aligned`` collects the set of leg labels each ``get_aligned_prices``
    group saw (so the batched-fetch split is asserted precisely, not just via
    ``await_count``), and ``seen_prices`` collects the ``(collection,
    instrument_id)`` each ``get_prices`` saw (the signals routing proof).
    """
    common_dates = np.array(DATES, dtype=np.int64)
    svc = MagicMock()
    svc.tag = tag
    svc.seen_aligned = []
    svc.seen_prices = []

    async def _aligned(legs_spec):
        svc.seen_aligned.append(set(legs_spec))
        return common_dates, {label: _price_series(100.0) for label in legs_spec}

    async def _prices(collection, instrument_id, *, start=None, end=None, provider=None):
        svc.seen_prices.append((collection, instrument_id))
        return _price_series(100.0)

    svc.asset_class_for = DefaultMarketDataService.asset_class_for
    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)
    svc.get_prices = AsyncMock(side_effect=_prices)
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
def test_child_request_keeps_the_childs_own_source_at_any_depth(depth):
    """``_child_request`` does NOT override the child's ``data_source`` — each
    child keeps its own (a special case of per-instrument selection), so a
    composed portfolio can mix v1/v2 children. The caller
    (``_evaluate_portfolio_leg``) binds the matching service via ``svc_for``, so
    body and reality stay in lock-step per-child (key-parity vs a standalone
    compute of the child on its own source).
    """
    parent = PortfolioRequest(**_nest(depth))
    parent = parent.model_copy(update={"data_source": "v2"})

    node = parent
    for _ in range(depth):
        child = node.legs["child"].portfolio
        assert child is not None
        assert child.data_source == "v1"        # arrives with its own value ...
        rewritten = portfolio._child_request(child, True)
        assert rewritten.data_source == "v1"    # ... and KEEPS it (no forcing)
        node = rewritten


async def test_composed_child_is_computed_on_its_own_source(client, services):
    """A composed leg computes each child on the CHILD's own ``data_source``, not
    the parent's — the basis for v1-vs-v2 comparison (per-instrument, composed)."""
    body = _nest(1) | {"data_source": "v1"}          # parent v1
    body["legs"]["child"]["portfolio"]["data_source"] = "v2"   # child explicitly v2
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    assert services["v2"].get_aligned_prices.await_count == 1   # child ran on v2
    assert services["v1"].get_aligned_prices.await_count == 0   # NOT the parent's v1


async def test_composed_mixes_v1_and_v2_children(client, services):
    """The feature — ONE composed portfolio comparing a v1 child and a v2 child,
    each computed on its own warehouse (per-instrument selection, composed)."""
    body = {
        "legs": {
            "a": {"type": "portfolio", "portfolio": _body() | {"data_source": "v1"}},
            "b": {"type": "portfolio", "portfolio": _body() | {"data_source": "v2"}},
        },
        "weights": {"a": 50.0, "b": 50.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
        "data_source": "v1",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    assert services["v1"].get_aligned_prices.await_count == 1   # child a → v1
    assert services["v2"].get_aligned_prices.await_count == 1   # child b → v2


async def test_signal_leg_is_evaluated_on_the_parent_source(monkeypatch, client):
    """A signal leg receives the parent's bound service, not ``app.state``.

    The single worst failure mode: a v2 parent whose signal leg silently reads
    v1 produces a curve attributable to neither warehouse.
    """
    seen: list[str] = []

    async def _fake_signal_leg(
        label, leg, svc, start, end, repo, cost_config=None, **kwargs
    ):
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


# --------------------------------------------------------------------------- #
# 9. The SIGNALS route gets the same boundary, on the same nesting walk.        #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def signals_client(services):
    from fastapi import FastAPI

    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.signals import router as signals_router
    from tcg.types.errors import TCGError

    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(signals_router)
    app.state.market_data = services["v1"]
    app.state.market_data_v2_compat = services["v2"]
    app.state.app_db_repo = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _signal_body(instrument: dict, **overrides) -> dict:
    body = {
        "spec": {"id": "s", "name": "s", "inputs": [], "rules": {}},
        "indicators": [],
        "instruments": {"px": instrument},
        "start": "2020-01-01",
        "end": "2020-06-30",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "instrument, expected_fragment",
    [
        (
            {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "option_type": "P",
                "maturity": {"kind": "end_of_month", "offset_months": 0},
                "selection": {"kind": "by_delta", "target": -0.10, "tolerance": 0.2},
                "stream": "mid",
                "cycle": "W3 Friday",
            },
            "no mid data",
        ),
        (
            {"type": "spot", "collection": "FUT_VIX", "instrument_id": "FUT_VIX_X"},
            "FUT_VIX",
        ),
    ],
    ids=["option-mid-stream", "unsupported-collection"],
)
async def test_signals_route_rejects_unservable_v2_instruments(
    signals_client, instrument, expected_fragment
):
    """``SignalComputeRequest.instruments`` is typed ``dict[str, Any]``, so the
    generic walk — not a typed model traversal — is what reaches these."""
    resp = await signals_client.post(
        "/api/signals/compute", json=_signal_body(instrument, data_source="v2")
    )
    assert resp.status_code == 400, resp.text
    assert expected_fragment in resp.json()["message"]


_FLOOR_OPTION_INSTRUMENT = {
    "type": "option_stream",
    "collection": "OPT_SP_500",
    "option_type": "P",
    "maturity": {"kind": "end_of_month", "offset_months": 0},
    "selection": {"kind": "by_delta", "target": -0.10, "tolerance": 0.2},
    "stream": "close",
    "cycle": "W3 Friday",
}


async def test_signals_route_enforces_the_v2_option_floor(signals_client, services):
    """E7 on the SIGNALS route — the half that was missing.

    Every pure precondition passes here (weekly cycle, servable collection,
    servable stream), so nothing below the route objects: the run reaches the
    engine and comes back starting at the v2 EW3 floor instead of 2013. Read
    against the v1 run of the same definition that is a five-year data gap
    wearing the costume of a strategy difference — exactly what this feature
    exists to prevent.
    """
    services["v2"].option_trade_date_coverage = AsyncMock(
        return_value=(date(2016, 2, 22), date(2026, 7, 21))
    )
    body = _signal_body(
        _FLOOR_OPTION_INSTRUMENT, data_source="v2", start="2013-01-01", end="2020-06-30"
    )
    resp = await signals_client.post("/api/signals/compute", json=body)

    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert "2016-02-22" in message  # names the floor
    assert "2013-01-01" in message  # and what was asked for
    assert "OPT_SP_500" in message  # and which root


async def test_signals_route_v1_never_consults_the_v2_floor(signals_client, services):
    """Sign 1 — the same definition on v1 must not reach the floor at all."""
    body = _signal_body(
        _FLOOR_OPTION_INSTRUMENT, data_source="v1", start="2013-01-01", end="2020-06-30"
    )
    with contextlib.suppress(Exception):
        await signals_client.post("/api/signals/compute", json=body)

    services["v2"].option_trade_date_coverage.assert_not_awaited()
    assert services["v1"].option_trade_date_coverage.await_count == 0


async def test_signals_route_v1_is_unaffected(signals_client):
    """Sign 1 — the same body on v1 must never see a v2 precondition."""
    instrument = {"type": "spot", "collection": "FUT_VIX", "instrument_id": "FUT_VIX_X"}
    resp = await signals_client.post(
        "/api/signals/compute", json=_signal_body(instrument, data_source="v1")
    )
    assert 'data source "v2"' not in resp.text.lower()


# --------------------------------------------------------------------------- #
# 10. Boundary preconditions (wfix) — every v2 shortfall the ENGINE would       #
#     otherwise swallow must surface as a clean, actionable 400.                #
#                                                                               #
#     Route-level on purpose: the defect being fixed is precisely that a unit   #
#     test of the reader passes while the user still sees an all-NaN curve.     #
# --------------------------------------------------------------------------- #


async def test_option_leg_without_cycle_is_400_with_a_clean_message(real_v2_client):
    """E4 — and the message must NOT be the nested/garbled one.

    ``options_reader`` used to pass this whole sentence as the ``cycle``
    ARGUMENT of ``V2UnsupportedCycle``, which interpolates it into
    "...expiration cycle '{cycle}'..." — nesting the paragraph inside itself.
    """
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle=None)}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]

    assert "explicit weekly expiration cycle" in message
    for literal in ("'W1 Friday'", "'W2 Friday'", "'W3 Friday'", "'W4 Friday'"):
        assert literal in message
    assert "o" in message  # names the offending leg
    # NOT nested: the E3 wrapper's phrasing must be absent entirely, and the
    # sentence must appear exactly once.
    assert "This leg requests expiration cycle" not in message
    assert message.count('Data source "v2" requires an explicit') == 1


async def test_option_leg_requesting_mid_is_400_naming_the_alternatives(real_v2_client):
    """E5 — ``mid`` is the model DEFAULT stream and does not exist on v2 at all.

    Before the boundary check this was the single most likely v2 option run,
    and it died inside the resolver as an unattributable all-NaN curve.
    """
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle="W3 Friday", stream="mid")}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]

    assert "no mid data" in message
    assert '"close"' in message and '"bs_mid"' in message  # the way forward
    assert "settlement" in message
    assert message.count('Data source "v2" has no') == 1  # not nested


@pytest.mark.parametrize("stream", ["volume", "open_interest"])
async def test_option_level_streams_absent_from_v2_are_400(real_v2_client, stream):
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle="W3 Friday", stream=stream)}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    assert f"no {stream} data" in resp.json()["message"]


async def test_option_leg_with_monthly_cycle_is_400(real_v2_client):
    """E3 — v2 has no 3rd-Friday monthlies; serving the weekly half silently
    would compare two different strategies."""
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle="M")}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert "monthly" in message
    assert "'M'" in message  # names the offending value
    assert "'W3 Friday'" in message


async def test_option_leg_on_an_unsupported_collection_is_400(real_v2_client):
    """E1 on an OPTION leg — the collection error is raised inside the reader,
    i.e. inside the resolve the engine swallows. Only the boundary check makes
    it visible."""
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle="W3 Friday", collection="OPT_VIX")}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert "OPT_VIX" in message
    assert "OPT_SP_500" in message  # what v2 DOES serve
    assert message.count("does not have data for collection") == 1  # not nested


@pytest.mark.parametrize(
    "leg_overrides",
    [
        {"cycle": None},
        {"cycle": "M"},
        {"cycle": "W3 Friday", "stream": "mid"},
        {"cycle": "W3 Friday", "collection": "OPT_VIX"},
    ],
    ids=["no-cycle", "monthly", "mid-stream", "bad-collection"],
)
async def test_v1_is_unaffected_by_every_v2_precondition(
    client, services, leg_overrides
):
    """Sign 1 — the SAME requests on v1 must never hit any of these checks.

    Asserting on the message is not enough here (v1 may legitimately fail for
    an unrelated reason), so this pins the mechanism: the v2 service is never
    consulted, and no v2 wording appears.
    """
    # The v1 stub is a MagicMock; give it the async methods the option path
    # reaches so the request gets PAST the boundary and fails (if at all) for a
    # data reason rather than on an un-awaitable mock. The hold-leg freeze fix
    # (dd8c611) added a data-source-agnostic open-probe on the two-phase hold
    # path, so the option resolve now also reaches ``query_chain_bulk`` (a real
    # async method the production reader provides). Empty results keep the leg a
    # data-reason NaN — the v2 service is still never consulted.
    services["v1"].list_option_expirations_filtered = AsyncMock(return_value=[])
    services["v1"].options_reader.query_chain_bulk = AsyncMock(return_value={})

    body = _body(data_source="v1", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(**leg_overrides)}
    body["weights"] = {"o": 100.0}
    resp = await client.post("/api/portfolio/compute", json=body)

    services["v2"].get_aligned_prices.assert_not_awaited()
    services["v2"].option_trade_date_coverage.assert_not_awaited()
    assert 'data source "v2"' not in resp.text.lower()


async def test_a_nested_composed_child_leg_is_checked_too(real_v2_client):
    """A composed child inlines a whole portfolio body; its legs are as capable
    of being unservable as the parent's, and the child is computed with the
    parent's already-bound v2 service."""
    child = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    child["legs"] = {"inner": _option_leg(cycle="W3 Friday", stream="mid")}
    child["weights"] = {"inner": 100.0}

    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"c": {"type": "portfolio", "portfolio": child}}
    body["weights"] = {"c": 100.0}

    resp = await real_v2_client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    assert "no mid data" in resp.json()["message"]


async def test_a_servable_v2_option_leg_is_not_rejected(real_v2_client):
    """Over-rejection guard: a weekly + settlement-stream leg is exactly what
    v2 CAN serve, so the boundary must let it through to the compute path."""
    body = _body(data_source="v2", start="2020-01-01", end="2020-06-30")
    body["legs"] = {"o": _option_leg(cycle="W3 Friday", stream="close")}
    body["weights"] = {"o": 100.0}
    resp = await real_v2_client.post("/api/portfolio/compute", json=body)

    # It will fail later (the pool is a MagicMock — no rows), but it must NOT
    # fail on a precondition: none of the boundary wordings may appear.
    for wording in ("expiration cycle", "does not have data for collection", "has no"):
        assert wording not in resp.text


# --------------------------------------------------------------------------- #
# 11. Per-instrument selection (flat portfolio + signals-route + byte-identity) #
# --------------------------------------------------------------------------- #


async def test_flat_portfolio_routes_each_instrument_to_its_own_source(client, services):
    """The generalization of the composed-child feature to leaves of ONE flat
    portfolio: the batched instrument fetch is SPLIT by each leg's ``data_source``.

    Asserting ``await_count`` alone is too weak (a single merged call on one
    service could pass); we record the leg labels each ``get_aligned_prices``
    group saw, so v1 must have seen ONLY its leg and v2 ONLY its leg.
    """
    body = {
        "legs": {
            "a": {"type": "instrument", "collection": "INDEX", "symbol": "SPX", "data_source": "v1"},
            "b": {"type": "instrument", "collection": "INDEX", "symbol": "SPX", "data_source": "v2"},
        },
        "weights": {"a": 50.0, "b": 50.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
        # no top-level data_source → v1 default; per-leaf overrides win
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    # Each source's service is hit exactly once, each with ONLY its own leg.
    assert services["v1"].get_aligned_prices.await_count == 1
    assert services["v2"].get_aligned_prices.await_count == 1
    assert services["v1"].seen_aligned == [{"a"}]
    assert services["v2"].seen_aligned == [{"b"}]


async def test_all_v1_flat_portfolio_issues_exactly_one_aligned_call(client, services):
    """INVARIANT: a single-source (all-v1) portfolio issues EXACTLY ONE
    ``get_aligned_prices`` call, on the v1 service, with the WHOLE leg set — so
    behaviour and perf are identical to before the split (no intersection/merge).
    """
    body = {
        "legs": {
            "a": {"type": "instrument", "collection": "INDEX", "symbol": "SPX"},
            "b": {"type": "instrument", "collection": "INDEX", "symbol": "SPX"},
        },
        "weights": {"a": 50.0, "b": 50.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2024-01-01",
        "end": "2024-12-31",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    assert services["v1"].get_aligned_prices.await_count == 1
    assert services["v2"].get_aligned_prices.await_count == 0
    assert services["v1"].seen_aligned == [{"a", "b"}]  # ONE call, BOTH legs


def _signal_input(iid: str, instrument_id: str, source: str | None) -> dict:
    inst = {"type": "spot", "collection": "INDEX", "instrument_id": instrument_id}
    if source is not None:
        inst["data_source"] = source
    return {"id": iid, "instrument": inst}


def _entry_block(bid: str, input_id: str) -> dict:
    return {
        "id": bid,
        "name": bid,
        "input_id": input_id,
        "weight": 100.0,
        "conditions": [
            {
                "op": "gt",
                "lhs": {"kind": "instrument", "input_id": input_id, "field": "close"},
                "rhs": {"kind": "constant", "value": 0.0},
            }
        ],
    }


async def test_signals_route_routes_each_spot_input_to_its_own_source(
    signals_client, services
):
    """Two spot inputs on DIFFERENT sources must hit ``services["v1"].get_prices``
    vs ``services["v2"].get_prices`` respectively.

    Distinct ``instrument_id``s (``V1SYM`` / ``V2SYM``) let us record which
    warehouse each input hit. This exercises BOTH per-instrument paths threaded
    by ``svc_for``: the overlap pass (``compute_input_overlap`` →
    ``_date_array_for_leaf_instrument``) AND the fetcher (rules reference each
    input via an instrument operand).
    """
    body = {
        "spec": {
            "id": "s",
            "name": "s",
            "inputs": [
                _signal_input("X", "V1SYM", "v1"),
                _signal_input("Y", "V2SYM", "v2"),
            ],
            "rules": {
                "entries": [_entry_block("e1", "X"), _entry_block("e2", "Y")],
                "exits": [],
            },
        },
        "indicators": [],
        "start": "2024-01-01",
        "end": "2024-12-31",
        # no top-level data_source → v1 default; per-input overrides win
    }
    resp = await signals_client.post("/api/signals/compute", json=body)
    assert resp.status_code == 200, resp.text

    v1_seen = services["v1"].seen_prices
    v2_seen = services["v2"].seen_prices
    assert ("INDEX", "V1SYM") in v1_seen
    assert ("INDEX", "V2SYM") in v2_seen
    # And crucially: neither input leaked to the OTHER warehouse.
    assert ("INDEX", "V2SYM") not in v1_seen
    assert ("INDEX", "V1SYM") not in v2_seen


# The cache key for a known all-v1 body. Per-leaf ``data_source`` must be OMITTED
# from the dump for a v1/unset leg (the conditional serializer), so the per-instrument
# field itself does NOT change the key: the ``body`` half is byte-identical to the
# pre-feature (checkpoint 02180c7) payload. The strategy-repro branch's own optional
# knobs (``cash_rate``/``delta_hedge`` per leg, ``normalize_weights`` top-level) are
# conditional-omitted the SAME way, so they too leave the ``body`` half byte-identical
# when unused. The hash below therefore differs from the ORIGINAL pre-feature capture
# ONLY through the ``_cv`` namespace, which has been bumped across releases while the
# body stayed fixed:
#   _cv 0.1.13 -> 54eaeb5509c8e2a1c83120942785b7f7c3194c1404e82f68c7bbea2f8c4e84d8
#   _cv 0.1.14 -> f950317e5d5addf63f07ea0cb6d8188aca0769df12ca0fe52eaa52e06d39bc0e
#   _cv 0.1.15 -> 188d1726eae7435e6e089f69e977453e47bf7b7a825f57e7e7fecfaea18d7907  (current)
# forcing the matching ``_cv`` reproduces each earlier value exactly (proven). The
# 0.1.14 -> 0.1.15 bump landed on main; this pin tracks the CURRENT COMPUTE_VERSION.
_PRE_FEATURE_ALL_V1_CACHE_KEY = (
    "188d1726eae7435e6e089f69e977453e47bf7b7a825f57e7e7fecfaea18d7907"
)


def _byte_identity_body() -> dict:
    return {
        "legs": {
            "a": {"type": "instrument", "collection": "INDEX", "symbol": "SPX"},
            "b": {
                "type": "continuous",
                "collection": "FUT_SP_500",
                "strategy": "front_month",
                "adjustment": "none",
            },
        },
        "weights": {"a": 60.0, "b": 40.0},
        "rebalance": "monthly",
        "return_type": "normal",
        "start": "2020-01-01",
        "end": "2024-12-31",
    }


def test_all_v1_body_dump_has_no_leg_data_source_and_matches_pre_feature_key():
    """Byte-identity: an all-v1 body's ``model_dump`` carries NO ``data_source``
    key on any leg (the conditional serializer omits it), so the per-instrument
    field never perturbs the cache key. The pinned hash tracks the current
    ``COMPUTE_VERSION`` namespace (the body half is byte-identical to pre-feature;
    only ``_cv`` differs — see ``_PRE_FEATURE_ALL_V1_CACHE_KEY``)."""
    pr = PortfolioRequest(**_byte_identity_body())
    dump = pr.model_dump(mode="json")
    for label, leg in dump["legs"].items():
        assert "data_source" not in leg, f"leg {label!r} leaked data_source: {leg}"
        # The strategy-repro branch's OPTIONAL leg fields must follow the same
        # conditional-omit discipline as ``data_source``: a default (None) value
        # must NOT appear, so an all-v1/legacy leg's dump stays byte-identical to
        # the pre-feature payload and its cache key is unperturbed.
        assert "cash_rate" not in leg, f"leg {label!r} leaked cash_rate: {leg}"
        assert "delta_hedge" not in leg, f"leg {label!r} leaked delta_hedge: {leg}"
    # ``normalize_weights`` (top level) defaults to True = pre-feature behaviour;
    # it too must be omitted when default so the body stays byte-identical.
    assert "normalize_weights" not in dump, f"body leaked normalize_weights: {dump}"
    assert _portfolio_cache_key(pr) == _PRE_FEATURE_ALL_V1_CACHE_KEY


def test_used_optional_leg_fields_appear_in_dump_and_change_the_key():
    """The over-omission guard, mirroring the v2 ``data_source`` case: a leg that
    actually USES one of the branch's optional fields (``cash_rate`` /
    ``delta_hedge``) — or a body that turns OFF ``normalize_weights`` — MUST
    serialise it and get a DIFFERENT cache key from the default body. Conditional
    omit drops only the default value, never a real one, so cache correctness is
    preserved (two compute-distinct bodies never share an entry)."""
    base = PortfolioRequest(**_byte_identity_body())
    base_key = _portfolio_cache_key(base)

    b1 = _byte_identity_body()
    b1["legs"]["a"]["cash_rate"] = {"kind": "flat", "rate_pct": 2.5}
    pr1 = PortfolioRequest(**b1)
    assert pr1.model_dump(mode="json")["legs"]["a"]["cash_rate"] is not None
    assert _portfolio_cache_key(pr1) != base_key

    b2 = _byte_identity_body()
    b2["legs"]["a"]["delta_hedge"] = {"enabled": True, "factor": 0.5}
    pr2 = PortfolioRequest(**b2)
    assert pr2.model_dump(mode="json")["legs"]["a"]["delta_hedge"] is not None
    assert _portfolio_cache_key(pr2) != base_key

    b3 = _byte_identity_body()
    b3["normalize_weights"] = False
    pr3 = PortfolioRequest(**b3)
    assert pr3.model_dump(mode="json")["normalize_weights"] is False
    assert _portfolio_cache_key(pr3) != base_key


def test_v2_leg_keeps_data_source_in_dump_and_changes_the_key():
    """A v2 leaf DOES appear in the dump (and thus the cache key) — a v1 and a v2
    variant of the same body must never share a cache entry."""
    v1 = PortfolioRequest(**_byte_identity_body())
    body_v2 = _byte_identity_body()
    body_v2["legs"]["a"]["data_source"] = "v2"
    v2 = PortfolioRequest(**body_v2)
    assert v2.model_dump(mode="json")["legs"]["a"]["data_source"] == "v2"
    assert _portfolio_cache_key(v1) != _portfolio_cache_key(v2)


def test_strip_use_cache_never_touches_data_source():
    """``_strip_use_cache`` strips ONLY ``use_cache`` — a v2 leg's ``data_source``
    survives into the cache key (it is compute-affecting)."""
    body_v2 = _byte_identity_body()
    body_v2["legs"]["a"]["data_source"] = "v2"
    pr = PortfolioRequest(**body_v2)
    stripped = portfolio._strip_use_cache(pr.model_dump(mode="json"))
    assert "use_cache" not in stripped
    assert stripped["legs"]["a"]["data_source"] == "v2"


# --------------------------------------------------------------------------- #
# 12. Multi-source calendar merge (NON-identical grids)                        #
#                                                                              #
#     The batched instrument fetch is split by source, each service returns    #
#     its own date grid, and the route intersects the grids + reindexes every  #
#     ``aligned_series`` onto the shared grid (``np.isin`` mask). Every OTHER   #
#     test in this file returns the SAME ``DATES`` from every service, so the   #
#     intersection is trivial and the reindex is a no-op — the realistic case   #
#     (v1 and v2 histories starting/ending on different dates) had never run    #
#     end-to-end. These tests drive DISTINCT grids with known close values so   #
#     the merged equity is predictable to the boundary bar.                     #
# --------------------------------------------------------------------------- #


def _series_on(grid: list[int], closes: list[float]) -> PriceSeries:
    """A ``PriceSeries`` on an ARBITRARY grid with the given close values.

    open/high/low/volume are given distinct offsets from ``close`` so a merge
    that masked the wrong array (or a different mask per field) would surface as
    a corrupted OHLC row, not just a wrong close.
    """
    c = np.asarray(closes, dtype=np.float64)
    g = np.asarray(grid, dtype=np.int64)
    assert len(c) == len(g)
    return PriceSeries(
        dates=g,
        open=c - 1.0,
        high=c + 1.0,
        low=c - 2.0,
        close=c,
        volume=np.full(len(g), 1000.0, dtype=np.float64),
    )


def _set_calendar(svc: MagicMock, grid: list[int], closes: list[float]) -> None:
    """Rebind a stub's ``get_aligned_prices`` to return ITS OWN grid/closes.

    Records the leg-label set into the stub's existing ``seen_aligned`` so the
    split (each service asked for ONLY its own leg) is asserted precisely.
    """
    g = np.asarray(grid, dtype=np.int64)

    async def _aligned(legs_spec):
        svc.seen_aligned.append(set(legs_spec))
        return g, {label: _series_on(grid, closes) for label in legs_spec}

    svc.get_aligned_prices = AsyncMock(side_effect=_aligned)


# v1 covers 2020..2024-12 (six bars, two of them BEFORE v2 starts); v2 covers
# 2022-06..2025-01 (five bars, one AFTER v1 ends). The dates present in BOTH are
# the four in the middle — so BOTH masks are non-trivial (v1 drops its two
# leading bars, v2 drops its one trailing bar).
_V1_GRID = [20200101, 20210101, 20220601, 20230601, 20240601, 20241201]
_V1_CLOSES = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
_V2_GRID = [20220601, 20230601, 20240601, 20241201, 20250101]
_V2_CLOSES = [100.0, 200.0, 400.0, 800.0, 1600.0]
_INTERSECTION_ISO = ["2022-06-01", "2023-06-01", "2024-06-01", "2024-12-01"]


async def test_multi_source_merge_intersects_calendars_and_reindexes(client, services):
    """v1 and v2 legs on DIFFERENT (partially overlapping) grids merge onto the
    intersection, with every aligned series correctly reindexed.

    Leg ``a`` is a v1 INSTRUMENT leg, leg ``b`` a v2 CONTINUOUS leg — both flow
    through the split fetch. Closes are chosen so the buy-and-hold, equal-weight
    equity is exact:

      intersection closes   a=[30,40,50,60]      b=[100,200,400,800]
      leg equity (w=0.5)    a=50·ratio           b=50·ratio
                            a=[50,66.67,83.33,100]  b=[50,100,200,400]
      portfolio = a+b       [100,166.67,283.33,500]

    A leg silently dropped, an off-by-one in the mask, or NaN leakage would each
    move one of these boundary numbers.
    """
    _set_calendar(services["v1"], _V1_GRID, _V1_CLOSES)
    _set_calendar(services["v2"], _V2_GRID, _V2_CLOSES)
    # The v2 leg is continuous → §9.5 re-surfaces roll boundaries via
    # get_continuous. It is display-only (equity is already built), but it runs
    # OUTSIDE a try/except when it returns, so hand it a real (empty) roll set.
    services["v2"].get_continuous = AsyncMock(
        return_value=SimpleNamespace(roll_dates=np.array([], dtype=np.int64))
    )

    body = {
        "legs": {
            "a": {
                "type": "instrument",
                "collection": "INDEX",
                "symbol": "SPX",
                "data_source": "v1",
            },
            "b": {
                "type": "continuous",
                "collection": "FUT_SP_500",
                "strategy": "front_month",
                "adjustment": "none",
                "data_source": "v2",
            },
        },
        "weights": {"a": 50.0, "b": 50.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2019-01-01",  # wide enough to NOT clip the intersection
        "end": "2025-12-31",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # ── The curve spans the INTERSECTION: later of the two starts, shared end ──
    assert data["dates"] == _INTERSECTION_ISO
    assert data["date_range"] == {"start": "2022-06-01", "end": "2024-12-01"}

    # ── Each source was hit EXACTLY once, each with ONLY its own leg ──
    assert services["v1"].get_aligned_prices.await_count == 1
    assert services["v2"].get_aligned_prices.await_count == 1
    assert services["v1"].seen_aligned == [{"a"}]
    assert services["v2"].seen_aligned == [{"b"}]

    # ── No leg silently dropped: BOTH contribute to the merged equity ──
    assert set(data["leg_equities"]) == {"a", "b"}
    assert len(data["leg_equities"]["a"]) == 4
    assert len(data["leg_equities"]["b"]) == 4

    # ── Correct reindex onto the intersected grid (exact, every bar) ──
    # v1's leading [10,20] and v2's trailing [1600] were dropped; the surviving
    # subsets are a=[30,40,50,60] and b=[100,200,400,800].
    assert data["leg_equities"]["a"] == pytest.approx([50.0, 200 / 3, 250 / 3, 100.0])
    assert data["leg_equities"]["b"] == pytest.approx([50.0, 100.0, 200.0, 400.0])
    assert data["portfolio_equity"] == pytest.approx(
        [100.0, 500 / 3, 850 / 3, 500.0]
    )
    # The end value pins BOTH legs at once: a-only would end 200, b-only 800,
    # b-dropped 200, a-dropped 800 — only both-present gives 500.
    assert data["portfolio_equity"][-1] == pytest.approx(500.0)

    # ── No NaN leakage: every emitted value is a finite JSON number ──
    for series in (
        data["portfolio_equity"],
        data["leg_equities"]["a"],
        data["leg_equities"]["b"],
    ):
        assert all(isinstance(v, float) and np.isfinite(v) for v in series)


async def test_multi_source_zero_overlap_is_a_clean_400_not_a_crash(client, services):
    """Disjoint v1/v2 calendars → the intersection is EMPTY.

    Documented behaviour: the route raises ``ValidationError`` → HTTP 400 with an
    actionable message, NOT a 500 and NOT a silently-empty/corrupt curve. (This
    is the guard at ``portfolio.py`` ``full_common_dates.size == 0`` inside the
    multi-source branch, which fires before any equity is built.)
    """
    _set_calendar(services["v1"], [20200101, 20210101], [10.0, 20.0])
    _set_calendar(services["v2"], [20240101, 20250101], [100.0, 200.0])

    body = {
        "legs": {
            "a": {
                "type": "instrument",
                "collection": "INDEX",
                "symbol": "SPX",
                "data_source": "v1",
            },
            "b": {
                "type": "instrument",
                "collection": "INDEX",
                "symbol": "SPX",
                "data_source": "v2",
            },
        },
        "weights": {"a": 50.0, "b": 50.0},
        "rebalance": "none",
        "return_type": "normal",
        "start": "2019-01-01",
        "end": "2025-12-31",
    }
    resp = await client.post("/api/portfolio/compute", json=body)
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    # Actionable and specific to the multi-source case (not the generic
    # all-legs-disjoint message emitted later at §5).
    assert "No overlapping dates" in message
    assert "data source" in message
    # Both services were still consulted (the split ran); the failure is the
    # merge, not a dropped leg.
    assert services["v1"].get_aligned_prices.await_count == 1
    assert services["v2"].get_aligned_prices.await_count == 1
