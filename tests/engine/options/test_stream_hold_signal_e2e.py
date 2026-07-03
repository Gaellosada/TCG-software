"""End-to-end: a short-put option signal with ``hold_between_rolls=True`` produces
the correct FIXED-CONTRACT DOLLAR-P&L equity through the FULL pipeline —
``resolve_option_stream`` (bulk path, synthetic chains) → the fetcher side-channel
→ ``evaluate_signal``'s hold-mode dollar-P&L accounting.

This is the load-bearing integration test the orchestrator live-validates against
ground truth: it wires the REAL resolver behind a ``PriceFetcher`` (whose
``fetch_hold_roll_info`` surfaces the resolver's ``is_roll`` / ``roll_premium``
out-dict) and runs it through ``evaluate_signal``:

  * DEFAULT (hold_between_rolls=False): the ByDelta strike churns daily, the
    emitted mid LEVEL jumps between contracts, and ``signal_exec``'s Δprice/price
    books spurious gap "returns" → the equity is contaminated.
  * HOLD (hold_between_rolls=True): the resolver emits the per-date HELD-contract
    premium LEVEL + roll info, and ``signal_exec`` runs the fixed-contract
    dollar-P&L recurrence → the equity equals the independent Java-faithful ORACLE
    (``java_faithful_s1``) accounting EXACTLY.

The two equities MUST differ (the fix changes the P&L), and the HOLD equity MUST
equal the oracle.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.engine.signal_exec import evaluate_signal
from tcg.types.options import (
    ByDelta,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)
from tcg.types.signal import (
    Block,
    CompareCondition,
    ConstantOperand,
    InstrumentOperand,
    InstrumentOptionStream,
    InstrumentSpot,
    Input,
    Signal,
    SignalRules,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

# Async tests are auto-marked via ``asyncio_mode = "auto"`` (pyproject).


# Oracle-exact roll fixture (same shape as test_stream_hold_between_rolls): BOTH
# APR and MAY are listed on every date; NearestToTarget(35) flips APR→MAY on
# 2024-04-01 while APR still quotes (DTE 18).  Held APR K4400 mids 30,28,26,(24
# on roll day); held MAY K4450 mids 18(roll day open),20,19.  ByDelta churns the
# APR strike 4400→4450→4500 in the DEFAULT path.
_APR = date(2024, 4, 19)
_MAY = date(2024, 5, 17)
_DATES = [
    date(2024, 3, 27),
    date(2024, 3, 28),
    date(2024, 3, 29),
    date(2024, 4, 1),  # ROLL day (APR still quoting)
    date(2024, 4, 2),
    date(2024, 4, 3),
]
_STRIKES = (4400, 4450, 4500)
_APR_C = {k: _contract(strike=float(k), expiration=_APR, type_="P") for k in _STRIKES}
_MAY_C = {k: _contract(strike=float(k), expiration=_MAY, type_="P") for k in _STRIKES}
_APR_DELTAS = {
    _DATES[0]: {4400: -0.10, 4450: -0.16, 4500: -0.22},
    _DATES[1]: {4400: -0.06, 4450: -0.10, 4500: -0.15},
    _DATES[2]: {4400: -0.04, 4450: -0.07, 4500: -0.10},
    _DATES[3]: {4400: -0.05, 4450: -0.10, 4500: -0.14},
    _DATES[4]: {4400: -0.05, 4450: -0.07, 4500: -0.09},
    _DATES[5]: {4400: -0.04, 4450: -0.06, 4500: -0.08},
}
_MAY_DELTAS = {
    _DATES[0]: {4400: -0.05, 4450: -0.08, 4500: -0.12},
    _DATES[1]: {4400: -0.05, 4450: -0.08, 4500: -0.12},
    _DATES[2]: {4400: -0.05, 4450: -0.08, 4500: -0.12},
    _DATES[3]: {4400: -0.06, 4450: -0.10, 4500: -0.15},
    _DATES[4]: {4400: -0.05, 4450: -0.10, 4500: -0.16},
    _DATES[5]: {4400: -0.04, 4450: -0.10, 4500: -0.17},
}
_APR_MIDS = {
    _DATES[0]: {4400: 30.0, 4450: 40.0, 4500: 55.0},
    _DATES[1]: {4400: 28.0, 4450: 42.0, 4500: 58.0},
    _DATES[2]: {4400: 26.0, 4450: 44.0, 4500: 60.0},
    _DATES[3]: {4400: 24.0, 4450: 46.0, 4500: 63.0},
    _DATES[4]: {4400: 23.0, 4450: 47.0, 4500: 64.0},
    _DATES[5]: {4400: 22.0, 4450: 48.0, 4500: 65.0},
}
_MAY_MIDS = {
    _DATES[0]: {4400: 10.0, 4450: 16.0, 4500: 23.0},
    _DATES[1]: {4400: 10.0, 4450: 16.0, 4500: 23.0},
    _DATES[2]: {4400: 10.0, 4450: 16.0, 4500: 23.0},
    _DATES[3]: {4400: 12.0, 4450: 18.0, 4500: 25.0},
    _DATES[4]: {4400: 13.0, 4450: 20.0, 4500: 27.0},
    _DATES[5]: {4400: 11.0, 4450: 19.0, 4500: 26.0},
}


def _build_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in _DATES:
        rows = [
            (_APR_C[k], _row(row_date=d, mid=_APR_MIDS[d][k], delta=_APR_DELTAS[d][k]))
            for k in _STRIKES
        ]
        rows += [
            (_MAY_C[k], _row(row_date=d, mid=_MAY_MIDS[d][k], delta=_MAY_DELTAS[d][k]))
            for k in _STRIKES
        ]
        chains[d] = rows
    return chains


_DATES_INT = np.array([int(d.strftime("%Y%m%d")) for d in _DATES], dtype=np.int64)
_MATURITY = NearestToTarget(target_dte_days=35)
_SELECTION = ByDelta(target_delta=-0.10, tolerance=0.20)


def _make_fetcher(chains, *, spx_series):
    """PriceFetcher that runs the REAL resolver for the option leg (returning its
    held premium LEVEL) and, via ``fetch_hold_roll_info``, the resolver's roll-info
    out-dict — the exact production wiring shape."""

    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            return _DATES_INT, spx_series
        if isinstance(instrument, InstrumentOptionStream):
            values, _diag, _contracts = await resolve_option_stream(
                dates=_DATES,
                collection=instrument.collection,
                option_type=instrument.option_type,
                cycle=instrument.cycle,
                maturity=instrument.maturity,
                selection=instrument.selection,
                stream=instrument.stream,
                chain_reader=FakeChainReader(chains),
                maturity_resolver=DefaultMaturityResolver(),
                underlying_price_resolver=None,
                bulk_chain_reader=FakeBulkChainReader(chains),
                available_expirations=[_APR, _MAY],
                hold_between_rolls=instrument.hold_between_rolls,
            )
            return _DATES_INT, values
        raise KeyError(f"no data for {instrument!r} ({field})")

    async def fetch_hold_roll_info(instrument):
        roll_info: dict = {}
        await resolve_option_stream(
            dates=_DATES,
            collection=instrument.collection,
            option_type=instrument.option_type,
            cycle=instrument.cycle,
            maturity=instrument.maturity,
            selection=instrument.selection,
            stream=instrument.stream,
            chain_reader=FakeChainReader(chains),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=FakeBulkChainReader(chains),
            available_expirations=[_APR, _MAY],
            hold_between_rolls=True,
            hold_roll_info_out=roll_info,
        )
        return _DATES_INT, roll_info["is_roll"], roll_info["roll_premium"]

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
    return fetch


def _short_put_signal(*, hold: bool, nav_times: float = 1.0) -> Signal:
    """Always-latched SHORT put on the option-stream input + a spot input whose
    always-true condition latches the entry from bar 0."""
    opt = InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream="mid",
        hold_between_rolls=hold,
        nav_times=nav_times,
    )
    return Signal(
        id="s_hold",
        name="short put hold e2e",
        inputs=(
            Input(id="P", instrument=opt),
            Input(
                id="S",
                instrument=InstrumentSpot(collection="INDEX", instrument_id="SPX"),
            ),
        ),
        rules=SignalRules(
            entries=(
                Block(
                    id="e1",
                    input_id="P",
                    weight=-10.0,  # SHORT; direction only — size is nav_times
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


def _oracle_ratio(owner_prev, owner_cur, is_roll, roll_premium, *, nav_times, sign):
    """Java-faithful fixed-contract dollar-P&L NAV → base-1 ratio (see the oracle
    ``java_faithful_s1``); ``sign`` = direction (short short-put = -1)."""
    T = len(owner_cur)
    nav = np.empty(T)
    nav[0] = 1_000_000.0
    qty = nav_times * nav[0] / roll_premium[0]
    for t in range(1, T):
        dprem = owner_cur[t] - owner_prev[t]
        if not np.isfinite(dprem):
            dprem = 0.0
        nav[t] = nav[t - 1] + sign * qty * dprem
        if bool(is_roll[t]):
            qty = nav_times * nav[t] / roll_premium[t]
    return nav / nav[0]


async def test_hold_signal_equity_matches_oracle_and_differs_from_default():
    chains = _build_chains()
    spx = np.full(len(_DATES), 100.0, dtype=np.float64)  # always > 0 → latched

    res_hold = await evaluate_signal(
        _short_put_signal(hold=True), {}, _make_fetcher(chains, spx_series=spx)
    )
    res_off = await evaluate_signal(
        _short_put_signal(hold=False), {}, _make_fetcher(chains, spx_series=spx)
    )

    # ── Independent Java-faithful oracle over the held-contract step owners ──
    # Held APR K4400 30,28,26,24(roll-day OLD mid); MAY K4450 18(open),20,19.
    #   step owners: t1 APR 30->28, t2 28->26, t3 26->24 (OLD into roll),
    #                t4 MAY 18->20, t5 20->19 ; roll_premium = [30,·,·,18,·,·]
    owner_prev = np.array([np.nan, 30.0, 28.0, 26.0, 18.0, 20.0])
    owner_cur = np.array([np.nan, 28.0, 26.0, 24.0, 20.0, 19.0])
    is_roll = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    roll_premium = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
    expected_equity = _oracle_ratio(
        owner_prev, owner_cur, is_roll, roll_premium, nav_times=1.0, sign=-1.0
    )

    np.testing.assert_allclose(
        res_hold.equity_ratio, expected_equity, rtol=1e-9, atol=1e-12
    )

    # The fix CHANGES the P&L: the default (churned) equity must differ.
    assert not np.allclose(res_hold.equity_ratio, res_off.equity_ratio), (
        "hold-mode equity is identical to the churned default — the fix had no effect"
    )
    assert not np.allclose(res_off.equity_ratio, expected_equity)

    # Reconciliation invariant holds end-to-end (the subtle NAV-coupling risk).
    total = np.zeros_like(res_hold.equity_ratio)
    for pr in res_hold.positions:
        total = total + pr.realized_pnl
    np.testing.assert_allclose(
        total, res_hold.equity_ratio - 1.0, rtol=1e-9, atol=1e-12
    )


async def test_hold_signal_navtimes_scales_pnl_matches_oracle():
    """nav_times=2.5 (leverage the premium notional) still matches the oracle."""
    chains = _build_chains()
    spx = np.full(len(_DATES), 100.0, dtype=np.float64)
    res = await evaluate_signal(
        _short_put_signal(hold=True, nav_times=2.5),
        {},
        _make_fetcher(chains, spx_series=spx),
    )
    owner_prev = np.array([np.nan, 30.0, 28.0, 26.0, 18.0, 20.0])
    owner_cur = np.array([np.nan, 28.0, 26.0, 24.0, 20.0, 19.0])
    is_roll = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
    roll_premium = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
    expected = _oracle_ratio(
        owner_prev, owner_cur, is_roll, roll_premium, nav_times=2.5, sign=-1.0
    )
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-9, atol=1e-12)


async def test_hold_signal_position_is_latched_short_throughout():
    """Guard the test's own premise: the short position is latched (sign<0) across
    the whole window in BOTH modes, so the equity difference is purely the
    resolver+accounting change, not a position-latching difference."""
    chains = _build_chains()
    spx = np.full(len(_DATES), 100.0, dtype=np.float64)
    for hold in (True, False):
        res = await evaluate_signal(
            _short_put_signal(hold=hold), {}, _make_fetcher(chains, spx_series=spx)
        )
        (pos_result,) = [p for p in res.positions if p.input_id == "P"]
        assert np.all(pos_result.values < 0.0)
