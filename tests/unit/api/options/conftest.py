"""Shared fixtures for options router unit tests.

Builds a fully mocked ``MarketDataService`` that:
- exposes a ``StubOptionsReader`` via the public ``options_reader`` property
  (so ``_options_wiring.get_options_reader`` returns it),
- delegates the four Protocol methods to the same stub,
- mocks ``get_prices`` for the INDEX / FUT_* underlying joins.

All synthetic data — no Mongo, no real network calls.
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
# Synthetic fixtures
# ---------------------------------------------------------------------------


def make_contract(
    *,
    collection: str = "OPT_SP_500",
    contract_id: str = "SPX_C_5100_20240419|M",
    strike: float = 5100.0,
    type: str = "C",
    expiration: date = date(2024, 4, 19),
    underlying_ref: str | None = "FUT_SP_500_EMINI_20240621",
    root_underlying: str = "IND_SP_500",
    strike_factor_verified: bool = True,
    provider: str = "IVOLATILITY",
) -> OptionContractDoc:
    return OptionContractDoc(
        collection=collection,
        contract_id=contract_id,
        root_underlying=root_underlying,
        underlying_ref=underlying_ref,
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle="M",
        strike=strike,
        type=type,  # type: ignore[arg-type]
        contract_size=100.0,
        currency="USD",
        provider=provider,
        strike_factor_verified=strike_factor_verified,
    )


def make_row(
    *,
    row_date: date = date(2024, 3, 15),
    bid: float | None = 85.5,
    ask: float | None = 86.0,
    iv_stored: float | None = 0.155,
    delta_stored: float | None = 0.512,
    gamma_stored: float | None = 0.0021,
    theta_stored: float | None = -0.42,
    vega_stored: float | None = 6.31,
    underlying_price_stored: float | None = None,
) -> OptionDailyRow:
    mid: float | None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
    else:
        mid = None
    return OptionDailyRow(
        date=row_date,
        open=85.0,
        high=86.5,
        low=84.5,
        close=85.75,
        bid=bid,
        ask=ask,
        bid_size=10.0,
        ask_size=12.0,
        volume=1500.0,
        open_interest=12345.0,
        mid=mid,
        iv_stored=iv_stored,
        delta_stored=delta_stored,
        gamma_stored=gamma_stored,
        theta_stored=theta_stored,
        vega_stored=vega_stored,
        underlying_price_stored=underlying_price_stored,
    )


def make_root_info(collection: str = "OPT_SP_500") -> OptionRootInfo:
    return OptionRootInfo(
        collection=collection,
        name="SP 500",
        has_greeks=True,
        providers=("IVOLATILITY",),
        expiration_first=date(2005, 12, 16),
        expiration_last=date(2030, 12, 20),
        doc_count_estimated=417315,
        strike_factor_verified=True,
    )


def make_index_close_series(
    target_date: date = date(2024, 3, 15),
    value: float = 5117.94,
) -> PriceSeries:
    target_int = target_date.year * 10000 + target_date.month * 100 + target_date.day
    return PriceSeries(
        dates=np.array([target_int], dtype=np.int64),
        open=np.array([value]),
        high=np.array([value]),
        low=np.array([value]),
        close=np.array([value]),
        volume=np.array([0.0]),
    )


# ---------------------------------------------------------------------------
# Mock options reader
# ---------------------------------------------------------------------------


class StubOptionsReader:
    """Minimal stand-in for ``MongoOptionsDataReader``.

    Tests configure ``query_chain_result``, ``get_contract_result``, and
    ``list_roots_result`` to control the responses.  Side-effects can be
    set to raise ``OptionsDataAccessError`` etc.
    """

    def __init__(self) -> None:
        self.query_chain_result: list[tuple[OptionContractDoc, OptionDailyRow]] = []
        self.get_contract_result: OptionContractSeries | None = None
        self.list_roots_result: list[OptionRootInfo] = []
        self.list_expirations_result: list[date] = []
        self.query_chain_calls: list[dict[str, Any]] = []
        self.query_chain_side_effect: BaseException | None = None
        # Per-call overrides (1-based call index), used to distinguish the
        # selector's chain read from the /select premium-probe re-read (which
        # is a SECOND query_chain call). Empty by default → no effect.
        self.query_chain_side_effect_by_call: dict[int, BaseException] = {}
        self.query_chain_result_by_call: dict[
            int, list[tuple[OptionContractDoc, OptionDailyRow]]
        ] = {}
        self.get_contract_side_effect: BaseException | None = None
        self.list_roots_side_effect: BaseException | None = None
        self.list_expirations_side_effect: BaseException | None = None
        # Per-root available expiration_cycle tags, keyed by collection. Backs
        # the ``svc.get_available_cycles`` the /roots endpoint now calls to
        # attach ``cycles`` to each root. Default empty → ``cycles: []``.
        self.available_cycles_result: dict[str, list[str]] = {}
        # Per-collection cycle-query failures, keyed by collection. Lets a test
        # make ONE root's cycle metadata query raise (e.g. a transient
        # DataAccessError) to prove the /roots listing degrades that root to
        # ``cycles: []`` instead of failing the whole listing.
        self.available_cycles_side_effect: dict[str, BaseException] = {}

    async def available_cycles(self, collection: str) -> list[str]:
        exc = self.available_cycles_side_effect.get(collection)
        if exc is not None:
            raise exc
        return list(self.available_cycles_result.get(collection, []))

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
        self.query_chain_calls.append(
            {
                "root": root,
                "date": date,
                "type": type,
                "expiration_min": expiration_min,
                "expiration_max": expiration_max,
                "strike_min": strike_min,
                "strike_max": strike_max,
                "expiration_cycle": expiration_cycle,
            }
        )
        call_index = len(self.query_chain_calls)  # 1-based (append happened above)
        per_call_exc = self.query_chain_side_effect_by_call.get(call_index)
        if per_call_exc is not None:
            raise per_call_exc
        if self.query_chain_side_effect is not None:
            raise self.query_chain_side_effect
        result = self.query_chain_result_by_call.get(
            call_index, self.query_chain_result
        )
        # When the test sets `query_chain_result` to a fixed list and the
        # router supplies an expiration_cycle filter, mimic the
        # production behaviour by filtering the canned result. This lets
        # the router test exercise the full plumbing end-to-end while
        # keeping existing tests untouched.
        if expiration_cycle is not None:
            return [
                (c, r) for (c, r) in result if c.expiration_cycle == expiration_cycle
            ]
        return result

    async def get_contract(
        self,
        collection: str,
        contract_id: str,
    ) -> OptionContractSeries:
        if self.get_contract_side_effect is not None:
            raise self.get_contract_side_effect
        if self.get_contract_result is None:
            from tcg.types.errors import OptionsContractNotFound

            raise OptionsContractNotFound(
                f"stub: no contract {contract_id} in {collection}"
            )
        return self.get_contract_result

    async def list_roots(self) -> list[OptionRootInfo]:
        if self.list_roots_side_effect is not None:
            raise self.list_roots_side_effect
        return self.list_roots_result

    async def list_expirations(self, root: str) -> list[date]:
        if self.list_expirations_side_effect is not None:
            raise self.list_expirations_side_effect
        return self.list_expirations_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def options_reader() -> StubOptionsReader:
    return StubOptionsReader()


@pytest.fixture
async def client(options_reader: StubOptionsReader):
    """Build a TestClient with a mocked MarketDataService.

    The mock exposes ``options_reader`` (the wiring helper reads it via
    the public Protocol property) and forwards the four Protocol methods
    to the same stub.
    """
    app = create_app()

    mock_svc = MagicMock()
    type(mock_svc).options_reader = PropertyMock(return_value=options_reader)

    async def _list_option_roots() -> list[OptionRootInfo]:
        return await options_reader.list_roots()

    async def _list_option_expirations(root: str) -> list[date]:
        return await options_reader.list_expirations(root)

    async def _get_option_contract(
        collection: str, contract_id: str
    ) -> OptionContractSeries:
        return await options_reader.get_contract(collection, contract_id)

    async def _query_options_chain(*args: Any, **kwargs: Any):
        return await options_reader.query_chain(*args, **kwargs)

    async def _get_available_cycles(collection: str) -> list[str]:
        return await options_reader.available_cycles(collection)

    mock_svc.list_option_roots = AsyncMock(side_effect=_list_option_roots)
    mock_svc.list_option_expirations = AsyncMock(side_effect=_list_option_expirations)
    mock_svc.get_option_contract = AsyncMock(side_effect=_get_option_contract)
    mock_svc.query_options_chain = AsyncMock(side_effect=_query_options_chain)
    mock_svc.get_available_cycles = AsyncMock(side_effect=_get_available_cycles)

    # Default: get_prices returns the SP_500 index close.
    mock_svc.get_prices = AsyncMock(return_value=make_index_close_series())

    app.state.market_data = mock_svc

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_svc(client: AsyncClient):
    app = client._transport.app  # type: ignore[attr-defined]
    return app.state.market_data
