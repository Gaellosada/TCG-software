"""Live-dwh integration tests for V2MarketDataAdapter.

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by the
``DWH_*`` variables being present (``load_dwh_config`` raises otherwise → skip).
Reads ``tcg_instruments_v2`` READ-ONLY via ``tcg_read``.

Verifies against the real warehouse:
  * the index series is non-empty, date-monotonic and emits v1 symbols;
  * a continuous FUT_SP_500 front-month series stitches and rolls;
  * futures ``close`` is the SETTLEMENT, not ``fact_bar.close``;
  * the ``freq='daily'`` filter holds — no duplicate dates from the 1m series;
  * unsupported collections/instruments RAISE rather than returning empty.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data._v2_compat.adapter import V2MarketDataAdapter
from tcg.data._v2_compat.errors import (
    V2CollectionUnavailable,
    V2InstrumentUnavailable,
)
from tcg.types.market import AdjustmentMethod, ContinuousRollConfig, RollStrategy

# Common v1∩v2 window for futures (spec §9.2): v2 settlement starts 2010-06-07,
# v1 ends 2026-06-10. Staying inside it keeps the comparison honest.
WINDOW_START = date(2010, 7, 1)
WINDOW_END = date(2026, 6, 10)


@pytest.fixture
async def adapter():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield V2MarketDataAdapter(pool)
    await pool.close()


async def test_index_series_is_non_empty_and_monotonic(adapter):
    ps = await adapter.get_prices(
        "INDEX", "IND_SP_500", start=WINDOW_START, end=WINDOW_END
    )
    assert ps is not None and len(ps) > 3000
    assert np.all(np.diff(ps.dates) > 0), "dates must be strictly ascending"
    assert ps.dates[0] >= 20100701 and ps.dates[-1] <= 20260610
    assert np.all(ps.close > 0)
    # All six arrays share a length (PriceSeries invariant).
    assert len({len(a) for a in (ps.dates, ps.open, ps.high, ps.low, ps.close)}) == 1


async def test_index_rejects_other_symbols_live(adapter):
    with pytest.raises(V2InstrumentUnavailable):
        await adapter.get_prices("INDEX", "IND_VIX")


async def test_unsupported_collection_raises_live(adapter):
    """Must RAISE, not fall through to v1 and not return empty."""
    with pytest.raises(V2CollectionUnavailable):
        await adapter.get_prices("FUT_VIX", "ANY")


async def test_continuous_front_month_futures(adapter):
    cfg = ContinuousRollConfig(
        strategy=RollStrategy.FRONT_MONTH, adjustment=AdjustmentMethod.NONE
    )
    cs = await adapter.get_continuous(
        "FUT_SP_500", cfg, start=WINDOW_START, end=WINDOW_END
    )
    assert cs is not None
    assert cs.collection == "FUT_SP_500"
    assert len(cs.prices) > 2000
    assert np.all(np.diff(cs.prices.dates) > 0)
    assert np.all(cs.prices.close > 0)
    assert len(cs.roll_dates) > 30, "≈4 rolls/year over ~16 years"
    # Decision D1: v1 symbols cross the boundary, never v2 contract_codes.
    for c in cs.contracts:
        assert c.startswith("FUT_SP_500_EMINI_")
        assert "." not in c and " " not in c


async def test_single_futures_contract_has_no_duplicate_dates(adapter):
    """Regression guard for the freq='daily' filter.

    Without ``serie.freq='daily'`` the 1m series (968,538 rows at REAL intraday
    timestamps) joins in and every YYYYMMDD appears hundreds of times.
    """
    ps = await adapter.get_prices("FUT_SP_500", "FUT_SP_500_EMINI_20260619")
    assert ps is not None and len(ps) > 0
    assert len(np.unique(ps.dates)) == len(ps.dates)
    assert np.all(np.diff(ps.dates) > 0)


async def test_futures_close_is_settlement_not_bar_close(adapter):
    """v1 ``close`` ≡ v2 ``fact_value`` settlement, bar for bar.

    ``fact_bar.close`` is Databento's last TRADED price and differs from v1 by a
    median of 9.25 index points, so a v2 series built on it would be silently
    wrong on every bar. This pins the adapter to the settlement stream by
    cross-checking it against v1 directly.

    Tolerated: v1 carries occasional FALSE-ZERO closes on US market holidays
    (e.g. 2023-04-07, Good Friday, where v1 reports 0.0 and v2 the true
    settlement 4359.0). Those are a v1 data artefact, so the assertion is on the
    exact-match RATE plus a zero-median, not on a global max.
    """
    from tcg.data.service import DefaultMarketDataService

    symbol = "FUT_SP_500_EMINI_20250620"
    v2 = await adapter.get_prices("FUT_SP_500", symbol)
    assert v2 is not None and len(v2) > 100

    v1_svc = DefaultMarketDataService(adapter._pool)
    v1 = await v1_svc.get_prices("FUT_SP_500", symbol)
    assert v1 is not None, "v1 must cover this contract for the parity check"

    common = np.intersect1d(v1.dates, v2.dates)
    assert len(common) > 100
    v1_close = v1.close[np.isin(v1.dates, common)]
    v2_close = v2.close[np.isin(v2.dates, common)]
    diff = np.abs(v1_close - v2_close)

    # Median 0 and >99% exact: the same quantity, not a 9.25-point-off proxy.
    assert np.median(diff) == 0.0
    exact_rate = float((diff < 1e-6).mean())
    assert exact_rate > 0.99, f"only {exact_rate:.4f} of bars matched v1 exactly"

    # Every non-matching bar is a v1 zero, never a genuine value disagreement.
    mismatched = diff >= 1e-6
    assert np.all(v1_close[mismatched] == 0.0)
    assert np.all(v2_close[mismatched] > 0.0)


async def test_futures_contract_meta_reports_live_multiplier(adapter):
    meta = await adapter.list_futures_contract_meta("FUT_SP_500")
    assert len(meta) > 50
    # v2 states the multiplier for 100% of contracts; v1 is NULL for 93/104.
    assert all(m.contract_size == 50.0 for m in meta)
    assert all(m.symbol.startswith("FUT_SP_500_EMINI_") for m in meta)
    assert all(m.expiration_cycle == "" for m in meta)
    assert meta == sorted(meta, key=lambda m: m.expiration)


async def test_front_contract_lookup_live(adapter):
    sym = await adapter.find_front_futures_contract_on_or_after("FUT_SP_500", 20260401)
    assert sym is not None and sym.startswith("FUT_SP_500_EMINI_2026")


async def test_list_instruments_live(adapter):
    page = await adapter.list_instruments("FUT_SP_500", limit=500)
    assert page.total > 50
    assert all(i.collection == "FUT_SP_500" for i in page.items)
