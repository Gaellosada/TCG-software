"""Tests for the standalone basket-series compute path (Issue #1).

Covers:
* ``compute_basket_series`` — the extracted orchestrator that materialises
  a saved/inline basket and runs the SHARED ``make_signal_fetcher`` over it.
* ``POST /api/data/basket/series`` — the Data-page endpoint serving both
  saved (``{basket_id}``) and inline (``{asset_class, legs}``) baskets.

PARITY is the load-bearing property: the standalone path must produce the
EXACT same weighted-sum series the in-signal basket branch produces, so the
extraction does not fork behaviour.  The fixtures mirror
``test_signals_basket_compute.py`` (same synthetic closes + weights) so the
two paths can be compared head-to-head.
"""

from __future__ import annotations

from datetime import datetime, timezone
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tcg.core.api._persistence_wiring import get_write_repository
from tcg.core.app import create_app
from tcg.types.market import PriceSeries
from tcg.types.persistence import BasketDoc, Category


# --- synthetic data (identical to test_signals_basket_compute.py) ----------
_DATES = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108, 20240109],
    dtype=np.int64,
)
_SPY_CLOSES = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
_QQQ_CLOSES = np.array([200.0, 201.0, 200.0, 202.0, 203.0, 204.0])


def _price_series(closes: np.ndarray) -> PriceSeries:
    n = closes.shape[0]
    return PriceSeries(
        dates=_DATES,
        open=closes - 1.0,
        high=closes + 1.0,
        low=closes - 2.0,
        close=closes,
        volume=np.full(n, 1000.0, dtype=np.float64),
    )


class _BasketRepo:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    def seed(self, doc: Any) -> None:
        self._store[(doc.type, doc.id)] = doc

    async def get_by_id(self, doc_type: str, doc_id: str) -> Any:
        return self._store.get((doc_type, doc_id))


@pytest.fixture
def fake_market_data() -> MagicMock:
    svc = MagicMock()

    async def fake_get_prices(
        collection: str, instrument_id: str, *, start=None, end=None, provider=None
    ):
        if instrument_id == "SPY":
            return _price_series(_SPY_CLOSES)
        if instrument_id == "QQQ":
            return _price_series(_QQQ_CLOSES)
        return None

    svc.get_prices = AsyncMock(side_effect=fake_get_prices)
    # No option expirations seeded by default — an option_stream leg in a
    # basket then surfaces a clean "no option expirations" 400 rather than
    # a 500 from an un-awaitable mock attribute.
    svc.list_option_expirations_filtered = AsyncMock(return_value=[])
    return svc


@pytest.fixture
def basket_repo() -> _BasketRepo:
    repo = _BasketRepo()
    now = datetime.now(timezone.utc)
    repo.seed(
        BasketDoc(
            id="basket-e2e",
            type="basket",
            name="E2E Basket",
            category=Category.RESEARCH,
            created_at=now,
            updated_at=now,
            asset_class="equity",
            legs=(
                {
                    "instrument": {
                        "type": "spot",
                        "collection": "ETF",
                        "instrument_id": "SPY",
                    },
                    "weight": 0.6,
                },
                {
                    "instrument": {
                        "type": "spot",
                        "collection": "ETF",
                        "instrument_id": "QQQ",
                    },
                    "weight": 0.4,
                },
            ),
        )
    )
    return repo


@pytest.fixture
def client(basket_repo: _BasketRepo, fake_market_data: MagicMock) -> TestClient:
    app = create_app()
    app.state.market_data = fake_market_data
    app.dependency_overrides[get_write_repository] = lambda: basket_repo
    return TestClient(app)


# expected composite close = 0.6*SPY + 0.4*QQQ
_EXPECTED_COMPOSITE = 0.6 * _SPY_CLOSES + 0.4 * _QQQ_CLOSES


# ---------------------------------------------------------------------------
# compute_basket_series — orchestrator unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_basket_series_inline_weighted_sum(
    fake_market_data: MagicMock, basket_repo: _BasketRepo
) -> None:
    from tcg.core.api._basket_compute import compute_basket_series

    legs = [
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "SPY"},
            "weight": 0.6,
        },
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "QQQ"},
            "weight": 0.4,
        },
    ]
    dates, values = await compute_basket_series(
        svc=fake_market_data,
        repo=basket_repo,
        basket_id=None,
        asset_class="equity",
        legs=legs,
        start=None,
        end=None,
        field="close",
    )
    np.testing.assert_array_equal(dates, _DATES)
    np.testing.assert_allclose(values, _EXPECTED_COMPOSITE)


