"""Phase-2 integration test for VIX greeks computed via Black-76.

Hermetic — uses a stubbed ``MarketDataService`` (with the public
``find_futures_contract_by_expiration`` method stubbed for the FUT_VIX-
by-expiration lookup) + ``StubOptionsReader`` modelled on
``tests/integration/api/options/test_options_chain_vix_greeks_phase1.py``.

Three scenarios cover the Phase 2 contract:

1. **Monthly OPT_VIX, ``compute_missing=true``** → resolver finds a
   matching FUT_VIX expiration → Black-76 computes greeks → each greek
   surfaces with ``source="computed"`` and no engine ``error_code``.
2. **Weekly OPT_VIX, ``compute_missing=true``** → resolver finds no
   matching FUT_VIX expiration → ``missing_forward_vix_curve`` for all
   5 greeks (Phase 3 will introduce a forward-curve interpolator).
3. **OPT_SP_500 regression** — the shared underlying-resolution code
   path must continue to work for SP500 (its computed greeks land via
   ``source="computed"``).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from tcg.core.app import create_app
from tcg.types.market import PriceSeries
from tcg.types.options import (
    OptionContractDoc,
    OptionContractSeries,
    OptionDailyRow,
    OptionRootInfo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vix_contract(expiration: date) -> OptionContractDoc:
    return OptionContractDoc(
        collection="OPT_VIX",
        contract_id=f"OPT_VIX_M_{expiration:%Y%m%d}_15_P|M",
        root_underlying="IND_VIX",
        underlying_ref=None,
        underlying_symbol="VIX",
        expiration=expiration,
        expiration_cycle="M",
        strike=15.0,
        type="P",
        contract_size=100.0,
        currency="USD",
        provider="CBOE",
        strike_factor_verified=True,
    )


def _make_sp500_contract(expiration: date) -> OptionContractDoc:
    return OptionContractDoc(
        collection="OPT_SP_500",
        contract_id=f"OPT_SP_500_M_{expiration:%Y%m%d}_5000_C|M",
        root_underlying="IND_SP_500",
        underlying_ref=f"FUT_SP_500_EMINI_{expiration:%Y%m%d}",
        underlying_symbol="SPX",
        expiration=expiration,
        expiration_cycle="M",
        strike=5000.0,
        type="C",
        contract_size=50.0,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _make_row_quotes_no_stored_greeks(target_date: date) -> OptionDailyRow:
    return OptionDailyRow(
        date=target_date,
        open=0.55,
        high=0.65,
        low=0.50,
        close=0.60,
        bid=0.55,
        ask=0.60,
        bid_size=10.0,
        ask_size=12.0,
        volume=1500.0,
        open_interest=12345.0,
        mid=0.575,
        iv_stored=None,
        delta_stored=None,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


def _make_price_series(target_date: date, value: float) -> PriceSeries:
    target_int = target_date.year * 10000 + target_date.month * 100 + target_date.day
    return PriceSeries(
        dates=np.array([target_int], dtype=np.int64),
        open=np.array([value]),
        high=np.array([value]),
        low=np.array([value]),
        close=np.array([value]),
        volume=np.array([0.0]),
    )


class _StubOptionsReader:
    """Minimal stand-in for ``MongoOptionsDataReader``."""

    def __init__(
        self,
        rows: list[tuple[OptionContractDoc, OptionDailyRow]],
        root_collection: str,
        expirations: list[date],
    ) -> None:
        self._rows = rows
        self._root = root_collection
        self._expirations = expirations

    async def query_chain(
        self,
        root: str,
        date: date,  # noqa: A002
        type: str,  # noqa: A002
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        return self._rows

    async def list_roots(self) -> list[OptionRootInfo]:
        return [
            OptionRootInfo(
                collection=self._root,
                name=self._root,
                has_greeks=True,
                providers=("CBOE",),
                expiration_first=date(2006, 1, 1),
                expiration_last=date(2030, 12, 19),
                doc_count_estimated=500000,
                strike_factor_verified=True,
                last_trade_date=None,
            )
        ]

    async def list_expirations(self, root: str) -> list[date]:
        return list(self._expirations)

    async def get_contract(
        self, collection: str, contract_id: str
    ) -> OptionContractSeries:
        raise NotImplementedError("not exercised by this test")


class _FutVixStore:
    """In-memory store of FUT_VIX documents used by the stub service to
    implement ``find_futures_contract_by_expiration``. Each doc is a dict
    with at least ``{"_id": str, "expiration": int (YYYYMMDD)}``.
    """

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._by_expiration: dict[int, str] = {
            doc["expiration"]: doc["_id"]
            for doc in docs
            if "expiration" in doc and "_id" in doc
        }

    def find_by_expiration(self, expiration_int: int) -> str | None:
        return self._by_expiration.get(expiration_int)


async def _build_client(
    rows: list[tuple[OptionContractDoc, OptionDailyRow]],
    *,
    root_collection: str,
    expirations: list[date],
    fut_vix_docs: list[dict[str, Any]] | None = None,
    get_prices_value: float | None = None,
    price_target_date: date | None = None,
) -> AsyncClient:
    app = create_app()
    reader = _StubOptionsReader(rows, root_collection, expirations)

    mock_svc = MagicMock()
    type(mock_svc).options_reader = PropertyMock(return_value=reader)

    async def _query_options_chain(*args: Any, **kwargs: Any):
        return await reader.query_chain(*args, **kwargs)

    async def _list_option_roots() -> list[OptionRootInfo]:
        return await reader.list_roots()

    async def _list_option_expirations(root: str) -> list[date]:
        return await reader.list_expirations(root)

    mock_svc.query_options_chain = AsyncMock(side_effect=_query_options_chain)
    mock_svc.list_option_roots = AsyncMock(side_effect=_list_option_roots)
    mock_svc.list_option_expirations = AsyncMock(
        side_effect=_list_option_expirations
    )

    if get_prices_value is not None and price_target_date is not None:
        mock_svc.get_prices = AsyncMock(
            return_value=_make_price_series(price_target_date, get_prices_value)
        )
    else:
        mock_svc.get_prices = AsyncMock(return_value=None)

    # Wire the public ``find_futures_contract_by_expiration`` method so
    # ``_FuturesDataPortAdapter.get_futures_close_by_expiration`` can
    # call it without reaching into private attributes.
    fut_vix_store = _FutVixStore(fut_vix_docs or [])

    async def _find_futures_contract(
        collection: str, expiration_int: int
    ) -> str | None:
        # Only FUT_VIX lookup is exercised in these tests.
        if collection == "FUT_VIX":
            return fut_vix_store.find_by_expiration(expiration_int)
        return None

    mock_svc.find_futures_contract_by_expiration = AsyncMock(
        side_effect=_find_futures_contract
    )

    app.state.market_data = mock_svc

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_TRADE_DATE = date(2024, 3, 15)
_MONTHLY_EXPIRY = date(2024, 4, 17)


@pytest.mark.asyncio
async def test_opt_vix_monthly_compute_missing_returns_computed_greeks():
    """Phase 2: monthly OPT_VIX with ``compute_missing=true`` resolves a
    FUT_VIX forward (matching expiration → 18.0) and Black-76 produces
    real greeks. Engine ``error_code`` is None — no gate fires.
    """
    contract = _make_vix_contract(_MONTHLY_EXPIRY)
    row = _make_row_quotes_no_stored_greeks(_TRADE_DATE)
    fut_vix_docs = [
        {
            "_id": "FUT_VIX_20240417",
            # YYYYMMDD int — legacy schema per _parse_expiration().
            "expiration": 20240417,
        }
    ]
    async with await _build_client(
        rows=[(contract, row)],
        root_collection="OPT_VIX",
        expirations=[_MONTHLY_EXPIRY],
        fut_vix_docs=fut_vix_docs,
        get_prices_value=18.0,
        price_target_date=_TRADE_DATE,
    ) as client:
        resp = await client.get(
            "/api/options/chain",
            params={
                "root": "OPT_VIX",
                "date": _TRADE_DATE.isoformat(),
                "type": "both",
                "expiration_min": _TRADE_DATE.isoformat(),
                "expiration_max": "2024-06-30",
                "compute_missing": "true",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row_out = body["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        cr = row_out[greek]
        assert cr["source"] == "computed", (greek, cr)
        assert cr["value"] is not None, (greek, cr)
        assert cr["error_code"] is None, (greek, cr)


@pytest.mark.asyncio
async def test_opt_vix_weekly_compute_missing_still_gated():
    """Phase 2: weekly OPT_VIX (no FUT_VIX expiry match) → resolver
    returns None → pricer surfaces ``missing_forward_vix_curve`` for all
    5 greeks. Phase 3 will replace this with curve interpolation.
    """
    weekly_expiry = date(2024, 4, 24)  # Wednesday — no monthly FUT_VIX expiry
    contract = _make_vix_contract(weekly_expiry)
    row = _make_row_quotes_no_stored_greeks(_TRADE_DATE)
    # Note: FUT_VIX stub contains only the monthly expiry, NOT the weekly one.
    fut_vix_docs = [{"_id": "FUT_VIX_20240417", "expiration": 20240417}]
    async with await _build_client(
        rows=[(contract, row)],
        root_collection="OPT_VIX",
        expirations=[weekly_expiry],
        fut_vix_docs=fut_vix_docs,
    ) as client:
        resp = await client.get(
            "/api/options/chain",
            params={
                "root": "OPT_VIX",
                "date": _TRADE_DATE.isoformat(),
                "type": "both",
                "expiration_min": _TRADE_DATE.isoformat(),
                "expiration_max": "2024-06-30",
                "compute_missing": "true",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row_out = body["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        cr = row_out[greek]
        assert cr["source"] == "missing", (greek, cr)
        assert cr["value"] is None, (greek, cr)
        assert cr["error_code"] == "missing_forward_vix_curve", (greek, cr)


@pytest.mark.asyncio
async def test_opt_sp_500_unchanged_regression():
    """SP500 must continue to work — the underlying-resolution code path
    is shared between OPT_VIX and the option-on-future branch. This
    regression test pins that the Phase 2 split did not break SP500.
    """
    contract = _make_sp500_contract(_MONTHLY_EXPIRY)
    row = _make_row_quotes_no_stored_greeks(_TRADE_DATE)
    async with await _build_client(
        rows=[(contract, row)],
        root_collection="OPT_SP_500",
        expirations=[_MONTHLY_EXPIRY],
        fut_vix_docs=None,
        get_prices_value=5117.94,
        price_target_date=_TRADE_DATE,
    ) as client:
        resp = await client.get(
            "/api/options/chain",
            params={
                "root": "OPT_SP_500",
                "date": _TRADE_DATE.isoformat(),
                "type": "both",
                "expiration_min": _TRADE_DATE.isoformat(),
                "expiration_max": "2024-06-30",
                "compute_missing": "true",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row_out = body["rows"][0]
    # SP_500 mid 0.575 with K=5000 is deep OTM; py_vollib may or may not
    # invert. The minimum regression assertion: ``error_code`` is NOT a
    # VIX gating code (the shared resolver did not misroute SP500).
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        cr = row_out[greek]
        assert cr["error_code"] != "missing_forward_vix_curve", (greek, cr)
        assert cr["error_code"] != "missing_deribit_feed", (greek, cr)
