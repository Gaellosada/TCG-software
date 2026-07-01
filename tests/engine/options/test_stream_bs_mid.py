"""Tests for the COMPUTED ``bs_mid`` option stream in the stream resolver.

``bs_mid`` reproduces the Java sim's price basis (recon §4): the held option is
priced as a Black-76 theoretical value from the day's stored surface IV on the
underlying FUTURE (ACT/365, r=0), intrinsic at expiry — NOT the raw bid-ask mid.
For deep-OTM puts with wide quotes the two differ a lot (the likely biggest
remaining S1 corr gap).

These tests are dwh-free: synthetic chains (``_stream_fakes``) + a fake
underlying-price resolver.  Expected values come from the SAME Black-76 kernel
(``BS76Kernel``) the resolver uses, so the test pins "bs_mid == kernel price
from (iv, future, strike, dte)" exactly, plus the at-expiry intrinsic and the
loud missing-input diagnostics.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.pricing.kernel import BS76Kernel
from tcg.engine.options.series.stream_resolver import (
    _price_bs_mid,
    resolve_option_stream,
)
from tcg.types.options import (
    ByStrike,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

# Async tests auto-marked (asyncio_mode="auto"); the sync unit tests below must
# NOT be marked, so no module-level pytestmark.

_KERNEL = BS76Kernel()

# One APR put expiration; a single strike (ByStrike, no churn) so the value is
# deterministically the held contract's BS price each day.
_APR = date(2024, 4, 19)
_DATES = [
    date(2024, 3, 20),
    date(2024, 3, 21),
    date(2024, 3, 22),
]
_STRIKE = 4000.0
_APR_C = _contract(strike=_STRIKE, expiration=_APR, type_="P")

# Per-date stored IV on the contract row.
_IV = {
    _DATES[0]: 0.20,
    _DATES[1]: 0.22,
    _DATES[2]: 0.18,
}
# Per-date underlying FUTURE price (returned by the fake underlying resolver).
_FUT = {
    _DATES[0]: 4500.0,
    _DATES[1]: 4480.0,
    _DATES[2]: 4520.0,
}


def _build_chains(iv_map=None):
    iv_map = iv_map or _IV
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in _DATES:
        # mid is deliberately a WRONG/placeholder number (5.0) so a test that
        # accidentally read the row mid instead of pricing bs would fail.
        chains[d] = [
            (_APR_C, _row(row_date=d, mid=5.0, iv=iv_map[d], delta=-0.10)),
        ]
    return chains


def _make_underlying_resolver(fut_map=None):
    fut_map = fut_map or _FUT

    async def resolver(contract, d):
        return fut_map.get(d)

    return resolver


_MATURITY = NearestToTarget(target_dte_days=30)
_SELECTION = ByStrike(strike=_STRIKE)


async def _resolve(stream, *, chains=None, underlying=None, selection=None, dates=None):
    return await resolve_option_stream(
        dates=dates or _DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=selection or _SELECTION,
        stream=stream,
        chain_reader=FakeChainReader(chains or _build_chains()),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=underlying or _make_underlying_resolver(),
        bulk_chain_reader=FakeBulkChainReader(chains or _build_chains()),
        available_expirations=[_APR],
    )


# ── Pure unit tests for _price_bs_mid ──────────────────────────────────────


def test_price_bs_mid_matches_kernel_put():
    F, K, T_days, iv = 4500.0, 4000.0, 60, 0.20
    price, code = _price_bs_mid(
        iv=iv,
        future_price=F,
        strike=K,
        option_type="P",
        dte_days=T_days,
        kernel=_KERNEL,
    )
    assert code is None
    expected = _KERNEL.price_put(F, K, T_days / 365.0, 0.0, iv)
    assert price == pytest.approx(expected, rel=1e-12)


def test_price_bs_mid_matches_kernel_call():
    F, K, T_days, iv = 4500.0, 4600.0, 45, 0.25
    price, code = _price_bs_mid(
        iv=iv,
        future_price=F,
        strike=K,
        option_type="C",
        dte_days=T_days,
        kernel=_KERNEL,
    )
    assert code is None
    expected = _KERNEL.price_call(F, K, T_days / 365.0, 0.0, iv)
    assert price == pytest.approx(expected, rel=1e-12)


def test_price_bs_mid_at_expiry_is_intrinsic_put():
    # dte_days == 0 → intrinsic max(K-F, 0), IV irrelevant (even None).
    itm, code = _price_bs_mid(
        iv=None,
        future_price=3900.0,
        strike=4000.0,
        option_type="P",
        dte_days=0,
        kernel=_KERNEL,
    )
    assert code is None and itm == pytest.approx(100.0)
    otm, code = _price_bs_mid(
        iv=None,
        future_price=4100.0,
        strike=4000.0,
        option_type="P",
        dte_days=0,
        kernel=_KERNEL,
    )
    assert code is None and otm == pytest.approx(0.0)


def test_price_bs_mid_at_expiry_is_intrinsic_call():
    itm, code = _price_bs_mid(
        iv=None,
        future_price=4100.0,
        strike=4000.0,
        option_type="C",
        dte_days=-1,
        kernel=_KERNEL,  # past expiry too
    )
    assert code is None and itm == pytest.approx(100.0)


def test_price_bs_mid_missing_future_is_loud():
    for bad in (None, 0.0, -1.0):
        price, code = _price_bs_mid(
            iv=0.2,
            future_price=bad,
            strike=4000.0,
            option_type="P",
            dte_days=30,
            kernel=_KERNEL,
        )
        assert price is None and code == "missing_underlying_price"


def test_price_bs_mid_missing_iv_before_expiry_is_loud_no_fabrication():
    for bad in (None, 0.0, -0.1):
        price, code = _price_bs_mid(
            iv=bad,
            future_price=4500.0,
            strike=4000.0,
            option_type="P",
            dte_days=30,
            kernel=_KERNEL,
        )
        assert price is None and code == "missing_bs_iv"


# ── Resolver integration: bs_mid per day == kernel price ───────────────────


async def test_bs_mid_stream_per_day_equals_kernel_price():
    v, e, c = await _resolve("bs_mid")
    assert all(err is None for err in e), e
    for i, d in enumerate(_DATES):
        dte = (_APR - d).days
        expected = _KERNEL.price_put(_FUT[d], _STRIKE, dte / 365.0, 0.0, _IV[d])
        np.testing.assert_allclose(v[i], expected, rtol=1e-12)
    # And bs_mid is NOT the raw row mid (5.0) — proves it priced, not read.
    assert not np.any(np.isclose(v, 5.0))


async def test_bs_mid_bydelta_path_also_prices():
    """ByDelta selection (async path already) also produces bs prices."""
    from tcg.types.options import ByDelta

    v, e, _c = await _resolve(
        "bs_mid", selection=ByDelta(target_delta=-0.10, tolerance=0.20)
    )
    assert all(err is None for err in e), e
    for i, d in enumerate(_DATES):
        dte = (_APR - d).days
        expected = _KERNEL.price_put(_FUT[d], _STRIKE, dte / 365.0, 0.0, _IV[d])
        np.testing.assert_allclose(v[i], expected, rtol=1e-12)


async def test_bs_mid_missing_iv_row_is_diagnostic_not_crash():
    """A row with no stored IV → that date is NaN + 'missing_bs_iv' (loud), the
    rest still price."""
    iv_hole = dict(_IV)
    iv_hole[_DATES[1]] = None
    chains = _build_chains(iv_map=iv_hole)
    v, e, _c = await _resolve("bs_mid", chains=chains)
    assert np.isnan(v[1])
    assert e[1] == "missing_bs_iv"
    # Neighbours priced fine.
    assert not np.isnan(v[0]) and not np.isnan(v[2])


async def test_bs_mid_missing_future_is_diagnostic():
    """No underlying future price on a date → NaN + 'missing_underlying_price'."""
    fut_hole = dict(_FUT)
    fut_hole[_DATES[2]] = None
    v, e, _c = await _resolve("bs_mid", underlying=_make_underlying_resolver(fut_hole))
    assert np.isnan(v[2])
    assert e[2] == "missing_underlying_price"


async def test_bs_mid_at_expiry_date_uses_intrinsic():
    """A trade date ON the expiration prices at intrinsic (needs no IV)."""
    dates = [date(2024, 3, 20), _APR]  # second date IS the expiry
    iv_map = {dates[0]: 0.20, dates[1]: None}  # no IV on expiry day
    fut_map = {dates[0]: 4500.0, dates[1]: 3950.0}  # ITM put on expiry (K-F=50)
    chains = {
        d: [(_APR_C, _row(row_date=d, mid=5.0, iv=iv_map[d], delta=-0.10))]
        for d in dates
    }
    v, e, _c = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream="bs_mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(fut_map),
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[_APR],
    )
    assert all(err is None for err in e), e
    # Day 0 priced via BS; expiry day = intrinsic max(4000-3950,0)=50.
    np.testing.assert_allclose(v[1], 50.0, rtol=1e-12)


async def test_default_mid_stream_unchanged_by_bs_mid_addition():
    """DEFAULT stream='mid' still reads the raw row mid (5.0 here) — bs_mid's
    addition did not disturb the row-attribute extraction path."""
    v, e, _c = await _resolve("mid")
    assert all(err is None for err in e), e
    np.testing.assert_allclose(v, [5.0, 5.0, 5.0])


async def test_hold_mode_bs_mid_emits_bs_priced_held_premium_and_roll_info():
    """HOLD mode with stream='bs_mid': the held-premium LEVEL AND the segment's
    roll_premium are BLACK-76 prices (from IV + future), not raw row mids — so
    the fixed-contract dollar-P&L's premium_roll and Δpremium are computed from
    BS prices.  Single ByStrike segment (no roll) → each value is the held
    contract's BS price; roll_premium[0] is the day-0 BS price."""
    roll_info: dict = {}
    v, e, c = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream="bs_mid",
        chain_reader=FakeChainReader(_build_chains()),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(),
        bulk_chain_reader=FakeBulkChainReader(_build_chains()),
        available_expirations=[_APR],
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
    )
    assert all(err is None for err in e), e
    # Held value each day = the held contract's BS price (NOT the raw mid 5.0).
    for i, d in enumerate(_DATES):
        dte = (_APR - d).days
        expected = _KERNEL.price_put(_FUT[d], _STRIKE, dte / 365.0, 0.0, _IV[d])
        np.testing.assert_allclose(v[i], expected, rtol=1e-12)
    assert not np.any(np.isclose(v, 5.0))
    # is_roll marks the initial open at index 0; roll_premium[0] = day-0 BS price.
    is_roll = np.asarray(roll_info["is_roll"], dtype=bool)
    roll_premium = np.asarray(roll_info["roll_premium"], dtype=np.float64)
    assert bool(is_roll[0])
    dte0 = (_APR - _DATES[0]).days
    bs0 = _KERNEL.price_put(_FUT[_DATES[0]], _STRIKE, dte0 / 365.0, 0.0, _IV[_DATES[0]])
    np.testing.assert_allclose(roll_premium[0], bs0, rtol=1e-12)


