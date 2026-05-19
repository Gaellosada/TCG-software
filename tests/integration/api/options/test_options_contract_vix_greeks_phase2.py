"""Phase-2 regression: contract-detail endpoint resolves VIX forward via FUT_VIX.

The chain endpoint resolver was fixed in Phase 2 (`_join.py`) to use the
matching FUT_VIX close as the Black-76 forward for OPT_VIX. The
contract-detail endpoint has its own batched underlying-price prefetch
(``_batch_underlying_prices`` in ``tcg.core.api.options``) that was still
using spot ``IND_VIX``; that meant the per-contract time series saw a
forward ~5 below the chain endpoint's forward (VIX in contango), wrongly
classified ITM-against-spot puts as ``deep_itm_degenerate``, and never
computed greeks on most days.

These tests pin the fix:
1. Monthly OPT_VIX → ``_batch_underlying_prices`` calls
   ``find_futures_contract_by_expiration("FUT_VIX", ...)`` and then
   ``get_prices("FUT_VIX", "FUT_VIX_<YYYYMMDD>", ...)``. Greeks compute.
2. Weekly OPT_VIX (no matching FUT_VIX) → returns empty lookup; the
   pricer surfaces ``missing_forward_vix_curve`` instead of a wrong
   forward.
3. Regression: ``get_prices`` is NEVER called with
   ``("INDEX", "IND_VIX", ...)`` for OPT_VIX — that's the bug we fixed.
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
# Fixtures (mirror tests/integration/api/options/test_options_chain_vix_greeks_phase2.py
# so the wiring is recognisable.)
# ---------------------------------------------------------------------------


def _vix_contract(expiration: date) -> OptionContractDoc:
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


def _row(target_date: date, mid: float) -> OptionDailyRow:
    # mid above intrinsic vs F=18.0 (K=15 put → intrinsic=0), so greeks compute.
    return OptionDailyRow(
        date=target_date,
        open=mid, high=mid, low=mid, close=mid,
        bid=mid - 0.05, ask=mid + 0.05,
        bid_size=10.0, ask_size=10.0,
        volume=100.0, open_interest=1000.0,
        mid=mid,
        iv_stored=None, delta_stored=None, gamma_stored=None,
        theta_stored=None, vega_stored=None,
        underlying_price_stored=None,
    )


def _price_series(dates_int: list[int], values: list[float]) -> PriceSeries:
    n = len(dates_int)
    return PriceSeries(
        dates=np.array(dates_int, dtype=np.int64),
        open=np.array(values),
        high=np.array(values),
        low=np.array(values),
        close=np.array(values),
        volume=np.zeros(n),
    )


async def _build_app(
    *,
    contract: OptionContractDoc,
    rows: tuple[OptionDailyRow, ...],
    fut_vix_id: str | None,
    fut_vix_prices: PriceSeries | None,
) -> tuple[AsyncClient, MagicMock]:
    """Wire a TestClient with a fully mocked MarketDataService.

    Returns (client, mock_svc) so tests can introspect the mock's call args.
    """
    app = create_app()

    # Minimal options-reader stub — only get_contract is exercised here.
    class _Reader:
        async def get_contract(self, _coll: str, _id: str) -> OptionContractSeries:
            return OptionContractSeries(contract=contract, rows=rows)

        async def query_chain(self, *a: Any, **kw: Any) -> list[Any]:
            return []

        async def list_roots(self) -> list[OptionRootInfo]:
            return []

        async def list_expirations(self, _root: str) -> list[date]:
            return []

    reader = _Reader()
    mock_svc = MagicMock()
    type(mock_svc).options_reader = PropertyMock(return_value=reader)

    async def _get_option_contract(c: str, i: str) -> OptionContractSeries:
        return await reader.get_contract(c, i)

    mock_svc.get_option_contract = AsyncMock(side_effect=_get_option_contract)
    # The contract endpoint sometimes calls list_option_roots / list_option_expirations
    # via Depends but not in the path we test; stub them harmlessly.
    mock_svc.list_option_roots = AsyncMock(return_value=[])
    mock_svc.list_option_expirations = AsyncMock(return_value=[])

    mock_svc.find_futures_contract_by_expiration = AsyncMock(return_value=fut_vix_id)
    # get_prices returns the fut series when asked for FUT_VIX; anything else None.
    async def _get_prices(coll: str, _id: str, **_kw: Any) -> PriceSeries | None:
        if coll == "FUT_VIX" and fut_vix_prices is not None:
            return fut_vix_prices
        return None
    mock_svc.get_prices = AsyncMock(side_effect=_get_prices)

    app.state.market_data = mock_svc
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), mock_svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_TRADE = date(2024, 3, 15)
_MONTHLY = date(2024, 4, 17)


@pytest.mark.asyncio
async def test_contract_vix_uses_fut_vix_not_ind_vix() -> None:
    """The fix: VIX contract-detail must resolve forward via FUT_VIX, not spot.

    Pins the bug where ``_batch_underlying_prices`` hardcoded
    ``collection, instrument_id = "INDEX", "IND_VIX"`` for VIX, causing
    the time-series view to use spot VIX (~14-15) while the chain view
    used the matching future (~20+). The asymmetry surfaced as
    ``missing_iv_deep_itm_degenerate`` on slightly-OTM-against-future
    puts that were ITM-against-spot.
    """
    contract = _vix_contract(_MONTHLY)
    row = _row(_TRADE, mid=0.5)  # K=15 put, F=18 → OTM, mid > 0 → computes
    trade_int = _TRADE.year * 10000 + _TRADE.month * 100 + _TRADE.day
    fut_series = _price_series([trade_int], [18.0])

    client, svc = await _build_app(
        contract=contract,
        rows=(row,),
        fut_vix_id="FUT_VIX_20240417",
        fut_vix_prices=fut_series,
    )
    async with client:
        resp = await client.get(
            "/api/options/contract/OPT_VIX/" + contract.contract_id,
            params={"compute_missing": "true"},
        )

    assert resp.status_code == 200, resp.text

    # 1. The FUT_VIX lookup happened, keyed by the option's expiration as an int.
    svc.find_futures_contract_by_expiration.assert_awaited_once_with(
        "FUT_VIX", 20240417
    )

    # 2. get_prices was called with FUT_VIX + the matching contract id.
    get_prices_calls = svc.get_prices.await_args_list
    fut_calls = [c for c in get_prices_calls if c.args[:2] == ("FUT_VIX", "FUT_VIX_20240417")]
    assert len(fut_calls) == 1, f"expected one FUT_VIX call, got {get_prices_calls}"

    # 3. CRITICAL REGRESSION: spot IND_VIX must NEVER be fetched for OPT_VIX.
    ind_vix_calls = [c for c in get_prices_calls if c.args[:2] == ("INDEX", "IND_VIX")]
    assert ind_vix_calls == [], (
        "contract-detail endpoint must NOT fall back to spot IND_VIX for OPT_VIX "
        "— Phase 2 requires the matching FUT_VIX close as forward. "
        f"Spurious calls: {ind_vix_calls}"
    )

    # 4. Greeks actually compute with the right forward.
    row_out = resp.json()["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        cr = row_out[greek]
        assert cr["source"] == "computed", (greek, cr)
        assert cr["value"] is not None, (greek, cr)
        assert cr["error_code"] is None, (greek, cr)


@pytest.mark.asyncio
async def test_contract_vix_weekly_no_fut_returns_no_underlying() -> None:
    """Weekly OPT_VIX (no matching FUT_VIX expiry) → batched lookup must
    return empty so the pricer gates the row, NOT fall back to spot.
    """
    weekly = date(2024, 4, 24)  # no monthly FUT_VIX for this Wednesday
    contract = _vix_contract(weekly)
    row = _row(_TRADE, mid=0.5)

    client, svc = await _build_app(
        contract=contract,
        rows=(row,),
        fut_vix_id=None,  # no matching future
        fut_vix_prices=None,
    )
    async with client:
        resp = await client.get(
            "/api/options/contract/OPT_VIX/" + contract.contract_id,
            params={"compute_missing": "true"},
        )

    assert resp.status_code == 200, resp.text

    # The lookup was attempted...
    svc.find_futures_contract_by_expiration.assert_awaited_once_with(
        "FUT_VIX", 20240424
    )
    # ...and nothing else was queried.
    assert svc.get_prices.await_count == 0, (
        "no future found → must not call get_prices (no fallback to spot)"
    )

    # Pricer surfaces missing_forward_vix_curve (handled by the engine gate).
    row_out = resp.json()["rows"][0]
    for greek in ("iv", "delta", "gamma", "theta", "vega"):
        cr = row_out[greek]
        assert cr["source"] == "missing", (greek, cr)
        assert cr["error_code"] == "missing_forward_vix_curve", (greek, cr)
