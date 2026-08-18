"""Live-dwh integration test for F5 (VVIX feed) — ``IND_VVIX``.

Confirms ``IND_VVIX`` is a first-class, selectable INDEX-collection instrument
through the SAME real path ``IND_VIX`` already uses — no new plumbing:

    DefaultMarketDataService (tcg/data/service.py)
      -> SqlInstrumentReader.list_instruments / read_prices (tcg/data/_sql/instruments.py)
      -> dwh tcg_instruments schema (source_collection='INDEX', symbol='IND_VVIX')

Gated by ``--run-integration`` (see ``tests/integration/conftest.py``) AND by
the ``DWH_*`` connection variables being present/reachable (skip otherwise).

Verifies:
  * ``IND_VVIX`` is enumerated by ``list_instruments('INDEX', ...)`` — the
    SAME discovery call the ``/api/data/{collection}`` route and the
    frontend InstrumentPickerModal / Data-page CategoryBrowser use, so this
    is proof the symbol surfaces to the UI with zero code changes.
  * ``get_prices('INDEX', 'IND_VVIX', ...)`` returns a real, non-empty
    ``PriceSeries`` with the expected span (earliest 2007-01-03, latest
    >= 2026-06-11 floor) and finite/positive close prices.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.data._sql.connection import DwhConnectionPool, load_dwh_config
from tcg.data.service import DefaultMarketDataService


@pytest.fixture
async def svc():
    try:
        cfg = load_dwh_config()
    except ValueError as exc:
        pytest.skip(f"dwh config not available: {exc}")
    pool = DwhConnectionPool(**cfg)
    try:
        await pool.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"dwh not reachable: {exc}")
    yield DefaultMarketDataService(pool)
    await pool.close()


@pytest.mark.integration
async def test_ind_vvix_is_enumerated_in_index_collection(svc):
    """``IND_VVIX`` shows up via the SAME discovery call the UI/API use.

    ``list_instruments`` is exactly what ``GET /api/data/INDEX`` (the route
    the frontend InstrumentPickerModal + Data-page CategoryBrowser call) and
    ``resolveDefaultIndexInstrument`` in the frontend delegate to — proving
    discovery is dynamic (DB-driven), not a hardcoded allowlist that would
    need editing to include VVIX.
    """
    result = await svc.list_instruments("INDEX", skip=0, limit=500)
    symbols = {inst.symbol for inst in result.items}
    assert "IND_VIX" in symbols, "sanity: IND_VIX must already be present"
    assert "IND_VVIX" in symbols, "IND_VVIX must be discoverable in INDEX collection"


@pytest.mark.integration
async def test_ind_vvix_prices_real_path(svc):
    """``IND_VVIX`` prices via the exact route the frontend/backend use for IND_VIX."""
    series = await svc.get_prices(
        "INDEX",
        "IND_VVIX",
        start=date(1980, 1, 1),
        end=date(2050, 12, 31),
    )
    assert series is not None, "IND_VVIX must resolve to a real PriceSeries"

    n_bars = len(series.dates)
    assert 4800 <= n_bars <= 4950, f"expected ~4882 bars, got {n_bars}"

    # ``PriceSeries.dates`` is YYYYMMDD int (see tcg/types/market.py), sorted
    # ascending by the reader's ``ORDER BY f.trade_date``.
    earliest = int(series.dates[0])
    latest = int(series.dates[-1])
    assert earliest == 20070103, f"unexpected earliest date {earliest}"
    assert latest >= 20260611, f"unexpected latest date {latest}"

    closes = np.asarray(series.close, dtype=float)
    assert np.all(np.isfinite(closes)), "IND_VVIX closes must all be finite"
    assert np.all(closes > 0), "IND_VVIX closes must all be positive"
