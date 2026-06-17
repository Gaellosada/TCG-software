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
from typing import Literal, Sequence

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByStrike,
    FixedDate,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

# Two monthly expirations standing in for a roll: APR (old) → MAY (new).
_APR = date(2024, 4, 19)
_MAY = date(2024, 5, 17)


def _contract(
    *,
    strike: float,
    expiration: date,
    type_: Literal["C", "P"] = "C",
    cycle: str = "M",
    collection: str = "OPT_SP_500",
) -> OptionContractDoc:
    cid = f"{collection}_K{int(strike)}_{type_}_{expiration.isoformat()}_{cycle}"
    return OptionContractDoc(
        collection=collection,
        contract_id=cid,
        root_underlying="IND_SP_500",
        underlying_ref="FUT_SP_500_EMINI",
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle=cycle,
        strike=float(strike),
        type=type_,
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _row(*, row_date: date, mid: float = 10.0) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=mid - 0.05,
        ask=mid + 0.05,
        bid_size=None,
        ask_size=None,
        volume=None,
        open_interest=None,
        mid=mid,
        iv_stored=0.20,
        delta_stored=0.50,
        gamma_stored=None,
        theta_stored=None,
        vega_stored=None,
        underlying_price_stored=None,
    )


class FakeBulkChainReader:
    """Bulk chain reader returning synthetic chains keyed by date."""

    def __init__(
        self,
        chains_by_date: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        self._chains = chains_by_date

    async def query_chain_bulk(
        self,
        *,
        root: str,
        dates: Sequence[date],
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates:
            filtered = [
                (c, r)
                for (c, r) in self._chains.get(d, [])
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and (expiration_cycle is None or c.expiration_cycle == expiration_cycle)
            ]
            if filtered:
                result[d] = filtered
        return result


class FakeChainReader:
    """Per-date chain reader — also serves the NearestToTarget probe query."""

    def __init__(
        self,
        chains_by_date: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        self._chains = chains_by_date

    async def query_chain(
        self,
        *,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        return [
            (c, r)
            for (c, r) in self._chains.get(date, [])
            if (c.type == type or type == "both")
            and expiration_min <= c.expiration <= expiration_max
            and (expiration_cycle is None or c.expiration_cycle == expiration_cycle)
        ]


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


async def _resolve(dates, chains, *, roll_offset, maturity=None):
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=maturity or NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
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
