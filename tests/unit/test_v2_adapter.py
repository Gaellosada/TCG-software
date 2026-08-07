"""Unit tests for V2MarketDataAdapter — gating, parity and delegation.

No live DB: every test here either exercises pure logic or a stubbed SQL layer.
The live reads are covered in ``tests/integration/data/test_v2_adapter_integration.py``.

The central property under test is **no silent fallthrough**: an unsupported
collection or instrument must RAISE, never quietly return v1 data or an empty
result. A portfolio that silently mixed sources produces a number attributable
to neither warehouse.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.data._v2_compat import _sql_v2
from tcg.data._v2_compat.adapter import V2MarketDataAdapter
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2FuturesContractUnavailable,
    V2InstrumentUnavailable,
    V2SymbolError,
)
from tcg.data.service import DefaultMarketDataService
from tcg.types.errors import DataNotFoundError
from tcg.types.market import (
    AdjustmentMethod,
    AssetClass,
    ContinuousRollConfig,
    InstrumentId,
    PriceSeries,
    RollStrategy,
)

UNSUPPORTED = [
    "ETF",
    "FUND",
    "FOREX",
    "FUT_VIX",
    "FUT_BTC",
    "FUT_NASDAQ_100",
    "OPT_VIX",
    "OPT_BTC",
    "nonsense",
]


@pytest.fixture
def adapter():
    """Adapter over a pool that would explode if any test actually used it."""
    return V2MarketDataAdapter(dwh_pool=None)  # type: ignore[arg-type]


def _series(dates: list[int], closes: list[float]) -> PriceSeries:
    n = len(dates)
    return PriceSeries(
        dates=np.array(dates, dtype=np.int64),
        open=np.full(n, np.nan),
        high=np.full(n, np.nan),
        low=np.full(n, np.nan),
        close=np.array(closes, dtype=np.float64),
        volume=np.full(n, np.nan),
    )


# --- asset_class_for parity with v1 ----------------------------------------- #


@pytest.mark.parametrize(
    "collection",
    [
        "INDEX",
        "FUT_SP_500",
        "FUT_VIX",
        "FUT_BTC",
        "ETF",
        "FUND",
        "FOREX",
        "OPT_SP_500",
        "OPT_VIX",
        "unknown",
        "",
    ],
)
def test_asset_class_for_matches_v1_exactly(collection):
    """The portfolio router relies on this contract; it must not drift."""
    assert V2MarketDataAdapter.asset_class_for(
        collection
    ) == DefaultMarketDataService.asset_class_for(collection)


def test_asset_class_for_key_values():
    assert V2MarketDataAdapter.asset_class_for("INDEX") is AssetClass.INDEX
    assert V2MarketDataAdapter.asset_class_for("FUT_SP_500") is AssetClass.FUTURE
    assert V2MarketDataAdapter.asset_class_for("OPT_SP_500") is None


# --- Collection gating: never a silent fallthrough --------------------------- #


@pytest.mark.parametrize("collection", UNSUPPORTED)
async def test_get_prices_raises_on_unsupported_collection(adapter, collection):
    with pytest.raises(V2CollectionUnavailable):
        await adapter.get_prices(collection, "ANY")


@pytest.mark.parametrize("collection", UNSUPPORTED)
async def test_list_instruments_raises_on_unsupported_collection(adapter, collection):
    with pytest.raises(V2CollectionUnavailable):
        await adapter.list_instruments(collection)


@pytest.mark.parametrize("collection", ["FUT_VIX", "FUT_BTC", "FUT_NASDAQ_100"])
async def test_get_continuous_raises_on_unsupported_futures_root(adapter, collection):
    """A FUT_ prefix is not enough — the root itself must exist on v2."""
    cfg = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
    with pytest.raises(V2CollectionUnavailable):
        await adapter.get_continuous(collection, cfg)


async def test_get_continuous_rejects_non_futures_collection(adapter):
    """Mirrors v1: a non-FUT_ collection is a DataNotFoundError, not a v2 error."""
    cfg = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
    with pytest.raises(DataNotFoundError):
        await adapter.get_continuous("INDEX", cfg)


@pytest.mark.parametrize("collection", UNSUPPORTED)
async def test_futures_lookups_raise_on_unsupported_collection(adapter, collection):
    with pytest.raises(V2CollectionUnavailable):
        await adapter.find_futures_contract_by_expiration(collection, 20260619)
    with pytest.raises(V2CollectionUnavailable):
        await adapter.find_front_futures_contract_on_or_after(collection, 20260619)
    with pytest.raises(V2CollectionUnavailable):
        await adapter.list_futures_contract_meta(collection)
    with pytest.raises(V2CollectionUnavailable):
        await adapter.get_available_cycles(collection)


# --- Instrument gating within INDEX ------------------------------------------ #


@pytest.mark.parametrize(
    "symbol", ["IND_VIX", "IND_VIX_3M", "IND_VVIX", "IND_CBOE_WPUT", "SPX"]
)
async def test_get_prices_rejects_non_sp500_index_symbols(adapter, symbol):
    """v2's INDEX covers only IND_SP_500 (spec §11 E2)."""
    with pytest.raises(V2InstrumentUnavailable) as ei:
        await adapter.get_prices("INDEX", symbol)
    assert symbol in ei.value.message