@pytest.mark.asyncio
async def test_compute_basket_series_saved_weighted_sum(
    fake_market_data: MagicMock, basket_repo: _BasketRepo
) -> None:
    from tcg.core.api._basket_compute import compute_basket_series

    dates, values = await compute_basket_series(
        svc=fake_market_data,
        repo=basket_repo,
        basket_id="basket-e2e",
        asset_class=None,
        legs=None,
        start=None,
        end=None,
        field="close",
    )
    np.testing.assert_array_equal(dates, _DATES)
    np.testing.assert_allclose(values, _EXPECTED_COMPOSITE)


@pytest.mark.asyncio
async def test_compute_basket_series_parity_with_in_signal_path(
    fake_market_data: MagicMock, basket_repo: _BasketRepo
) -> None:
    """The standalone path must equal what make_signal_fetcher's basket
    branch yields for the SAME basket — proving the extraction did not
    fork behaviour."""
    from tcg.core.api._basket_compute import compute_basket_series
    from tcg.core.api._series_fetch import make_signal_fetcher
    from tcg.types.signal import InstrumentBasket, InstrumentSpot

    # Reference: build the basket directly and call the shared fetcher.
    basket = InstrumentBasket(
        legs=(
            (InstrumentSpot(collection="ETF", instrument_id="SPY"), 0.6),
            (InstrumentSpot(collection="ETF", instrument_id="QQQ"), 0.4),
        ),
        basket_id=None,
        asset_class="equity",
    )
    fetcher = make_signal_fetcher(fake_market_data, None, None)
    ref_dates, ref_values = await fetcher(basket, "close")

    legs = [
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "SPY"},
            "weight": 0.6,
        },
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "QQQ"},
            "weight": 0.4,
        },
    ]
    out_dates, out_values = await compute_basket_series(
        svc=fake_market_data,
        repo=basket_repo,
        basket_id=None,
        asset_class="equity",
        legs=legs,
        start=None,
        end=None,
        field="close",
    )
    np.testing.assert_array_equal(out_dates, ref_dates)
    np.testing.assert_allclose(out_values, ref_values)


# ---------------------------------------------------------------------------
# POST /api/data/basket/series — endpoint
# ---------------------------------------------------------------------------


