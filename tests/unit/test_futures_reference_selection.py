"""Reference-future selection for futures-notional option sizing (dwh-free).

Covers ``_pick_reference_contract`` (nearest_on_or_after + nearest_abs incl. a tie
and a before-vs-after case) and the ``build_futures_reference_resolver`` closure
returning (price, contract_size) — the live M_fut hint.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.core.api._options_wiring import (
    _pick_reference_contract,
    build_futures_reference_resolver,
)
from tcg.data._utils import date_to_int
from tcg.types.market import FuturesContractMeta, PriceSeries


def _m(
    y: int,
    mo: int,
    d: int,
    cs: float | None,
    sym: str,
    cycle: str | None = None,
) -> FuturesContractMeta:
    return FuturesContractMeta(
        symbol=sym,
        expiration=date(y, mo, d),
        contract_size=cs,
        expiration_cycle=cycle,
    )


# ascending by (expiration, symbol), as list_futures_contract_meta returns
_METAS = [
    _m(2024, 3, 15, 50.0, "ESH24"),
    _m(2024, 6, 21, 50.0, "ESM24"),
    _m(2024, 9, 20, 50.0, "ESU24"),
]


def test_nearest_on_or_after_first_ge() -> None:
    # Option expiry 2024-05-01 → first future expiring >= is the JUN contract.
    c = _pick_reference_contract(_METAS, date(2024, 5, 1), "nearest_on_or_after")
    assert c is not None and c.symbol == "ESM24"


def test_nearest_on_or_after_exact_boundary() -> None:
    c = _pick_reference_contract(_METAS, date(2024, 6, 21), "nearest_on_or_after")
    assert c is not None and c.symbol == "ESM24"  # >= includes equal


def test_nearest_on_or_after_none_beyond_curve() -> None:
    assert (
        _pick_reference_contract(_METAS, date(2025, 1, 1), "nearest_on_or_after")
        is None
    )


def test_nearest_abs_before_vs_after_picks_closer() -> None:
    # 2024-06-10 is 11 days before JUN(21) and ~87 after MAR(15) → JUN wins.
    c = _pick_reference_contract(_METAS, date(2024, 6, 10), "nearest_abs")
    assert c is not None and c.symbol == "ESM24"
    # 2024-04-01 is 17 days after MAR(15) and 81 before JUN(21) → MAR wins.
    c2 = _pick_reference_contract(_METAS, date(2024, 4, 1), "nearest_abs")
    assert c2 is not None and c2.symbol == "ESH24"


def test_nearest_abs_beyond_curve_picks_last() -> None:
    # Option outlives the curve → nearest_abs still returns the closest (last) one,
    # unlike nearest_on_or_after which returns None.
    c = _pick_reference_contract(_METAS, date(2025, 1, 1), "nearest_abs")
    assert c is not None and c.symbol == "ESU24"


def test_nearest_abs_equidistant_tie_prefers_on_or_after() -> None:
    # Two contracts equidistant (3 days each side); tie breaks toward on/after.
    metas = [
        _m(2024, 6, 18, 50.0, "A"),  # 3 days before
        _m(2024, 6, 24, 50.0, "B"),  # 3 days after (on/after preferred)
    ]
    c = _pick_reference_contract(metas, date(2024, 6, 21), "nearest_abs")
    assert c is not None and c.symbol == "B"


# ── ENG-SIZING-2: weekly contracts are never the sizing reference ───────────
def test_weekly_contracts_excluded_when_monthly_present() -> None:
    """For a multi-cycle root (VIX = monthly 'M' + weekly 'W') the reference must
    be a MONTHLY contract even when a weekly expires closer to the option expiry."""
    metas = [
        _m(2024, 5, 15, 1000.0, "VXK24", cycle="M"),  # monthly
        _m(2024, 5, 22, 1000.0, "VX22W", cycle="W"),  # weekly, expires closer
        _m(2024, 6, 19, 1000.0, "VXM24", cycle="M"),  # monthly
    ]
    # Option expiry 2024-05-20: the WEEKLY (05-22) is nearest on/after, but must be
    # skipped → the JUN monthly (first monthly >= expiry) is chosen.
    c = _pick_reference_contract(metas, date(2024, 5, 20), "nearest_on_or_after")
    assert c is not None and c.symbol == "VXM24"
    # nearest_abs would also pick the 05-22 weekly (2 days) over 05-15 monthly (5
    # days); with weeklies excluded the 05-15 monthly wins.
    c2 = _pick_reference_contract(metas, date(2024, 5, 20), "nearest_abs")
    assert c2 is not None and c2.symbol == "VXK24"


def test_single_cycle_root_unaffected_by_weekly_filter() -> None:
    """SP_500 futures are single-cycle (empty-string ``expiration_cycle``); the
    weekly filter must leave selection byte-identical to the no-cycle path."""
    metas = [
        _m(2024, 3, 15, 50.0, "ESH24", cycle=""),
        _m(2024, 6, 21, 50.0, "ESM24", cycle=""),
    ]
    c = _pick_reference_contract(metas, date(2024, 5, 1), "nearest_on_or_after")
    assert c is not None and c.symbol == "ESM24"


def test_all_weekly_root_falls_back_to_weeklies() -> None:
    """Degenerate: if EVERY candidate is weekly, don't return None — fall back to
    the full set rather than refusing to size."""
    metas = [
        _m(2024, 5, 22, 1000.0, "VX22W", cycle="W"),
        _m(2024, 5, 29, 1000.0, "VX29W", cycle="W"),
    ]
    c = _pick_reference_contract(metas, date(2024, 5, 20), "nearest_on_or_after")
    assert c is not None and c.symbol == "VX22W"


# ── ENG-SIZING-3: a transient DB error must PROPAGATE, not become silent [] ──
async def test_db_error_propagates_not_silent_empty() -> None:
    from tcg.types.errors import DataAccessError

    class _FailingSvc:
        async def list_futures_contract_meta(self, collection, *, cycle=None):
            raise DataAccessError("transient dwh pool timeout")

    import pytest

    resolver = build_futures_reference_resolver(
        _FailingSvc(),
        option_collection="OPT_VIX",
        futures_reference="nearest_on_or_after",
        prefetch_window=None,
    )
    with pytest.raises(DataAccessError):
        await resolver(date(2024, 5, 1), date(2024, 5, 17))


# ── The closure returns (price, contract_size) — the live M_fut hint ────────
class _FakeSvc:
    def __init__(self, metas, closes):
        self._metas = metas
        self._closes = closes  # {symbol: {date_int: close}}

    async def list_futures_contract_meta(self, collection, *, cycle=None):
        return self._metas

    async def get_prices(self, collection, contract_ref, *, start=None, end=None):
        cl = self._closes.get(contract_ref)
        if cl is None:
            return None
        dates = sorted(cl)
        n = len(dates)
        arr = np.array([cl[d] for d in dates], dtype=np.float64)
        return PriceSeries(
            dates=np.array(dates, dtype=np.int64),
            open=arr,
            high=arr,
            low=arr,
            close=arr,
            volume=np.zeros(n, dtype=np.float64),
        )


async def test_closure_returns_price_and_contract_size() -> None:
    roll = date(2024, 5, 1)
    metas = [_m(2024, 6, 21, 50.0, "ESM24")]
    svc = _FakeSvc(metas, {"ESM24": {date_to_int(roll): 5300.0}})
    resolver = build_futures_reference_resolver(
        svc,
        option_collection="OPT_SP_500",
        futures_reference="nearest_on_or_after",
        prefetch_window=(roll, roll),
    )
    result = await resolver(roll, date(2024, 5, 17))
    assert result is not None
    price, cs = result
    assert price == 5300.0 and cs == 50.0


async def test_closure_none_when_no_price() -> None:
    roll = date(2024, 5, 1)
    metas = [_m(2024, 6, 21, 50.0, "ESM24")]
    svc = _FakeSvc(metas, {})  # no close for the contract
    resolver = build_futures_reference_resolver(
        svc,
        option_collection="OPT_SP_500",
        futures_reference="nearest_abs",
        prefetch_window=(roll, roll),
    )
    assert await resolver(roll, date(2024, 5, 17)) is None


async def test_continuous_front_still_raises() -> None:
    import pytest

    resolver = build_futures_reference_resolver(
        _FakeSvc([], {}),
        option_collection="OPT_SP_500",
        futures_reference="continuous_front",
        prefetch_window=None,
    )
    with pytest.raises(NotImplementedError):
        await resolver(date(2024, 5, 1), date(2024, 5, 17))