async def test_get_prices_rejects_malformed_futures_symbol(adapter):
    with pytest.raises(V2SymbolError):
        await adapter.get_prices("FUT_SP_500", "FUT_VIX_20260619")


# --- Discovery --------------------------------------------------------------- #


async def test_list_collections(adapter):
    assert await adapter.list_collections() == ["INDEX", "FUT_SP_500", "RATE"]


async def test_list_collections_filtered_by_asset_class(adapter):
    # RATE carries no AssetClass (asset_class_for -> None), so a class-filtered
    # discovery never surfaces it — it is reachable only by explicit name.
    assert await adapter.list_collections(AssetClass.INDEX) == ["INDEX"]
    assert await adapter.list_collections(AssetClass.FUTURE) == ["FUT_SP_500"]
    assert await adapter.list_collections(AssetClass.EQUITY) == []


async def test_list_instruments_rate_returns_cmt_symbols(adapter):
    page = await adapter.list_instruments("RATE")
    assert [i.symbol for i in page.items] == ["RATE_US_CMT_1M"]
    assert page.items[0].collection == "RATE"


async def test_get_prices_rate_reads_value_series(adapter, monkeypatch):
    captured = {}

    async def fake_read_rate_values(pool, symbol, *, start=None, end=None):
        captured["symbol"] = symbol
        return _series([20070601, 20120601, 20230601], [4.80, 0.03, 5.30])

    monkeypatch.setattr(_sql_v2, "read_rate_values", fake_read_rate_values)
    ps = await adapter.get_prices("RATE", "RATE_US_CMT_1M")
    assert captured["symbol"] == "RATE_US_CMT_1M"
    assert ps is not None
    assert ps.close.tolist() == [4.80, 0.03, 5.30]  # PERCENT, verbatim


async def test_get_prices_rate_rejects_unknown_symbol(adapter):
    with pytest.raises(V2InstrumentUnavailable):
        await adapter.get_prices("RATE", "RATE_US_CMT_99Y")


async def test_list_collections_excludes_options(adapter):
    """Options are discovered via list_option_roots, mirroring v1."""
    assert "OPT_SP_500" not in await adapter.list_collections()


async def test_list_instruments_index_returns_only_sp500(adapter):
    page = await adapter.list_instruments("INDEX")
    assert page.total == 1
    assert page.items[0] == InstrumentId(
        symbol="IND_SP_500",
        asset_class=AssetClass.INDEX,
        collection="INDEX",
    )


async def test_list_instruments_futures_paginates(adapter, monkeypatch):
    expirations = [date(2026, 3, 20), date(2026, 6, 19), date(2026, 9, 18)]

    async def fake(pool):
        return expirations

    monkeypatch.setattr(_sql_v2, "list_futures_expirations", fake)
    page = await adapter.list_instruments("FUT_SP_500", skip=1, limit=1)
    assert page.total == 3
    assert page.skip == 1 and page.limit == 1
    assert [i.symbol for i in page.items] == ["FUT_SP_500_EMINI_20260619"]
    assert page.items[0].asset_class is AssetClass.FUTURE


async def test_available_cycles_mirrors_v1_empty_string(adapter):
    """Every v1 FUT_SP_500 contract stores '' — not v2's 'quarterly'."""
    assert await adapter.get_available_cycles("FUT_SP_500") == [""]