def test_endpoint_inline_basket_returns_dates_and_values(client: TestClient) -> None:
    body = {
        "basket": {
            "kind": "inline",
            "asset_class": "equity",
            "legs": [
                {
                    "instrument": {
                        "type": "spot",
                        "collection": "ETF",
                        "instrument_id": "SPY",
                    },
                    "weight": 0.6,
                },
                {
                    "instrument": {
                        "type": "spot",
                        "collection": "ETF",
                        "instrument_id": "QQQ",
                    },
                    "weight": 0.4,
                },
            ],
        }
    }
    resp = client.post("/api/data/basket/series", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "dates" in data and "values" in data
    assert data["dates"] == _DATES.tolist()
    np.testing.assert_allclose(data["values"], _EXPECTED_COMPOSITE)


def test_endpoint_saved_basket_returns_dates_and_values(client: TestClient) -> None:
    body = {"basket": {"kind": "saved", "basket_id": "basket-e2e"}}
    resp = client.post("/api/data/basket/series", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dates"] == _DATES.tolist()
    np.testing.assert_allclose(data["values"], _EXPECTED_COMPOSITE)


def test_endpoint_unknown_saved_basket_returns_400(client: TestClient) -> None:
    body = {"basket": {"kind": "saved", "basket_id": "nope"}}
    resp = client.post("/api/data/basket/series", json=body)
    assert resp.status_code == 400, resp.text
    assert "nope" in resp.json().get("message", "")


def test_endpoint_empty_legs_rejected(client: TestClient) -> None:
    body = {"basket": {"kind": "inline", "asset_class": "equity", "legs": []}}
    resp = client.post("/api/data/basket/series", json=body)
    assert resp.status_code in (400, 422), resp.text


def test_endpoint_option_leg_no_expirations_returns_400_not_500(
    client: TestClient,
) -> None:
    """An option_stream leg whose root has no listed expirations must
    surface a clean 400 (client/data problem) — never a 500."""
    body = {
        "basket": {
            "kind": "inline",
            "asset_class": "option",
            "legs": [
                {
                    "instrument": {
                        "type": "option_stream",
                        "collection": "OPT_SP_500",
                        "option_type": "C",
                        "cycle": None,
                        "maturity": {"kind": "next_third_friday"},
                        "selection": {"kind": "by_moneyness", "target": 1.0},
                        "stream": "mid",
                    },
                    "weight": 1.0,
                }
            ],
        }
    }
    # No svc.list_option_expirations_filtered configured → without dates the
    # date-window resolution / fetcher must fail loudly as a 400-class error,
    # never a 500.
    resp = client.post("/api/data/basket/series", json=body)
    assert resp.status_code in (400, 422), resp.text


# ---------------------------------------------------------------------------
# Option-stream-leg PARITY — the standalone path must equal the in-signal
# fetcher for a basket containing an option_stream leg, including the
# date-window derivation.  Patches the option-stream wiring + resolver (same
# lightweight stubs the in-signal option-stream suite uses) so both paths hit
# the identical fake resolver, isolating the basket-orchestration equivalence.
# ---------------------------------------------------------------------------

# Distinct from _DATES — the option leg's own (business-day) axis.
_OPT_TRADE_DATES = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
_OPT_VALUES = np.array([0.25, 0.26, 0.27], dtype=np.float64)
_OPT_EXPIRATIONS = [date(2024, 1, 19), date(2024, 2, 16), date(2024, 3, 15)]


@pytest.mark.asyncio
async def test_compute_basket_series_option_leg_parity_with_in_signal_path(
    monkeypatch,
) -> None:
    """An option_stream-leg basket: ``compute_basket_series`` (with explicit
    start/end) == the REAL in-signal two-phase path
    (``compute_input_overlap`` → ``make_signal_fetcher``). Locks the
    window-derivation + weighted-sum equivalence on the option path.

    NB: the comparison is against the two-phase pipeline, NOT the bare
    fetcher with raw (start,end) — because for an option_stream input the
    in-signal endpoint FIRST clamps the window to the expiration
    intersection via ``compute_input_overlap`` (the Bug-2 fall-through),
    then feeds that to the fetcher.  ``compute_basket_series`` mirrors
    exactly that two-phase behaviour; this test pins the equivalence."""
    from tcg.core.api._basket_compute import compute_basket_series
    from tcg.core.api._series_fetch import make_signal_fetcher
    from tcg.core.api.signals import compute_input_overlap
    from tcg.types.options import ByMoneyness, NextThirdFriday
    from tcg.types.signal import (
        Block,
        CompareCondition,
        ConstantOperand,
        Input,
        InstrumentBasket,
        InstrumentOperand,
        InstrumentOptionStream,
        Signal,
        SignalRules,
    )

    svc = MagicMock()
    svc.list_option_expirations_filtered = AsyncMock(return_value=_OPT_EXPIRATIONS)

    # Same stubs the in-signal option-stream suite uses (accept the optional
    # underlying_prefetch_window kwarg the perf memo threads through).
    monkeypatch.setattr(
        "tcg.core.api._options_wiring.build_stream_resolver_wiring",
        lambda _svc, **_kw: (MagicMock(), MagicMock(), MagicMock(), MagicMock()),
    )

    async def fake_resolve(*, dates, **_kw):
        # Return one value per requested trade date so both paths see an
        # identically-shaped series regardless of the derived window.
        vals = np.arange(len(dates), dtype=np.float64) * 0.01 + 0.25
        return vals, [None] * len(dates), [None] * len(dates)

    monkeypatch.setattr(
        "tcg.engine.options.series.stream_resolver.resolve_option_stream",
        fake_resolve,
    )

    start, end = date(2024, 1, 1), date(2024, 1, 31)

    opt = InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=1),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.01),
        stream="mid",
    )
    basket = InstrumentBasket(legs=((opt, 1.0),), basket_id=None, asset_class="option")

    # Reference = the REAL in-signal two-phase pipeline.
    signal = Signal(
        id="s",
        name="s",
        inputs=(Input(id="B", instrument=basket),),
        rules=SignalRules(
            entries=(
                Block(
                    id="E1",
                    name="AlwaysOn",
                    input_id="B",
                    weight=100.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="B", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            ),
            exits=(),
        ),
    )
    ov_start, ov_end = await compute_input_overlap(svc, signal, start, end)
    fetcher = make_signal_fetcher(svc, ov_start, ov_end)
    ref_dates, ref_values = await fetcher(basket, "close")

    # Standalone path (inline) with the same explicit window.
    legs = [
        {
            "instrument": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "option_type": "C",
                "cycle": None,
                "maturity": {"kind": "next_third_friday"},
                "selection": {"kind": "by_moneyness", "target": 1.0},
                "stream": "mid",
            },
            "weight": 1.0,
        }
    ]
    out_dates, out_values = await compute_basket_series(
        svc=svc,
        repo=_BasketRepo(),
        basket_id=None,
        asset_class="option",
        legs=legs,
        start=start,
        end=end,
        field="close",
    )

    np.testing.assert_array_equal(out_dates, ref_dates)
    np.testing.assert_allclose(out_values, ref_values)
    # And it actually produced the option leg's values (not empty / spot path).
    assert out_values.shape[0] == ref_values.shape[0]
    assert out_values.shape[0] >= 1


