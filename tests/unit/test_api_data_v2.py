"""Router-level tests for /api/data-v2 (mocked service, no live dwh).

Verifies endpoint shapes, route ordering (/continuous/* and /series/* are not
captured by /objects/{object_id}), and that a delta criterion surfaces a 400.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.api.data_v2 import (
    _FREQ_VALUES,
    _OPTION_TYPE_VALUES,
    _SERIE_TYPE_VALUES,
)
from tcg.core.app import create_app
from tcg.data._sql.instruments_v2 import SqlInstrumentReaderV2
from tcg.types.errors import ValidationError
from tcg.types.market import (
    AdjustmentMethod,
    ContinuousRollConfig,
    ContinuousSeries,
    OptionsContinuousV2,
    PriceSeries,
    RollStrategy,
)


def _continuous():
    prices = PriceSeries(
        dates=np.array([20240102, 20240103], dtype=np.int64),
        open=np.array([10.0, 11.0]),
        high=np.array([10.5, 11.5]),
        low=np.array([9.5, 10.5]),
        close=np.array([10.2, 11.2]),
        volume=np.array([100.0, 200.0]),
    )
    return ContinuousSeries(
        collection="FUT_SP_500",
        roll_config=ContinuousRollConfig(
            strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.NONE
        ),
        prices=prices,
        roll_dates=(20240103,),
        contracts=("ESH4", "ESM4"),
    )


@pytest.fixture
async def client():
    app = create_app()
    mock = AsyncMock()
    mock.list_objects = AsyncMock(
        return_value=[
            {
                "object_id": 6,
                "kind": "future",
                "symbol": "FUT_SP_500",
                "name": "S&P 500 E-mini",
                "cycle": "quarterly",
                "underlying_object_id": 5,
            }
        ]
    )
    mock.get_object_detail = AsyncMock(
        return_value={
            "object": {"object_id": 6, "kind": "future", "symbol": "FUT_SP_500"},
            "contracts": [
                {
                    "contract_id": 87,
                    "contract_code": "ESM0.20100618",
                    "expiration": "2010-06-18",
                    "strike": None,
                    "option_type": None,
                    "multiplier": 50.0,
                }
            ],
            "series": [
                {
                    "serie_id": 76,
                    "contract_id": 71,
                    "type": "bar",
                    "freq": "daily",
                    "source": "DATABENTO",
                }
            ],
        }
    )
    mock.get_object_facets = AsyncMock(
        return_value={
            "object_id": 12,
            "kind": "option",
            "expirations": [{"expiration": "2026-09-11", "contracts": 146}],
            "strike_min": 15.0,
            "strike_max": 10600.0,
            "option_types": ["call", "put"],
            "serie_types": [{"type": "bbba", "freq": "1m", "series": 96106}],
            "totals": {"contracts": 96106, "series": 200672},
        }
    )
    mock.list_object_series = AsyncMock(
        return_value={
            "items": [
                {
                    "serie_id": 1433194,
                    "contract_id": 77,
                    "type": "bbba",
                    "freq": "1m",
                    "source": "DATABENTO:GLBX.MDP3:bbo-1m",
                    "contract_code": "EW2H6 P6260.20260313",
                    "expiration": "2026-03-13",
                    "strike": 6260.0,
                    "option_type": "put",
                }
            ],
            "total": 195,
            "skip": 0,
            "limit": 50,
        }
    )
    mock.get_series = AsyncMock(
        return_value={
            "serie_id": 5,
            "type": "bar",
            "fields": ["open", "high", "low", "close", "volume", "open_interest"],
            "points": {"ts": [20240102], "close": [42.0]},
        }
    )
    mock.get_continuous_future = AsyncMock(return_value=_continuous())
    mock.get_future_cycles = AsyncMock(return_value=["quarterly"])
    mock.get_continuous_options = AsyncMock(
        return_value=OptionsContinuousV2(
            object_id=7,
            criterion="strike",
            option_type="put",
            dates=(20240618,),
            values=(0.25,),
            roll_dates=(),
            contracts=("EW3M4 P5000.20240621",),
            contract_codes=("EW3M4 P5000.20240621",),
        )
    )
    app.state.market_data_v2 = mock

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_list_objects(client):
    resp = await client.get("/api/data-v2/objects")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["symbol"] == "FUT_SP_500"
    assert body[0]["underlying_object_id"] == 5


async def test_object_detail(client):
    resp = await client.get("/api/data-v2/objects/6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"]["symbol"] == "FUT_SP_500"
    assert body["contracts"][0]["multiplier"] == 50.0


async def test_facets_route_returns_dimensions(client):
    res = await client.get("/api/data-v2/objects/12/facets")
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "option"
    assert body["totals"]["series"] == 200672
    assert body["expirations"][0]["contracts"] == 146
    # No response_model on this route, deliberately: every facet key the filter
    # form reads must reach the client, not a silently-filtered subset.
    assert set(body) == {
        "object_id",
        "kind",
        "expirations",
        "strike_min",
        "strike_max",
        "option_types",
        "serie_types",
        "totals",
    }


async def test_facets_route_dispatches_to_the_facets_handler(client):
    """``/objects/{id}/facets`` must reach the facets handler, not ``/objects/{id}``.

    Asserting only "200 + a body key" would NOT discriminate declaration order:
    the catch-all compiles to ``objects/(?P<object_id>[^/]+)$``, and ``[^/]+``
    cannot span the ``/facets`` segment, so the catch-all can never match this
    path however it is ordered (verified by moving the route below it — the naive
    assertion stayed green). What a capture WOULD change is which service call the
    request lands on, so that is what is pinned here: it goes red if the catch-all
    is ever widened (e.g. to ``{object_id:path}``) and left declared first, and
    red today if facets were routed through ``get_object_detail``.
    """
    svc = client._transport.app.state.market_data_v2  # type: ignore[attr-defined]
    res = await client.get("/api/data-v2/objects/12/facets")
    assert res.status_code == 200
    assert "expirations" in res.json()
    # Called with a parsed int (not the raw "12"), and the id route never ran.
    svc.get_object_facets.assert_awaited_once_with(12)
    svc.get_object_detail.assert_not_awaited()


async def test_series_list_route_returns_paginated_shape(client):
    res = await client.get("/api/data-v2/objects/12/series?serie_type=bbba&freq=1m")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 195
    assert body["limit"] == 50
    assert body["items"][0]["option_type"] == "put"
    # No response_model, deliberately: the whole page shape reaches the client.
    assert set(body) == {"items", "total", "skip", "limit"}


async def test_series_list_route_dispatches_and_forwards_filters(client):
    """Pins the handler the request reaches AND the kwargs it forwards.

    Declaration order relative to ``/objects/{object_id}`` is NOT load-bearing
    for a two-segment path (``[^/]+`` cannot span the ``/series`` segment — see
    ``test_facets_route_dispatches_to_the_facets_handler``), so a "200 + a body
    key" assertion would not discriminate placement at all. What IS worth
    pinning is that every query param lands on the service unaltered and parsed:
    dates as ``date``, numbers as ``float``, ``object_id`` as ``int``.
    """
    svc = client._transport.app.state.market_data_v2  # type: ignore[attr-defined]
    res = await client.get(
        "/api/data-v2/objects/12/series"
        "?expiration_min=2026-03-01&expiration_max=2026-03-31"
        "&strike_min=6000&strike_max=7000"
        "&option_type=put&serie_type=bbba&freq=1m&skip=50&limit=100"
    )
    assert res.status_code == 200
    svc.list_object_series.assert_awaited_once_with(
        12,
        expiration_min=date(2026, 3, 1),
        expiration_max=date(2026, 3, 31),
        strike_min=6000.0,
        strike_max=7000.0,
        option_type="put",
        serie_type="bbba",
        freq="1m",
        skip=50,
        limit=100,
    )
    svc.get_object_detail.assert_not_awaited()


async def test_series_list_route_defaults_are_permissive(client):
    """With no query params the page must be unfiltered, not accidentally narrow."""
    svc = client._transport.app.state.market_data_v2  # type: ignore[attr-defined]
    res = await client.get("/api/data-v2/objects/12/series")
    assert res.status_code == 200
    svc.list_object_series.assert_awaited_once_with(
        12,
        expiration_min=None,
        expiration_max=None,
        strike_min=None,
        strike_max=None,
        option_type="both",
        serie_type="any",
        freq="any",
        skip=0,
        limit=50,
    )


async def test_series_list_route_rejects_inverted_strike_range(client):
    res = await client.get(
        "/api/data-v2/objects/12/series?strike_min=7000&strike_max=6000"
    )
    assert res.status_code == 400
    assert "strike_min" in res.json()["message"]


async def test_series_list_route_rejects_inverted_expiration_range(client):
    res = await client.get(
        "/api/data-v2/objects/12/series"
        "?expiration_min=2026-03-31&expiration_max=2026-03-01"
    )
    assert res.status_code == 400
    assert "expiration_min" in res.json()["message"]


async def test_series_list_route_rejects_unknown_serie_type(client):
    res = await client.get("/api/data-v2/objects/12/series?serie_type=nope")
    assert res.status_code == 400
    assert "serie_type" in res.json()["message"]


@pytest.mark.parametrize("param", ["freq", "option_type"])
async def test_series_list_route_rejects_unknown_enum(client, param):
    res = await client.get(f"/api/data-v2/objects/12/series?{param}=nope")
    assert res.status_code == 400
    assert param in res.json()["message"]


async def test_series_list_route_rejects_unparseable_expiration(client):
    res = await client.get("/api/data-v2/objects/12/series?expiration_min=not-a-date")
    assert res.status_code == 400
    assert "Invalid date format" in res.json()["message"]


async def test_series_list_route_caps_limit_at_500(client):
    """The cap is 500 exactly — the same one v1 uses (``tcg/core/api/data.py``).

    Both halves are needed. ``limit=5000`` rejected alone passes for ANY cap
    below 5000; ``limit=500`` accepted alone passes with no cap whatsoever. The
    501/500 pair is what pins the boundary at 500 rather than at 100 or 1000.

    The status is 400, not FastAPI's default 422: this app installs a
    ``RequestValidationError`` handler (``tcg/core/app.py``) that remaps query
    validation into the project envelope so the frontend can read
    ``body.message``. The message is asserted too — a bare ``== 400`` would also
    be satisfied by, say, an unrelated enum rejection.
    """
    over = await client.get("/api/data-v2/objects/12/series?limit=5000")
    assert over.status_code == 400
    assert "limit" in over.json()["message"]
    assert over.json()["error_type"] == "validation_error"

    boundary = await client.get("/api/data-v2/objects/12/series?limit=501")
    assert boundary.status_code == 400
    assert "limit" in boundary.json()["message"]

    ok = await client.get("/api/data-v2/objects/12/series?limit=500")
    assert ok.status_code == 200
    assert ok.json()["total"] == 195


BASE = "/api/data-v2/objects/12/series"


async def test_series_list_route_rejects_out_of_range_paging(client):
    """``skip >= 0`` and ``limit >= 1``. Same 400 remapping as the cap above."""
    neg_skip = await client.get("/api/data-v2/objects/12/series?skip=-1")
    assert neg_skip.status_code == 400
    assert "skip" in neg_skip.json()["message"]
    assert (await client.get(BASE + "?skip=0")).status_code == 200

    zero_limit = await client.get("/api/data-v2/objects/12/series?limit=0")
    assert zero_limit.status_code == 400
    assert "limit" in zero_limit.json()["message"]
    assert (await client.get(BASE + "?limit=1")).status_code == 200


def test_route_enum_whitelists_match_the_readers():
    """The route's 400-guard domains must not drift from the reader's whitelist.

    They are duplicated on purpose — the route wants a stable, ordered error
    message and must not import a frozenset's arbitrary iteration order — so the
    duplication needs a pin. Without it, adding a fifth ``FACT_DISPATCH`` type
    would give a 400 at the boundary for a value the adapter accepts.
    """
    assert set(_SERIE_TYPE_VALUES) == SqlInstrumentReaderV2._SERIE_TYPES
    assert set(_FREQ_VALUES) == SqlInstrumentReaderV2._FREQS
    assert set(_OPTION_TYPE_VALUES) == SqlInstrumentReaderV2._OPTION_TYPES


async def test_series_route_not_captured_by_object_id(client):
    # /series/{id} must resolve to the series handler, not /objects/{id}.
    resp = await client.get("/api/data-v2/series/5")
    assert resp.status_code == 200
    assert resp.json()["type"] == "bar"


async def test_continuous_futures_route_ordering(client):
    # /continuous/futures/{id} and its /cycles sub-route resolve correctly.
    resp = await client.get("/api/data-v2/continuous/futures/6")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contracts"] == ["ESH4", "ESM4"]
    assert body["prices"]["close"] == [10.2, 11.2]
    assert body["close"] == [10.2, 11.2]  # flat mirror

    resp2 = await client.get("/api/data-v2/continuous/futures/6/cycles")
    assert resp2.status_code == 200
    assert resp2.json()["cycles"] == ["quarterly"]


async def test_continuous_options_strike(client):
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "strike", "target": 5000, "option_type": "put"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["points"]["ts"] == [20240618]
    assert body["points"]["value"] == [0.25]
    # per-date contract codes serialized alongside ts/value (1:1)
    assert body["points"]["contract"] == ["EW3M4 P5000.20240621"]
    assert body["contracts"] == ["EW3M4 P5000.20240621"]


async def test_continuous_options_delta_returns_400(client):
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.market_data_v2.get_continuous_options = AsyncMock(
        side_effect=ValidationError("Delta unavailable: greeks empty in v2")
    )
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "delta", "target": 0.1, "option_type": "put"},
    )
    assert resp.status_code == 400
    assert resp.json()["error_type"] == "validation_error"


async def test_continuous_options_delta_without_target_returns_friendly_400(client):
    # criterion=delta with NO target must surface the friendly greeks-unavailable
    # 400 (not a generic 422 for a missing required query param).
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "delta", "option_type": "put"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert "greeks" in body["message"].lower()


async def test_continuous_options_missing_target_returns_400(client):
    # A non-delta criterion still requires target; omitting it is a friendly 400.
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "strike", "option_type": "put"},
    )
    assert resp.status_code == 400
    assert resp.json()["error_type"] == "validation_error"


async def test_continuous_options_bad_roll_returns_400(client):
    resp = await client.get(
        "/api/data-v2/continuous/options/7",
        params={"criterion": "strike", "target": 5000, "roll": "monthly"},
    )
    assert resp.status_code == 400