# --- Futures contract lookups ------------------------------------------------ #


@pytest.fixture
def adapter_with_expirations(adapter, monkeypatch):
    expirations = [date(2026, 3, 20), date(2026, 6, 19), date(2026, 9, 18)]

    async def fake(pool):
        return expirations

    monkeypatch.setattr(_sql_v2, "list_futures_expirations", fake)
    return adapter


async def test_find_contract_by_exact_expiration(adapter_with_expirations):
    got = await adapter_with_expirations.find_futures_contract_by_expiration(
        "FUT_SP_500", 20260619
    )
    assert got == "FUT_SP_500_EMINI_20260619"


async def test_find_contract_by_expiration_returns_none_when_absent(
    adapter_with_expirations,
):
    assert (
        await adapter_with_expirations.find_futures_contract_by_expiration(
            "FUT_SP_500", 20260620
        )
        is None
    )


async def test_find_front_contract_picks_nearest_on_or_after(
    adapter_with_expirations,
):
    got = await adapter_with_expirations.find_front_futures_contract_on_or_after(
        "FUT_SP_500", 20260401
    )
    assert got == "FUT_SP_500_EMINI_20260619"


async def test_find_front_contract_is_inclusive_of_the_date(
    adapter_with_expirations,
):
    got = await adapter_with_expirations.find_front_futures_contract_on_or_after(
        "FUT_SP_500", 20260619
    )
    assert got == "FUT_SP_500_EMINI_20260619"


async def test_find_front_contract_returns_none_past_the_curve(
    adapter_with_expirations,
):
    assert (
        await adapter_with_expirations.find_front_futures_contract_on_or_after(
            "FUT_SP_500", 20301231
        )
        is None
    )


# --- Caching ----------------------------------------------------------------- #


async def test_get_prices_caches_and_does_not_requery(adapter, monkeypatch):
    calls = []

    async def fake(pool, *, start=None, end=None):
        calls.append((start, end))
        return _series([20260101, 20260102], [1.0, 2.0])

    monkeypatch.setattr(_sql_v2, "read_index_prices", fake)
    a = await adapter.get_prices("INDEX", "IND_SP_500")
    b = await adapter.get_prices("INDEX", "IND_SP_500")
    assert len(calls) == 1
    assert a is b


async def test_cache_key_separates_date_windows(adapter, monkeypatch):
    calls = []

    async def fake(pool, *, start=None, end=None):
        calls.append((start, end))
        return _series([20260101], [1.0])

    monkeypatch.setattr(_sql_v2, "read_index_prices", fake)
    await adapter.get_prices("INDEX", "IND_SP_500", start=date(2026, 1, 1))
    await adapter.get_prices("INDEX", "IND_SP_500", start=date(2026, 2, 1))
    assert len(calls) == 2


# --- get_aligned_prices ------------------------------------------------------ #


async def test_aligned_prices_inner_joins_dates(adapter, monkeypatch):
    async def fake_index(pool, *, start=None, end=None):
        return _series([20260101, 20260102, 20260103], [1.0, 2.0, 3.0])

    async def fake_fut(pool, expiration_int, *, start=None, end=None):
        return _series([20260102, 20260103, 20260106], [10.0, 20.0, 30.0])

    monkeypatch.setattr(_sql_v2, "read_index_prices", fake_index)
    monkeypatch.setattr(_sql_v2, "read_futures_prices", fake_fut)

    dates, aligned = await adapter.get_aligned_prices(
        {
            "idx": InstrumentId("IND_SP_500", AssetClass.INDEX, "INDEX"),
            "fut": InstrumentId(
                "FUT_SP_500_EMINI_20260619", AssetClass.FUTURE, "FUT_SP_500"
            ),
        }
    )
    assert dates.tolist() == [20260102, 20260103]
    assert aligned["idx"].close.tolist() == [2.0, 3.0]
    assert aligned["fut"].close.tolist() == [10.0, 20.0]


async def test_aligned_prices_rejects_empty_legs(adapter):
    from tcg.types.errors import ValidationError

    with pytest.raises(ValidationError):
        await adapter.get_aligned_prices({})


# --- Continuous futures ------------------------------------------------------ #