# ---------------------------------------------------------------------------
# Coverage surfacing (Issue #1 fix): explain missing points, never silent
# ---------------------------------------------------------------------------


def test_leg_coverage_summarises_gappy_diagnostics() -> None:
    """A synthetic gappy option leg → correct counts / dominant / gap range."""
    from tcg.core.api._basket_compute import _leg_coverage

    record = {
        "descriptor": "OPT_BTC C ByStrike",
        "dates": np.array(
            [20210105, 20210106, 20210107, 20210108, 20210111], dtype=np.int64
        ),
        "error_codes": [
            "no_chain_for_date",
            "no_chain_for_date",
            None,
            "missing_mid",
            "no_chain_for_date",
        ],
    }
    cov = _leg_coverage(record)
    assert cov["n"] == 5
    assert cov["n_holes"] == 4
    assert cov["counts"] == {"no_chain_for_date": 3, "missing_mid": 1}
    assert cov["dominant_code"] == "no_chain_for_date"
    assert cov["first_gap"] == "2021-01-05"
    assert cov["last_gap"] == "2021-01-11"
    assert cov["descriptor"] == "OPT_BTC C ByStrike"


def test_leg_coverage_no_holes() -> None:
    from tcg.core.api._basket_compute import _leg_coverage

    cov = _leg_coverage(
        {"descriptor": "x", "dates": np.array([20240101]), "error_codes": [None]}
    )
    assert cov["n_holes"] == 0
    assert cov["counts"] == {}
    assert cov["dominant_code"] is None
    assert cov["first_gap"] is None


@pytest.mark.asyncio
async def test_compute_basket_series_populates_composite_coverage(
    fake_market_data: MagicMock, basket_repo: _BasketRepo
) -> None:
    """A spot basket (no option legs) still reports composite coverage: full
    coverage, zero holes, empty per-leg list."""
    from tcg.core.api._basket_compute import compute_basket_series

    coverage: dict = {}
    legs = [
        {
            "instrument": {"type": "spot", "collection": "ETF", "instrument_id": "SPY"},
            "weight": 1.0,
        },
    ]
    _, values = await compute_basket_series(
        svc=fake_market_data,
        repo=basket_repo,
        basket_id=None,
        asset_class="equity",
        legs=legs,
        start=None,
        end=None,
        field="close",
        coverage_out=coverage,
    )
    assert coverage["composite"] == {"n": int(values.size), "n_holes": 0}
    assert coverage["legs"] == []


def test_leg_coverage_success_side_notes_are_not_holes() -> None:
    """MAJOR-2 regression: the resolver's SUCCESS-side annotations
    (``snapped_to:`` / ``coverage_skipped:``) coexist with a REAL value — they
    must NOT be tallied as coverage holes (a snapped date resolved fine).
    """
    from tcg.core.api._basket_compute import _leg_coverage

    record = {
        "descriptor": "OPT_SP_500 P NearestToTarget",
        "dates": np.array([20240102, 20240103, 20240104, 20240105], dtype=np.int64),
        "error_codes": [
            "snapped_to:2024-01-19",  # success-side note, real value
            None,
            "coverage_skipped:2024-02-16",  # success-side note, real value
            "no_chain_for_date",  # the ONLY genuine hole
        ],
    }
    cov = _leg_coverage(record)
    assert cov["n"] == 4
    # Only the no_chain_for_date date is a hole; the two annotations are not.
    assert cov["n_holes"] == 1
    assert cov["counts"] == {"no_chain_for_date": 1}
    assert cov["dominant_code"] == "no_chain_for_date"
    assert cov["first_gap"] == cov["last_gap"] == "2024-01-05"