async def test_hold_bs_mid_full_pipeline_equity_matches_oracle_from_bs_prices():
    """FULL pipeline: a short-put signal with hold_between_rolls + stream='bs_mid'
    → resolver emits BS-priced held premium + roll info → signal_exec books
    fixed-contract dollar P&L → equity == the Java-faithful oracle computed from
    the SAME BS prices (proves bs_mid feeds premium_roll/Δpremium, end to end)."""
    from tcg.engine.signal_exec import evaluate_signal
    from tcg.types.signal import (
        Block,
        CompareCondition,
        ConstantOperand,
        Input,
        InstrumentOperand,
        InstrumentOptionStream,
        InstrumentSpot,
        Signal,
        SignalRules,
    )

    dates_int = np.array([int(d.strftime("%Y%m%d")) for d in _DATES], dtype=np.int64)
    chains = _build_chains()
    underlying = _make_underlying_resolver()

    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            return dates_int, np.full(len(_DATES), 100.0, dtype=np.float64)
        if isinstance(instrument, InstrumentOptionStream):
            v, _e, _c = await resolve_option_stream(
                dates=_DATES,
                collection="OPT_SP_500",
                option_type="P",
                cycle=None,
                maturity=_MATURITY,
                selection=_SELECTION,
                stream=instrument.stream,
                chain_reader=FakeChainReader(chains),
                maturity_resolver=DefaultMaturityResolver(),
                underlying_price_resolver=underlying,
                bulk_chain_reader=FakeBulkChainReader(chains),
                available_expirations=[_APR],
                hold_between_rolls=instrument.hold_between_rolls,
            )
            return dates_int, v
        raise KeyError(instrument)

    async def fetch_hold_roll_info(instrument):
        ri: dict = {}
        await resolve_option_stream(
            dates=_DATES,
            collection="OPT_SP_500",
            option_type="P",
            cycle=None,
            maturity=_MATURITY,
            selection=_SELECTION,
            stream=instrument.stream,
            chain_reader=FakeChainReader(chains),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=underlying,
            bulk_chain_reader=FakeBulkChainReader(chains),
            available_expirations=[_APR],
            hold_between_rolls=True,
            hold_roll_info_out=ri,
        )
        return dates_int, ri["is_roll"], ri["roll_premium"]

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]

    opt = InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream="bs_mid",
        hold_between_rolls=True,
        nav_times=1.0,
    )
    signal = Signal(
        id="s",
        name="bs hold",
        inputs=(
            Input(id="P", instrument=opt),
            Input(
                id="S", instrument=InstrumentSpot(collection="I", instrument_id="SPX")
            ),
        ),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="P",
                    weight=-10.0,
                    conditions=(
                        CompareCondition(
                            op="gt",
                            lhs=InstrumentOperand(input_id="S", field="close"),
                            rhs=ConstantOperand(value=0.0),
                        ),
                    ),
                ),
            )
        ),
    )
    res = await evaluate_signal(signal, {}, fetch)

    # Oracle from the SAME BS prices (single ByStrike segment, no roll): qty sized
    # off NAV at day 0 / BS[0]; daily $ pnl = sign*qty*(BS[t]-BS[t-1]); short=-1.
    bs = np.array(
        [
            _KERNEL.price_put(_FUT[d], _STRIKE, (_APR - d).days / 365.0, 0.0, _IV[d])
            for d in _DATES
        ]
    )
    nav = np.empty(len(_DATES))
    nav[0] = 1_000_000.0
    qty = 1.0 * nav[0] / bs[0]  # nav_times=1
    for t in range(1, len(_DATES)):
        nav[t] = nav[t - 1] + (-1.0) * qty * (bs[t] - bs[t - 1])  # short sign
    expected = nav / nav[0]
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-9, atol=1e-12)
    # Reconciliation invariant holds end-to-end.
    total = np.zeros_like(res.equity_ratio)
    for p in res.positions:
        total = total + p.realized_pnl
    np.testing.assert_allclose(total, res.equity_ratio - 1.0, rtol=1e-9, atol=1e-12)


async def test_bs_mid_requires_bulk_reader_legacy_path_raises():
    """The legacy per-date path does not implement the computed bs_mid extraction
    → loud ValueError (production always wires the bulk reader)."""
    with pytest.raises(ValueError, match="bs_mid stream requires the bulk"):
        await resolve_option_stream(
            dates=_DATES,
            collection="OPT_SP_500",
            option_type="P",
            cycle=None,
            maturity=_MATURITY,
            selection=_SELECTION,
            stream="bs_mid",
            chain_reader=FakeChainReader(_build_chains()),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=_make_underlying_resolver(),
            bulk_chain_reader=None,  # legacy path
        )