async def test_get_continuous_uses_the_shared_roller(adapter, monkeypatch):
    """Roll output must come from the UNCHANGED ContinuousSeriesBuilder."""
    from tcg.types.market import ContractPriceData

    async def fake(pool):
        return [
            ContractPriceData(
                contract_id="FUT_SP_500_EMINI_20260320",
                expiration=20260320,
                expiration_cycle="",
                prices=_series([20260101, 20260102], [100.0, 101.0]),
            ),
            ContractPriceData(
                contract_id="FUT_SP_500_EMINI_20260619",
                expiration=20260619,
                expiration_cycle="",
                prices=_series([20260101, 20260102, 20260401], [99.0, 100.0, 105.0]),
            ),
        ]

    monkeypatch.setattr(_sql_v2, "read_futures_contracts", fake)
    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.NONE
    )
    result = await adapter.get_continuous("FUT_SP_500", cfg)
    assert result is not None
    assert result.collection == "FUT_SP_500"
    assert len(result.prices) > 0
    # Emits v1 symbols, never v2 contract_codes (decision D1).
    for c in result.contracts:
        assert c.startswith("FUT_SP_500_EMINI_")


async def test_get_continuous_returns_none_without_contracts(adapter, monkeypatch):
    async def fake(pool):
        return []

    monkeypatch.setattr(_sql_v2, "read_futures_contracts", fake)
    cfg = ContinuousRollConfig(strategy=RollStrategy.FRONT_MONTH)
    assert await adapter.get_continuous("FUT_SP_500", cfg) is None


# --- Options delegation ------------------------------------------------------ #


async def test_options_reader_raises_when_not_injected(adapter):
    """Better a named cause here than an AttributeError deep in the engine."""
    with pytest.raises(V2CollectionUnavailable):
        _ = adapter.options_reader


async def test_options_reader_returns_the_injected_reader():
    sentinel = object()
    a = V2MarketDataAdapter(dwh_pool=None, options_reader=sentinel)  # type: ignore[arg-type]
    assert a.options_reader is sentinel


async def test_option_passthroughs_delegate_to_the_reader():
    """Every option method must delegate, exactly as v1's service does."""

    class FakeReader:
        def __init__(self):
            self.calls = []

        async def get_contract(self, collection, contract_id):
            self.calls.append(("get_contract", collection, contract_id))
            return "series"

        async def list_roots(self):
            self.calls.append(("list_roots",))
            return ["root"]

        async def list_expirations(self, root):
            self.calls.append(("list_expirations", root))
            return []

        async def trade_date_coverage(self, root):
            self.calls.append(("trade_date_coverage", root))
            return (None, None)

        async def cycle_trade_date_span(self, root, start, end, cycle=None):
            self.calls.append(("cycle_trade_date_span", root, start, end, cycle))
            return (None, None)

        async def list_expirations_filtered(self, root, option_type=None, cycle=None):
            self.calls.append(("list_expirations_filtered", root, option_type, cycle))
            return []

        async def list_expirations_by_date(
            self, root, start, end, option_type=None, cycle=None, expiration_max=None
        ):
            self.calls.append(("list_expirations_by_date", root, cycle))
            return {}

        async def query_chain(
            self, root, d, type, exp_min, exp_max, strike_min=None, strike_max=None
        ):
            self.calls.append(("query_chain", root, type))
            return []

    reader = FakeReader()
    a = V2MarketDataAdapter(dwh_pool=None, options_reader=reader)  # type: ignore[arg-type]

    assert await a.get_option_contract("OPT_SP_500", "X") == "series"
    assert await a.list_option_roots() == ["root"]
    await a.list_option_expirations("OPT_SP_500")
    await a.option_trade_date_coverage("OPT_SP_500")
    await a.option_cycle_trade_date_span(
        "OPT_SP_500", date(2026, 1, 1), date(2026, 2, 1), cycle="W1 Friday"
    )
    await a.list_option_expirations_filtered("OPT_SP_500", cycle="W1 Friday")
    await a.list_option_expirations_by_date(
        "OPT_SP_500", date(2026, 1, 1), date(2026, 2, 1), cycle="W1 Friday"
    )
    await a.query_options_chain(
        "OPT_SP_500", date(2026, 1, 2), "P", date(2026, 1, 1), date(2026, 3, 1)
    )
    assert [c[0] for c in reader.calls] == [
        "get_contract",
        "list_roots",
        "list_expirations",
        "trade_date_coverage",
        "cycle_trade_date_span",
        "list_expirations_filtered",
        "list_expirations_by_date",
        "query_chain",
    ]