def test_leg_coverage_all_success_side_notes_zero_holes() -> None:
    """A leg whose only non-None codes are success-side notes has ZERO holes."""
    from tcg.core.api._basket_compute import _leg_coverage

    cov = _leg_coverage(
        {
            "descriptor": "x",
            "dates": np.array([20240101, 20240102], dtype=np.int64),
            "error_codes": ["snapped_to:2024-01-19", "coverage_skipped:2024-02-16"],
        }
    )
    assert cov["n_holes"] == 0
    assert cov["counts"] == {}
    assert cov["dominant_code"] is None
    assert cov["first_gap"] is None and cov["last_gap"] is None


@pytest.mark.asyncio
async def test_materialise_option_streams_threads_per_date_map_for_nearest_target(
    monkeypatch,
) -> None:
    """MAJOR-1 regression: the shared ``materialise_option_streams`` (behind
    /api/options/stream, Indicators and portfolio level legs) must fetch the
    per-date LISTED-expiration map for a NearestToTarget ref and thread it into
    the resolver as ``available_expirations_by_date`` — the daily-expiration
    global-snap fix was previously applied ONLY on the signals path.
    """
    from tcg.core.api._models import OptionStreamRef
    from tcg.core.api._options_materialise import materialise_option_streams

    svc = MagicMock()
    svc.list_option_expirations_filtered = AsyncMock(return_value=_OPT_EXPIRATIONS)
    per_date_map = {date(2021, 1, 5): [date(2021, 1, 29)]}
    svc.list_option_expirations_by_date = AsyncMock(return_value=per_date_map)

    monkeypatch.setattr(
        "tcg.core.api._options_materialise.build_stream_resolver_wiring",
        lambda _svc, **_kw: (MagicMock(), MagicMock(), MagicMock(), MagicMock()),
    )

    captured: dict = {}

    async def fake_resolve(*, dates, **kw):
        captured.update(kw)
        vals = np.zeros(len(dates), dtype=np.float64)
        return vals, [None] * len(dates), [None] * len(dates)

    monkeypatch.setattr(
        "tcg.core.api._options_materialise.resolve_option_stream", fake_resolve
    )

    ref = OptionStreamRef.model_validate(
        {
            "type": "option_stream",
            "collection": "OPT_BTC",
            "option_type": "C",
            "cycle": None,
            "maturity": {"kind": "nearest_to_target", "target_dte_days": 30},
            "selection": {"kind": "by_strike", "strike": 100.0},
            "stream": "mid",
        }
    )
    result = await materialise_option_streams(
        [("_x", ref)],
        svc=svc,
        start_date=date(2021, 1, 5),
        end_date=date(2021, 1, 6),
    )
    assert not isinstance(result, str)
    # The per-date map was fetched ONCE and threaded through verbatim.
    svc.list_option_expirations_by_date.assert_awaited_once()
    assert captured["available_expirations_by_date"] is per_date_map
    # And the scan was bounded (expiration_max passed as a kwarg).
    _, call_kwargs = svc.list_option_expirations_by_date.call_args
    assert call_kwargs["expiration_max"] is not None


