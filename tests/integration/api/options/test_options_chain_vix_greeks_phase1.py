"""Phase-1 integration test for VIX greeks stored-passthrough.

Hermetic — uses a stubbed ``MarketDataService`` + ``StubOptionsReader``
in the style of ``tests/unit/api/options/conftest.py``. The point of
living under ``tests/integration/api/options/`` is to verify the full
router → engine-chain → response-model pipeline (not just unit-level
data assembly) on the OPT_VIX path after the data-layer blanket was
lifted in Phase 1 of the VIX greeks rollout.

Two scenarios:

1. **Stored greeks present** → each row's iv/delta/gamma/theta/vega
   surfaces with ``source="stored"`` (no engine compute kicked in
   because ``compute_missing=false``).
2. **Stored greeks absent** → each greek surfaces with
   ``source="missing"`` and ``error_code is None`` — the engine
   compute path stayed dormant for the same reason. (When Phase 2
   lands and ``compute_missing=true`` is exercised, the engine gate
   will fill ``error_code`` for OPT_VIX until the FUT_VIX forward
   resolver is fixed — that is explicitly out of Phase-1 scope.)
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
# Synthetic fixtures (mirrors tests/unit/api/options/conftest.py)
# ---------------------------------------------------------------------------


def _make_vix_contract() -> OptionContractDoc:
    return OptionContractDoc(
        collection="OPT_VIX",
        contract_id="OPT_VIX_M_20240417_15_P|M",
        root_underlying="IND_VIX",
        underlying_ref=None,  # VIX has no FUT underlying ref on the contract
        underlying_symbol="VIX",
        expiration=date(2024, 4, 17),
        expiration_cycle="M",
        strike=15.0,
        type="P",
        contract_size=100.0,
        currency="USD",
        provider="CBOE",
        strike_factor_verified=True,
    )


def _make_row_with_stored_greeks() -> OptionDailyRow:
    return OptionDailyRow(
        date=date(2024, 3, 15),
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
        iv_stored=0.85,
        delta_stored=-0.42,
        gamma_stored=0.03,
        theta_stored=-0.08,
        vega_stored=0.04,
        underlying_price_stored=None,
    )


def _make_row_without_stored_greeks() -> OptionDailyRow:
    return OptionDailyRow(
        date=date(2024, 3, 15),
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


def _make_vix_index_price() -> PriceSeries:
    target_int = 20240315
    value = 15.5
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
    ) -> None:
        self._rows = rows

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
        limit: int | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        return self._rows

    async def list_roots(self) -> list[OptionRootInfo]:
        return [
            OptionRootInfo(
                collection="OPT_VIX",
                name="VIX",
                has_greeks=True,
                providers=("CBOE",),
                expiration_first=date(2006, 1, 1),
                expiration_last=date(2027, 12, 19),
                doc_count_estimated=500000,
                strike_factor_verified=True,
                last_trade_date=None,
            )
        ]

    async def list_expirations(self, root: str) -> list[date]:
        return [date(2024, 4, 17)]

    async def get_contract(
        self, collection: str, contract_id: str
    ) -> OptionContractSeries:
        raise NotImplementedError("not exercised by this test")


async def _build_client(
    rows: list[tuple[OptionContractDoc, OptionDailyRow]],
) -> AsyncClient:
    app = create_app()
    reader = _StubOptionsReader(rows)

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
    mock_svc.list_option_expirations = AsyncMock(side_effect=_list_option_expirations)
    mock_svc.get_prices = AsyncMock(return_value=_make_vix_index_price())

    app.state.market_data = mock_svc

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vix_chain_passes_through_stored_greeks():
    """OPT_VIX with stored CBOE greeks (Phase 1 unblock):
    each greek surfaces with ``source="stored"`` via the chain endpoint.
    """
    rows = [(_make_vix_contract(), _make_row_with_stored_greeks())]
    async with await _build_client(rows) as client:
        resp = await client.get(
            "/api/options/chain",
            params={
                "root": "OPT_VIX",
                "date": "2024-03-15",
                "type": "both",
                "expiration_min": "2024-03-15",
                "expiration_max": "2024-06-30",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["root"] == "OPT_VIX"
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    for greek, expected in (
        ("iv", 0.85),
        ("delta", -0.42),
        ("gamma", 0.03),
        ("theta", -0.08),
        ("vega", 0.04),
    ):
        cr = row[greek]
        assert cr["source"] == "stored", (greek, cr)
        assert cr["value"] == pytest.approx(expected), (greek, cr)
        assert cr["error_code"] is None, (greek, cr)


@pytest.mark.asyncio
async def test_vix_chain_missing_greeks_without_compute_have_no_engine_error():
    """OPT_VIX with no stored greeks AND ``compute_missing=false``:
    each greek surfaces with ``source="missing"`` and the engine
    block did NOT fire — ``error_code`` is the data-layer
    ``"not_stored"`` signal (set by ``widen_stored`` in
    ``tcg.engine.options.chain._widen``), never the engine gate's
    ``"missing_forward_vix_curve"``.

    The point of this assertion is to pin Phase-1 behaviour: lifting
    the data-layer blanket does not invite the engine compute path
    when callers opt out via ``compute_missing=false``. The engine
    gate stays untouched in Phase 1 and only fires under
    ``compute_missing=true`` (Phase 2 scope).
    """
    rows = [(_make_vix_contract(), _make_row_without_stored_greeks())]
    async with await _build_client(rows) as client:
        resp = await client.get(
            "/api/options/chain",
            params={
                "root": "OPT_VIX",
                "date": "2024-03-15",
                "type": "both",
                "expiration_min": "2024-03-15",
                "expiration_max": "2024-06-30",
                # compute_missing defaults to false; explicit for clarity.
                "compute_missing": "false",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        cr = row[greek]
        assert cr["source"] == "missing", (greek, cr)
        assert cr["value"] is None, (greek, cr)
        # Engine compute block (``missing_forward_vix_curve``) must NOT
        # have fired — only the benign data-layer ``not_stored`` signal.
        assert cr["error_code"] in (None, "not_stored"), (greek, cr)
        assert cr["error_code"] != "missing_forward_vix_curve", (greek, cr)