# --- Protocol conformance ----------------------------------------------------- #


def test_adapter_satisfies_the_market_data_service_protocol():
    """Every method v1's service exposes must exist on the adapter."""
    missing = [
        name
        for name in dir(DefaultMarketDataService)
        if not name.startswith("_") and not hasattr(V2MarketDataAdapter, name)
    ]
    assert missing == []


# --- M1: an unlisted futures expiration must be LOUD, not None --------------- #
#
# v1 and v2 do not agree on every ES expiration (live-verified 2026-07-27): v1
# lists 20260618/20270617 where v2 lists 20260619/20270618, plus 18 pre-2010
# contracts v2 never had. Keying futures identity on the expiration date made
# those symbols resolve to zero rows and answer ``None``. In a v1-vs-v2
# COMPARISON that reads as missing data or a strategy effect, not as an
# identity mismatch — so it must raise.


@pytest.fixture
def adapter_unlisted(adapter, monkeypatch):
    """v2 lists the 19th; the caller will ask for v1's 18th."""

    async def no_rows(pool, expiration_int, *, start=None, end=None):
        return None

    async def expirations(pool):
        return [date(2026, 3, 20), date(2026, 6, 19), date(2026, 9, 18)]

    monkeypatch.setattr(_sql_v2, "read_futures_prices", no_rows)
    monkeypatch.setattr(_sql_v2, "list_futures_expirations", expirations)
    return adapter


async def test_get_prices_raises_on_expiration_v2_does_not_list(adapter_unlisted):
    with pytest.raises(V2FuturesContractUnavailable) as exc:
        await adapter_unlisted.get_prices("FUT_SP_500", "FUT_SP_500_EMINI_20260618")

    msg = str(exc.value)
    # Names the symbol asked for...
    assert "FUT_SP_500_EMINI_20260618" in msg
    # ...and the nearest v2 expiration, so the one-day offset is legible.
    assert "20260619" in msg
    assert exc.value.nearest == 20260619
    # Reaches the API as 400, like its siblings — not 502.
    assert exc.value.error_type == "validation_error"


async def test_unlisted_expiration_error_is_an_instrument_unavailable(adapter_unlisted):
    """Existing handlers catching the base class keep working."""
    with pytest.raises(V2InstrumentUnavailable):
        await adapter_unlisted.get_prices("FUT_SP_500", "FUT_SP_500_EMINI_20260618")


async def test_empty_window_on_a_listed_contract_still_returns_none(
    adapter, monkeypatch
):
    """The narrow fix must not over-reject.

    A contract v2 DOES list, asked for a window it has no bars in, is an
    ordinary empty range — v1 returns None there and so must v2.
    """

    async def no_rows(pool, expiration_int, *, start=None, end=None):
        return None

    async def expirations(pool):
        return [date(2026, 6, 19)]

    monkeypatch.setattr(_sql_v2, "read_futures_prices", no_rows)
    monkeypatch.setattr(_sql_v2, "list_futures_expirations", expirations)

    got = await adapter.get_prices(
        "FUT_SP_500",
        "FUT_SP_500_EMINI_20260619",
        start=date(1999, 1, 1),
        end=date(1999, 12, 31),
    )
    assert got is None


async def test_dimension_is_not_queried_when_rows_exist(adapter, monkeypatch):
    """The guard is cold-path only: a successful read must not pay for it."""
    calls = 0

    async def rows(pool, expiration_int, *, start=None, end=None):
        return _series([20260101], [5000.0])

    async def expirations(pool):
        nonlocal calls
        calls += 1
        return [date(2026, 6, 19)]

    monkeypatch.setattr(_sql_v2, "read_futures_prices", rows)
    monkeypatch.setattr(_sql_v2, "list_futures_expirations", expirations)

    got = await adapter.get_prices("FUT_SP_500", "FUT_SP_500_EMINI_20260619")
    assert got is not None
    assert calls == 0