@pytest.mark.asyncio
async def test_signal_fetcher_nearest_target_awaits_per_date_map_expanded_cycle(
    monkeypatch,
) -> None:
    """MINOR-7(a): a NearestToTarget option input drives ``make_signal_fetcher``
    to fetch the per-date map with the EXPANDED cycle and thread it into the
    resolver as ``available_expirations_by_date``."""
    from tcg.core.api._series_fetch import make_signal_fetcher
    from tcg.types.options import ByStrike, NearestToTarget
    from tcg.types.signal import InstrumentOptionStream

    svc = MagicMock()
    svc.list_option_expirations_filtered = AsyncMock(return_value=_OPT_EXPIRATIONS)
    per_date_map = {date(2024, 1, 2): [date(2024, 1, 19)]}
    svc.list_option_expirations_by_date = AsyncMock(return_value=per_date_map)

    monkeypatch.setattr(
        "tcg.core.api._options_wiring.build_stream_resolver_wiring",
        lambda _svc, **_kw: (MagicMock(), MagicMock(), MagicMock(), MagicMock()),
    )

    captured: dict = {}

    async def fake_resolve(*, dates, **kw):
        captured.update(kw)
        return (
            np.zeros(len(dates), dtype=np.float64),
            [None] * len(dates),
            [None] * len(dates),
        )

    monkeypatch.setattr(
        "tcg.engine.options.series.stream_resolver.resolve_option_stream",
        fake_resolve,
    )

    inst = InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="C",
        cycle="M",  # expand_cycle('M') broadens to the 3rd-Friday series
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
    )
    fetcher = make_signal_fetcher(svc, date(2024, 1, 2), date(2024, 1, 4))
    await fetcher(inst, "close")

    svc.list_option_expirations_by_date.assert_awaited_once()
    _, call_kwargs = svc.list_option_expirations_by_date.call_args
    # Expanded cycle used for BOTH the list and the resolver.
    from tcg.types.options import expand_cycle

    assert call_kwargs["cycle"] == expand_cycle("M")
    assert call_kwargs["expiration_max"] is not None
    assert captured["available_expirations_by_date"] is per_date_map


@pytest.mark.asyncio
async def test_compute_basket_series_option_leg_populates_leg_coverage(
    monkeypatch, basket_repo: _BasketRepo
) -> None:
    """MINOR-7(b): an OPTION-leg basket populates ``coverage["legs"][0]`` with a
    per-leg coverage block derived from the resolver's per-date diagnostics."""
    from tcg.core.api._basket_compute import compute_basket_series

    svc = MagicMock()
    svc.list_option_expirations_filtered = AsyncMock(return_value=_OPT_EXPIRATIONS)

    monkeypatch.setattr(
        "tcg.core.api._options_wiring.build_stream_resolver_wiring",
        lambda _svc, **_kw: (MagicMock(), MagicMock(), MagicMock(), MagicMock()),
    )

    async def fake_resolve(*, dates, **_kw):
        n = len(dates)
        vals = np.full(n, 0.25, dtype=np.float64)
        codes: list = [None] * n
        if n >= 2:
            vals[0] = np.nan
            codes[0] = "no_chain_for_date"
            codes[1] = "snapped_to:2024-01-19"  # success-side, NOT a hole
        return vals, codes, [None] * n

    monkeypatch.setattr(
        "tcg.engine.options.series.stream_resolver.resolve_option_stream",
        fake_resolve,
    )

    legs = [
        {
            "instrument": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "option_type": "C",
                "cycle": None,
                "maturity": {"kind": "next_third_friday"},
                "selection": {"kind": "by_moneyness", "target": 1.0},
                "stream": "mid",
            },
            "weight": 1.0,
        }
    ]
    coverage: dict = {}
    await compute_basket_series(
        svc=svc,
        repo=basket_repo,
        basket_id=None,
        asset_class="option",
        legs=legs,
        start=date(2024, 1, 15),
        end=date(2024, 2, 20),
        field="close",
        coverage_out=coverage,
    )
    assert len(coverage["legs"]) == 1
    leg = coverage["legs"][0]
    assert set(leg) == {
        "descriptor",
        "n",
        "n_holes",
        "counts",
        "dominant_code",
        "first_gap",
        "last_gap",
    }
    # Exactly ONE genuine hole (the snapped_to note is not counted).
    assert leg["n_holes"] == 1
    assert leg["counts"] == {"no_chain_for_date": 1}
    assert leg["dominant_code"] == "no_chain_for_date"
    assert "OPT_SP_500" in leg["descriptor"]


def test_basket_series_endpoint_carries_coverage(client: TestClient) -> None:
    """The Data-page endpoint response includes a ``coverage`` block."""
    resp = client.post(
        "/api/data/basket/series",
        json={
            "basket": {
                "kind": "inline",
                "asset_class": "equity",
                "legs": [
                    {
                        "instrument": {
                            "type": "spot",
                            "collection": "ETF",
                            "instrument_id": "SPY",
                        },
                        "weight": 1.0,
                    }
                ],
            },
            "field": "close",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "coverage" in body
    assert body["coverage"]["composite"]["n_holes"] == 0
