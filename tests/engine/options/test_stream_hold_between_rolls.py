"""Tests for the ``hold_between_rolls`` select-and-hold option-stream mode in
``tcg.engine.options.series.stream_resolver``.

Background (the bug this fixes)
-------------------------------
A ``ByDelta`` option stream RE-SELECTS the contract nearly every day because the
target-delta *strike* moves with spot.  The default resolver emits that daily-
churned mid LEVEL series; downstream ``signal_exec`` books ``Δprice/price``
blindly, so each contract switch injects a spurious price-gap "return" and the
equity curve is meaningless (measured live: 59.9% of days switch contract).

The fix (``hold_between_rolls=True``, DEFAULT-OFF) — FIXED-CONTRACT $-P&L
-----------------------------------------------------------------------
When ON, the resolver selects the contract ONCE at each ROLL (a roll = the
maturity target's *expiration* change, matching ``derive_rolls``), FREEZES it
between rolls, and emits — PER DATE — the HELD-CONTRACT PREMIUM (mid) LEVEL of the
contract that OWNS the step ending on that date (the OLD contract on the roll day),
PLUS roll info (``is_roll`` segment-start markers + each segment's roll-day OPEN
premium).  ``signal_exec`` runs the fixed-contract dollar-P&L recurrence over that
(see ``test_signal_exec_option_hold_pnl.py``): a held quantity is sized once per
roll off the compounding NAV and the roll premium, daily $ P&L is
``qty·Δpremium`` (short sign folded via the block weight), realise+resize at the
next roll.  This matches the ground-truth Java close+reopen sim (the ORACLE
``java_faithful_s1``) EXACTLY, and it never stitches/ratio-adjusts the option
series (which would court the hard "no ratio-adjustment for options" constraint).

These tests use the synthetic bulk-path fakes (no dwh) and prove:
  (a) DEFAULT-OFF is byte-identical to the current stitched daily-reselect series;
  (b) HOLD-ON freezes the contract between rolls (no daily strike churn);
  (c) within a hold the emitted VALUE = the held contract's OWN mid LEVEL;
  (d) the roll boundary is oracle-exact & seam-free — the roll-day value is the
      OLD contract's mid ON the roll day (so its Δpremium into the roll is the
      OLD's own move), and the NEW segment's roll-day OPEN premium is surfaced in
      the roll info (never the raw old→new premium-level gap);
  (e) the roll-info arrays (``is_roll`` + ``roll_premium``) mark every segment
      start with the NEW contract's roll-day open mid.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByDelta,
    ByStrike,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

# Async tests are auto-marked via ``asyncio_mode = "auto"`` (pyproject); the sync
# helper tests at the bottom must NOT be marked, so no module-level pytestmark.


# Two monthly put expirations standing in for a roll: APR (old) → MAY (new).
# BOTH are listed on EVERY trade date (realistic — a May contract is listed in
# March); NearestToTarget(35) flips APR→MAY on 2024-04-01 while APR still has 18
# DTE (so APR still QUOTES on the roll day → the roll-day Δpremium is well-defined).
_APR = date(2024, 4, 19)
_MAY = date(2024, 5, 17)

_DATES = [
    date(2024, 3, 27),  # APR (DTE 23)
    date(2024, 3, 28),  # APR
    date(2024, 3, 29),  # APR (last APR-target day)
    date(2024, 4, 1),  # ROLL: maturity flips to MAY (APR still DTE 18, quoting)
    date(2024, 4, 2),  # MAY
    date(2024, 4, 3),  # MAY
]
_ROLL_IDX = 3  # index where the resolved expiration flips APR→MAY

_STRIKES = (4400, 4450, 4500)
_APR_C = {k: _contract(strike=float(k), expiration=_APR, type_="P") for k in _STRIKES}
_MAY_C = {k: _contract(strike=float(k), expiration=_MAY, type_="P") for k in _STRIKES}

# Per-date, per-strike DELTA for APR puts — engineered so ByDelta(-0.10) churns
# the APR strike day-to-day in the DEFAULT path: d0→4400, d1→4450, d2→4500,
# d3(roll)→4450.  The HELD path freezes the day-0 pick (4400).
_APR_DELTAS = {
    _DATES[0]: {4400: -0.10, 4450: -0.16, 4500: -0.22},  # → 4400
    _DATES[1]: {4400: -0.06, 4450: -0.10, 4500: -0.15},  # → 4450
    _DATES[2]: {4400: -0.04, 4450: -0.07, 4500: -0.10},  # → 4500
    _DATES[3]: {4400: -0.05, 4450: -0.10, 4500: -0.14},  # → 4450
    _DATES[4]: {4400: -0.05, 4450: -0.07, 4500: -0.09},
    _DATES[5]: {4400: -0.04, 4450: -0.06, 4500: -0.08},
}
# MAY put deltas: 4450 = -0.10 on every MAY-target date → ByDelta holds 4450.
_MAY_DELTAS = {
    _DATES[0]: {4400: -0.05, 4450: -0.08, 4500: -0.12},
    _DATES[1]: {4400: -0.05, 4450: -0.08, 4500: -0.12},
    _DATES[2]: {4400: -0.05, 4450: -0.08, 4500: -0.12},
    _DATES[3]: {4400: -0.06, 4450: -0.10, 4500: -0.15},  # roll day → MAY 4450
    _DATES[4]: {4400: -0.05, 4450: -0.10, 4500: -0.16},
    _DATES[5]: {4400: -0.04, 4450: -0.10, 4500: -0.17},
}

# APR per-strike mids.  The HELD APR strike is 4400: mids 30,28,26 then STILL
# QUOTES on the roll day at 24 (its move into roll = 24-26).
_APR_MIDS = {
    _DATES[0]: {4400: 30.0, 4450: 40.0, 4500: 55.0},
    _DATES[1]: {4400: 28.0, 4450: 42.0, 4500: 58.0},
    _DATES[2]: {4400: 26.0, 4450: 44.0, 4500: 60.0},
    _DATES[3]: {4400: 24.0, 4450: 46.0, 4500: 63.0},  # roll day: APR 4400 = 24
    _DATES[4]: {4400: 23.0, 4450: 47.0, 4500: 64.0},
    _DATES[5]: {4400: 22.0, 4450: 48.0, 4500: 65.0},
}
# MAY per-strike mids.  The HELD MAY strike is 4450: opens on the roll day at 18,
# then 20, 19 → its roll-day OPEN premium is 18 (the segment's roll_premium).
_MAY_MIDS = {
    _DATES[0]: {4400: 10.0, 4450: 16.0, 4500: 23.0},
    _DATES[1]: {4400: 10.0, 4450: 16.0, 4500: 23.0},
    _DATES[2]: {4400: 10.0, 4450: 16.0, 4500: 23.0},
    _DATES[3]: {4400: 12.0, 4450: 18.0, 4500: 25.0},  # roll day: MAY 4450 = 18
    _DATES[4]: {4400: 13.0, 4450: 20.0, 4500: 27.0},
    _DATES[5]: {4400: 11.0, 4450: 19.0, 4500: 26.0},
}


def _build_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    """Every date carries BOTH APR and MAY strikes (both expirations listed)."""
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


_MATURITY = NearestToTarget(target_dte_days=35)
_BYDELTA = ByDelta(target_delta=-0.10, tolerance=0.20)


async def _resolve(chains, *, selection, maturity, hold_between_rolls, roll_info=None):
    """Drive the bulk path with the synthetic fakes.

    ``available_expirations`` is passed explicitly (as the production signals
    path does via ``list_option_expirations_filtered``) so the NearestToTarget
    probe query is skipped.  ``roll_info`` (optional out-dict) receives the
    hold-mode ``is_roll`` / ``roll_premium`` arrays when hold mode is on.
    """
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=maturity,
        selection=selection,
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[_APR, _MAY],
        hold_between_rolls=hold_between_rolls,
        hold_roll_info_out=roll_info,
    )


async def test_default_off_churns_the_contract_daily():
    """DEFAULT: ByDelta re-selects the strike each day → contract_id churns within
    the APR segment; the emitted series is the daily-selected contract's mid
    LEVEL (unchanged behaviour)."""
    chains = _build_chains()
    v, e, c = await _resolve(
        chains, selection=_BYDELTA, maturity=_MATURITY, hold_between_rolls=False
    )
    assert all(err is None for err in e), e
    # APR-target days: strike churns 4400→4450→4500→4450 (the bug); the roll day
    # (index 3) resolves MAY (single -0.10 match = 4450).
    assert [ct.strike for ct in c[:3]] == [4400.0, 4450.0, 4500.0]
    assert [ct.expiration for ct in c[:3]] == [_APR] * 3
    # Emitted values are the daily-selected contract's mid LEVEL.
    np.testing.assert_allclose(v[0], 30.0)  # APR 4400
    np.testing.assert_allclose(v[1], 42.0)  # APR 4450
    np.testing.assert_allclose(v[2], 60.0)  # APR 4500
    assert c[3].expiration == _MAY and c[3].strike == 4450.0


async def test_default_off_is_byte_identical_to_omitting_the_flag():
    """hold_between_rolls=False == not passing it at all (golden-master discipline)."""
    chains = _build_chains()
    v_off, e_off, c_off = await _resolve(
        chains, selection=_BYDELTA, maturity=_MATURITY, hold_between_rolls=False
    )
    v_base, e_base, c_base = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_BYDELTA,
        stream="mid",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=[_APR, _MAY],
    )
    np.testing.assert_array_equal(v_off, v_base)
    assert e_off == e_base
    assert [None if x is None else x.contract_id for x in c_off] == [
        None if x is None else x.contract_id for x in c_base
    ]


async def test_hold_on_freezes_the_contract_between_rolls():
    """HOLD-ON: contract selected ONCE at the APR roll (day 0 = K4400) and held
    across the APR-target days; switches only at the true APR→MAY roll."""
    chains = _build_chains()
    _v, e, c = await _resolve(
        chains, selection=_BYDELTA, maturity=_MATURITY, hold_between_rolls=True
    )
    assert all(err is None for err in e), e
    # APR-target days (indices 0..2): ONE frozen contract (K4400), no churn.
    apr_ids = {c[i].contract_id for i in range(_ROLL_IDX)}
    assert len(apr_ids) == 1
    assert c[0].strike == 4400.0 and c[0].expiration == _APR
    # MAY segment (roll day onward): ONE frozen contract (K4450).
    may_ids = {c[i].contract_id for i in range(_ROLL_IDX, len(_DATES))}
    assert len(may_ids) == 1
    assert c[_ROLL_IDX].strike == 4450.0 and c[_ROLL_IDX].expiration == _MAY
    # Exactly ONE maturity roll across the window.
    switches = sum(
        1
        for i in range(1, len(c))
        if c[i - 1] is not None
        and c[i] is not None
        and c[i - 1].expiration != c[i].expiration
    )
    assert switches == 1


async def test_hold_on_within_segment_value_is_held_contract_mid_level():
    """(c) Within a hold, ``values[t]`` == the HELD contract's OWN mid LEVEL.
    Held APR strike = 4400 (mids 30,28,26)."""
    chains = _build_chains()
    v, e, _c = await _resolve(
        chains, selection=_BYDELTA, maturity=_MATURITY, hold_between_rolls=True
    )
    assert all(err is None for err in e), e
    np.testing.assert_allclose(v[0], 30.0)  # APR 4400 day 0
    np.testing.assert_allclose(v[1], 28.0)
    np.testing.assert_allclose(v[2], 26.0)


async def test_roll_boundary_value_is_old_contract_mid_and_roll_premium_is_new_open():
    """(d)(e) The roll-day VALUE (index 3) is the OLD (APR K4400) contract's mid ON
    the roll day (24) — so its Δpremium into the roll is the OLD's OWN move (24-26),
    NOT the raw old→new level gap.  The roll info surfaces the NEW (MAY K4450)
    contract's roll-day OPEN premium (18) as the segment's roll_premium."""
    chains = _build_chains()
    roll_info: dict = {}
    v, e, _c = await _resolve(
        chains,
        selection=_BYDELTA,
        maturity=_MATURITY,
        hold_between_rolls=True,
        roll_info=roll_info,
    )
    assert all(err is None for err in e), e

    # Roll-day value (index 3): OLD APR K4400 mid ON the roll day = 24 (NOT 18).
    np.testing.assert_allclose(v[3], 24.0)
    # Post-roll MAY value (index 4): the NEW held MAY K4450 mid = 20.
    np.testing.assert_allclose(v[4], 20.0)
    np.testing.assert_allclose(v[5], 19.0)

    # Roll info: is_roll marks index 0 (initial open) and index 3 (APR→MAY roll).
    is_roll = np.asarray(roll_info["is_roll"], dtype=bool)
    roll_premium = np.asarray(roll_info["roll_premium"], dtype=np.float64)
    assert list(np.where(is_roll)[0]) == [0, _ROLL_IDX]
    # roll_premium at index 0 = APR K4400 open (30); at index 3 = MAY K4450 open (18).
    np.testing.assert_allclose(roll_premium[0], 30.0)
    np.testing.assert_allclose(roll_premium[_ROLL_IDX], 18.0)


async def test_hold_on_bystrike_roll_boundary_values_and_roll_premium():
    """With ByStrike (fixed 4450, no daily churn), HOLD-ON emits the OLD contract's
    mid ON the roll day and the NEW's roll-day open in roll_premium.  Held APR
    K4450 mids 40,42,44,(46 on roll day); MAY K4450 opens at 18."""
    chains = _build_chains()
    sel = ByStrike(strike=4450.0)
    roll_info: dict = {}
    v, e, c = await _resolve(
        chains,
        selection=sel,
        maturity=_MATURITY,
        hold_between_rolls=True,
        roll_info=roll_info,
    )
    assert all(err is None for err in e), e
    assert c[0].strike == 4450.0 and c[0].expiration == _APR
    assert c[_ROLL_IDX].strike == 4450.0 and c[_ROLL_IDX].expiration == _MAY
    # APR K4450 held mid levels.
    np.testing.assert_allclose(v[0], 40.0)
    np.testing.assert_allclose(v[1], 42.0)
    np.testing.assert_allclose(v[2], 44.0)
    # Roll day: OLD APR K4450 mid ON the roll day = 46 (NOT 18).
    np.testing.assert_allclose(v[3], 46.0)
    # NEW MAY K4450 held mid = 20, 19.
    np.testing.assert_allclose(v[4], 20.0)
    np.testing.assert_allclose(v[5], 19.0)
    roll_premium = np.asarray(roll_info["roll_premium"], dtype=np.float64)
    np.testing.assert_allclose(roll_premium[0], 40.0)  # APR open
    np.testing.assert_allclose(roll_premium[_ROLL_IDX], 18.0)  # MAY open


async def test_hold_on_missing_held_mid_is_nan_value_and_keeps_holding():
    """If the HELD contract has no quote on an interior day, that day's VALUE is
    NaN (no bookable P&L for the adjacent steps) but the hold is not broken — the
    same contract is still held and the next quoted day resumes."""
    chains = _build_chains()
    # Punch a hole: remove the held APR K4400 quote on index 1 (mid=None), keep
    # the other strikes so selection on day 0 (which picks 4400) is unaffected.
    d1 = _DATES[1]
    chains[d1] = [
        (_APR_C[4400], _row(row_date=d1, mid=None, delta=_APR_DELTAS[d1][4400])),
        (
            _APR_C[4450],
            _row(row_date=d1, mid=_APR_MIDS[d1][4450], delta=_APR_DELTAS[d1][4450]),
        ),
        (
            _APR_C[4500],
            _row(row_date=d1, mid=_APR_MIDS[d1][4500], delta=_APR_DELTAS[d1][4500]),
        ),
    ] + [
        (_MAY_C[k], _row(row_date=d1, mid=_MAY_MIDS[d1][k], delta=_MAY_DELTAS[d1][k]))
        for k in _STRIKES
    ]
    v, e, c = await _resolve(
        chains, selection=_BYDELTA, maturity=_MATURITY, hold_between_rolls=True
    )
    # Held contract is still K4400 across the APR segment.
    assert all(c[i].strike == 4400.0 for i in range(_ROLL_IDX))
    # Index 1 (missing quote) → NaN value + missing diagnostic.
    assert np.isnan(v[1])
    assert e[1] == "missing_mid"
    # Index 0 and 2 still carry the held mid level.
    np.testing.assert_allclose(v[0], 30.0)
    np.testing.assert_allclose(v[2], 26.0)
    # Index 3 (roll) is unaffected by the idx-1 hole: OLD APR K4400 mid = 24.
    np.testing.assert_allclose(v[3], 24.0)


async def test_hold_requires_bulk_reader_legacy_path_raises():
    """The legacy per-date path (no bulk reader) cannot honour select-and-hold;
    asking for it must fail LOUDLY, not silently return the daily-reselect series."""
    import pytest

    chains = _build_chains()
    with pytest.raises(ValueError, match="hold_between_rolls requires the bulk"):
        await resolve_option_stream(
            dates=_DATES,
            collection="OPT_SP_500",
            option_type="P",
            cycle=None,
            maturity=_MATURITY,
            selection=_BYDELTA,
            stream="mid",
            chain_reader=FakeChainReader(chains),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=None,  # legacy path
            hold_between_rolls=True,
        )


# ---------------------------------------------------------------------------
# Pure helpers (unit-level, no resolver plumbing)
# ---------------------------------------------------------------------------


def test_hold_segments_splits_on_expiration_change_only():
    from tcg.engine.options.series.stream_resolver import _hold_segments

    e1 = date(2024, 4, 19)
    e2 = date(2024, 5, 17)
    queryable = [
        (0, date(2024, 3, 1)),
        (1, date(2024, 3, 2)),
        (2, date(2024, 3, 3)),
        (3, date(2024, 3, 4)),
        (4, date(2024, 3, 5)),
    ]
    # idx 0,1 → e1; idx 2 → None (dropped, no split); idx 3,4 → e2.
    expirations = {0: e1, 1: e1, 2: None, 3: e2, 4: e2}
    segs = _hold_segments(queryable, expirations)
    idx_runs = [[i for i, _d in seg] for seg in segs]
    assert idx_runs == [[0, 1], [3, 4]]
