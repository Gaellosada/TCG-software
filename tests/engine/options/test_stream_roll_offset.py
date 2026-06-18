"""Tests for the options roll offset ("roll N days earlier") in
``tcg.engine.options.series.stream_resolver``.

``OptionStreamRef.roll_offset`` shifts maturity resolution forward by N
calendar days — the resolver resolves the maturity rule as of
``date + roll_offset`` — so every roll happens N days sooner (mirrors the
futures ``rollOffset``).  These tests drive the bulk path with a faked chain
reader (same harness as ``test_stream_mid_adjustment.py``) over a
``NearestToTarget`` roll where the offset flips which expiration is selected
on a boundary date.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByDelta,
    ByStrike,
    FixedDate,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

# Two monthly expirations standing in for a roll: APR (old) → MAY (new).
_APR = date(2024, 4, 19)
_MAY = date(2024, 5, 17)


# K4500 present in BOTH expirations on every date (APR mid 10, MAY mid 20 — so
# the series value alone reveals which expiration was selected).
_K_APR = _contract(strike=4500, expiration=_APR)
_K_MAY = _contract(strike=4500, expiration=_MAY)


def _both_exp_chains(
    dates: Sequence[date],
) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    return {
        d: [
            (_K_APR, _row(row_date=d, mid=10.0)),
            (_K_MAY, _row(row_date=d, mid=20.0)),
        ]
        for d in dates
    }


async def _resolve(dates, chains, *, roll_offset, maturity=None, selection=None):
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=maturity or NearestToTarget(target_dte_days=30),
        selection=selection or ByStrike(strike=4500.0),
        stream="mid",
        adjustment="none",
        roll_offset=roll_offset,
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
    )


def test_resolver_math_boundary_shifts_with_offset():
    """Pin the assumption the e2e tests rely on: NearestToTarget(30) over
    [APR, MAY] selects APR at ref_date 2024-04-01 but MAY at 2024-04-06
    (= 04-01 + 5).  So shifting ref_date forward by 5 rolls APR→MAY early."""
    r = DefaultMaturityResolver()
    rule = NearestToTarget(target_dte_days=30)
    avail = [_APR, _MAY]
    assert (
        r.resolve_with_chain(
            ref_date=date(2024, 4, 1), rule=rule, available_expirations=avail
        )
        == _APR
    )
    assert (
        r.resolve_with_chain(
            ref_date=date(2024, 4, 6), rule=rule, available_expirations=avail
        )
        == _MAY
    )


async def test_roll_offset_rolls_earlier():
    """On the boundary date 2024-04-01: roll_offset=0 still holds APR, but
    roll_offset=5 has already rolled to MAY (5 calendar days early)."""
    dates = [date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 8)]
    chains = _both_exp_chains(dates)

    v0, e0, c0 = await _resolve(dates, chains, roll_offset=0)
    v5, e5, c5 = await _resolve(dates, chains, roll_offset=5)

    assert all(e is None for e in e0) and all(e is None for e in e5)
    # 2024-04-01 is the discriminating date.
    assert c0[1].expiration == _APR
    assert c5[1].expiration == _MAY
    # Series value follows the selected contract's mid (APR=10, MAY=20).
    assert v0[1] == 10.0
    assert v5[1] == 20.0
    # Endpoints agree: both still APR on 03-28, both already MAY on 04-08.
    assert c0[0].expiration == _APR and c5[0].expiration == _APR
    assert c0[2].expiration == _MAY and c5[2].expiration == _MAY


async def test_roll_offset_zero_is_baseline():
    """roll_offset=0 is identical to omitting it (the default)."""
    dates = [date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 8)]
    chains = _both_exp_chains(dates)

    v0, _e0, c0 = await _resolve(dates, chains, roll_offset=0)
    # Default path (roll_offset not passed at all).
    vd, _ed, cd = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        adjustment="none",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
    )
    np.testing.assert_array_equal(v0, vd)
    assert [c.expiration for c in c0] == [c.expiration for c in cd]


async def test_roll_offset_noop_for_fixed_date():
    """FixedDate targets one absolute expiration → the offset changes nothing."""
    dates = [date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 8)]
    chains = _both_exp_chains(dates)
    fixed = FixedDate(date=_MAY)

    _v0, _e0, c0 = await _resolve(dates, chains, roll_offset=0, maturity=fixed)
    _v20, _e20, c20 = await _resolve(dates, chains, roll_offset=20, maturity=fixed)

    assert all(c.expiration == _MAY for c in c0)
    assert all(c.expiration == _MAY for c in c20)


# ── roll_offset touches the EXPIRATION only, never the STRIKE ────────────


# A symmetric delta surface present in BOTH expirations: K4500 is the ATM
# (delta 0.50) leg in each.  ByDelta(0.50) therefore selects K4500 regardless
# of which expiration the maturity rule lands on — so if roll_offset shifts the
# strike (a bug) the selected strike would move off 4500.  Distinct mids per
# (expiration, strike) make the selected contract unambiguous.
_DELTA_BY_STRIKE = {4400: 0.62, 4500: 0.50, 4600: 0.38}


def _multi_strike_both_exp_chains(
    dates,
) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    """Each date carries all three strikes in BOTH APR and MAY.

    Mid encodes (expiration, strike) so the chosen contract is identifiable:
    APR strikes get mid = strike/100 (44/45/46), MAY strikes get that + 100
    (144/145/146).  The delta of a given strike is identical across
    expirations, so ByDelta's strike pick must NOT depend on the expiration.
    """
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in dates:
        rows: list[tuple[OptionContractDoc, OptionDailyRow]] = []
        for strike, delta in _DELTA_BY_STRIKE.items():
            for exp, mid_base in ((_APR, strike / 100.0), (_MAY, strike / 100.0 + 100)):
                rows.append(
                    (
                        _contract(strike=strike, expiration=exp),
                        _row(row_date=d, mid=mid_base, delta=delta),
                    )
                )
        chains[d] = rows
    return chains


async def test_roll_offset_shifts_expiration_not_strike_under_by_delta():
    """On the boundary date, roll_offset flips the selected EXPIRATION
    (APR→MAY) while ByDelta keeps the SAME selected STRIKE (4500).

    The existing ByStrike tests pin the strike by construction (it is the
    selection criterion), so they cannot show that the offset leaves a
    *selection-derived* strike untouched.  ByDelta derives the strike from the
    chain, so an offset that leaked into strike selection would surface here.
    """
    dates = [date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 8)]
    chains = _multi_strike_both_exp_chains(dates)
    by_delta = ByDelta(target_delta=0.50, tolerance=0.05, strict=False)

    _v0, e0, c0 = await _resolve(dates, chains, roll_offset=0, selection=by_delta)
    _v5, e5, c5 = await _resolve(dates, chains, roll_offset=5, selection=by_delta)

    assert all(e is None for e in e0) and all(e is None for e in e5)

    # 2024-04-01 is the discriminating date (APR with offset 0, MAY with 5).
    sel0, sel5 = c0[1], c5[1]
    # The EXPIRATION moved with the offset ...
    assert sel0.expiration == _APR
    assert sel5.expiration == _MAY
    assert sel0.expiration != sel5.expiration
    # ... but the STRIKE selected by delta did NOT (0.50 → 4500 in both).
    assert sel0.strike == 4500.0
    assert sel5.strike == 4500.0
    assert sel0.strike == sel5.strike

    # Endpoints: same strike on both ends too (only the expiration ever moves).
    assert c0[0].strike == 4500.0 and c5[0].strike == 4500.0
    assert c0[2].strike == 4500.0 and c5[2].strike == 4500.0


# ── Guard: roll_offset / adjustment require the bulk chain reader ────────


async def test_roll_offset_without_bulk_reader_raises():
    """The legacy per-date path (no bulk reader) cannot apply roll_offset, so
    requesting it raises rather than silently returning an unshifted series."""
    dates = [date(2024, 3, 28), date(2024, 4, 1)]
    chains = _both_exp_chains(dates)
    with pytest.raises(ValueError, match="require the bulk chain reader"):
        await resolve_option_stream(
            dates=dates,
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NearestToTarget(target_dte_days=30),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            adjustment="none",
            roll_offset=5,  # non-zero offset with NO bulk reader → must raise.
            chain_reader=FakeChainReader(chains),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=None,
        )


async def test_adjustment_without_bulk_reader_raises():
    """The legacy per-date path cannot back-adjust mids, so a non-'none'
    adjustment without a bulk reader raises (same guard, adjustment arm)."""
    dates = [date(2024, 3, 28), date(2024, 4, 1)]
    chains = _both_exp_chains(dates)
    with pytest.raises(ValueError, match="require the bulk chain reader"):
        await resolve_option_stream(
            dates=dates,
            collection="OPT_SP_500",
            option_type="C",
            cycle=None,
            maturity=NearestToTarget(target_dte_days=30),
            selection=ByStrike(strike=4500.0),
            stream="mid",
            adjustment="ratio",  # real adjustment with NO bulk reader → raise.
            roll_offset=0,
            chain_reader=FakeChainReader(chains),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=None,
        )


async def test_legacy_path_ok_without_bulk_reader_when_defaults():
    """Sanity: the legacy path still WORKS with default roll_offset/adjustment
    and no bulk reader — the guard does not break the supported fallback."""
    dates = [date(2024, 3, 28), date(2024, 4, 1)]
    chains = _both_exp_chains(dates)
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=None,  # legacy path, defaults → no raise.
    )
    assert all(e is None for e in errors)
    assert all(c is not None for c in contracts)
    np.testing.assert_array_equal(values, [10.0, 10.0])
