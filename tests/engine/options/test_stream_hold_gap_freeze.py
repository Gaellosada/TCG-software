"""Regression: a hold segment whose OPEN (roll) date is a globally-missing mark
day must NOT freeze the whole segment.

Live bug (portfolio "Short S&P 10 delta put 2M DB v2"): the
``NextThirdFriday(offset_months=2)`` + ``RollOffset(2 days)`` maturity rolled the
option-stream leg to a NEW expiration whose FIRST (segment-open) trade date landed
exactly on 2020-02-28 — a day globally absent from v2 W3 marks.  Because the
per-segment selection was pinned to the segment's first date, the empty chain made
selection FAIL, ``seg_states[seg]`` stayed ``None``, the marking pass skipped the
whole segment, and every date from the gap to the NEXT roll got a NaN value → the
portfolio equity FROZE flat through the entire COVID crash (hiding the short-put
loss), un-freezing only at the next roll.

The fix defers the segment OPEN / selection to the first date in the segment that
actually has a quotable chain (forward-fill spans ONLY the genuinely-missing days,
then re-syncs to the real marks).  These deterministic tests pin that behaviour on
BOTH the two-phase hold path and the full-chain hold path — no warehouse.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByDelta,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    RollOffset,
)

from _stream_fakes import _contract, _row
from test_stream_hold_two_phase import _FullChainHoldReader, _TwoPhaseReader

# --------------------------------------------------------------------------- #
# Scenario: two monthly hold segments; segment 2's OPEN date is a data gap.
#
#   SEG1 (exp E1) : 02-25, 02-26, 02-27
#   SEG2 (exp E2) : 02-28 (GLOBAL GAP — no marks), 03-02, 03-20
#
# E2's held -0.10Δ put EXPLODES through the crash (a short put's loss).  The
# engine must mark E2's ACTUAL closes on 03-02 / 03-20, not freeze to NaN.
# --------------------------------------------------------------------------- #
_E1 = date(2020, 4, 17)
_E2 = date(2020, 5, 15)

_SEG1 = [date(2020, 2, 25), date(2020, 2, 26), date(2020, 2, 27)]
_GAP = date(2020, 2, 28)
# 03-02 is the segment-2 OPEN (roll) date; 03-05 / 03-20 are interior held days
# whose value is UNAMBIGUOUSLY the NEW held contract's own mid.
_ROLL = date(2020, 3, 2)
_SEG2_INTERIOR = [date(2020, 3, 5), date(2020, 3, 20)]
_SEG2_LIVE = [_ROLL] + _SEG2_INTERIOR
_DATES = _SEG1 + [_GAP] + _SEG2_LIVE

# Per-date listed-expiration map drives the NearestToTarget roll: SEG1 dates list
# E1; the GAP date 02-28 AND the SEG2 live dates list E2 — so the resolved
# expiration ROLLS to E2 on 02-28, making the GAP DAY itself the segment-2 OPEN
# (exactly the live NextThirdFriday month-boundary roll that landed the open on a
# globally-missing mark day).  02-28 is in the map (its expiration resolves) but
# DELIBERATELY absent from the CHAINS (no marks that day).
_BY_DATE = {
    **{d: [_E1] for d in _SEG1},
    _GAP: [_E2],
    **{d: [_E2] for d in _SEG2_LIVE},
}

# A 3-rung put ladder; the -0.10Δ winner is the middle strike.
_LADDER_DELTAS = [(-0.02, 3000.0), (-0.10, 2900.0), (-0.30, 2700.0)]

# E2 held put (2900, -0.10Δ) closes: a SHORT put explodes in the crash.
_E2_CLOSE = {_ROLL: 93.5, date(2020, 3, 5): 300.0, date(2020, 3, 20): 694.25}
# E1 closes (steady, pre-roll).
_E1_CLOSE = 20.0


def _ladder_rows(
    d: date, exp: date, closes: dict[float, float]
) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
    rows: list[tuple[OptionContractDoc, OptionDailyRow]] = []
    for dlt, k in _LADDER_DELTAS:
        c = closes[k]
        rows.append(
            (
                _contract(strike=k, expiration=exp, type_="P"),
                _row(row_date=d, mid=c, delta=dlt, close=c),
            )
        )
    return rows


def _gap_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    # SEG1: E1 chain on each date (steady closes).
    for d in _SEG1:
        chains[d] = _ladder_rows(d, _E1, {3000.0: _E1_CLOSE, 2900.0: _E1_CLOSE, 2700.0: _E1_CLOSE})
    # GAP date 02-28: NO rows at all (globally missing marks) — omit from chains.
    # SEG2 live dates: E2 chain, held 2900 put explodes.
    for d in _SEG2_LIVE:
        held = _E2_CLOSE[d]
        chains[d] = _ladder_rows(d, _E2, {3000.0: held * 0.3, 2900.0: held, 2700.0: held * 2.0})
    return chains


async def _resolve(reader):
    roll_info: dict = {}
    values, errors, contracts = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=40),
        selection=ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream="close",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=[_E1, _E2],
        available_expirations_by_date=_BY_DATE,
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
    )
    return values, errors, contracts, roll_info


def _idx(d: date) -> int:
    return _DATES.index(d)


class TestHoldSegmentOpenOnGapDoesNotFreeze:
    async def test_two_phase_tracks_post_gap_marks(self):
        two = _TwoPhaseReader(_gap_chains())
        values, errors, contracts, roll_info = await _resolve(two)
        v = np.asarray(values, dtype=np.float64)
        # The gap day itself is genuinely missing → NaN is acceptable (≤1 day).
        assert np.isnan(v[_idx(_GAP)])
        # The crux: post-gap INTERIOR marks must be the E2 held put's ACTUAL
        # closes, NOT frozen to NaN (interior value == the held contract's own mid).
        assert v[_idx(date(2020, 3, 5))] == 300.0, f"03-05 frozen: {v[_idx(date(2020,3,5))]}"
        assert v[_idx(date(2020, 3, 20))] == 694.25, (
            f"03-20 frozen: {v[_idx(date(2020,3,20))]}"
        )
        # The segment OPENED (a roll marker) on the first quotable date 03-02, and
        # its roll-day OPEN premium is the NEW E2 held contract's own 03-02 mid.
        ri = roll_info["is_roll"]
        assert ri[_idx(_ROLL)] == 1.0
        assert roll_info["roll_premium"][_idx(_ROLL)] == 93.5
        # Segment 2 is genuinely held: the E2 -0.10Δ (2900) put.
        held_ids = {c.contract_id for c in contracts if c is not None}
        assert any("2900" in cid and "2020-05-15" in cid for cid in held_ids)

    async def test_full_chain_tracks_post_gap_marks(self):
        full = _FullChainHoldReader(_gap_chains())
        values, _errors, _contracts, roll_info = await _resolve(full)
        v = np.asarray(values, dtype=np.float64)
        assert v[_idx(date(2020, 3, 5))] == 300.0
        assert v[_idx(date(2020, 3, 20))] == 694.25
        assert roll_info["is_roll"][_idx(_ROLL)] == 1.0


# --------------------------------------------------------------------------- #
# Scenario: MULTI-day leading gap on segment 2's open (perf regression).
#
#   SEG1 (exp E1) : 02-25, 02-26, 02-27
#   SEG2 (exp E2) : 02-28, 03-02, 03-03 (ALL globally-missing) → 03-04 OPEN,
#                   then 03-05, 03-20 (interior held days)
#
# The freeze fix advances past the THREE missing leading days and opens on the
# first quotable date (03-04).  The per-day open loop probed EACH missing leading
# day as its own single-date ``query_chain_bulk`` (serialized under the bulk
# semaphore) → N sequential dwh round-trips.  The ranged-probe fix must fetch the
# whole leading window in ONE bounded round-trip while selecting the SAME open
# date and producing the SAME marks.
# --------------------------------------------------------------------------- #
_MG_SEG1 = [date(2020, 2, 25), date(2020, 2, 26), date(2020, 2, 27)]
_MG_GAPS = [date(2020, 2, 28), date(2020, 3, 2), date(2020, 3, 3)]
_MG_OPEN = date(2020, 3, 4)
_MG_INTERIOR = [date(2020, 3, 5), date(2020, 3, 20)]
_MG_SEG2_LIVE = [_MG_OPEN] + _MG_INTERIOR
_MG_DATES = _MG_SEG1 + _MG_GAPS + _MG_SEG2_LIVE

_MG_BY_DATE = {
    **{d: [_E1] for d in _MG_SEG1},
    # The maturity ROLLS to E2 on the first gap day (02-28) and stays there — so
    # the whole leading gap belongs to segment 2, exactly like the live roll that
    # landed the open on a run of missing-mark days.
    **{d: [_E2] for d in _MG_GAPS},
    **{d: [_E2] for d in _MG_SEG2_LIVE},
}

# E2 held put (2900, -0.10Δ) closes on the LIVE dates (gap days carry no chain).
_MG_E2_CLOSE = {_MG_OPEN: 93.5, date(2020, 3, 5): 300.0, date(2020, 3, 20): 694.25}


def _mg_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in _MG_SEG1:
        chains[d] = _ladder_rows(d, _E1, {3000.0: _E1_CLOSE, 2900.0: _E1_CLOSE, 2700.0: _E1_CLOSE})
    # _MG_GAPS: NO rows at all (globally missing) — omit from chains.
    for d in _MG_SEG2_LIVE:
        held = _MG_E2_CLOSE[d]
        chains[d] = _ladder_rows(d, _E2, {3000.0: held * 0.3, 2900.0: held, 2700.0: held * 2.0})
    return chains


async def _resolve_mg(reader):
    roll_info: dict = {}
    values, errors, contracts = await resolve_option_stream(
        dates=_MG_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=40),
        selection=ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream="close",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=[_E1, _E2],
        available_expirations_by_date=_MG_BY_DATE,
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
    )
    return values, errors, contracts, roll_info


def _mg_idx(d: date) -> int:
    return _MG_DATES.index(d)


class TestMultiDayLeadingGapRangedProbe:
    async def test_multi_gap_uses_single_ranged_probe(self):
        """The 3-day leading gap must resolve with ONE bounded open-probe fetch,
        not one-per-missing-day (the per-day loop issued 4 serial round-trips)."""
        two = _TwoPhaseReader(_mg_chains())
        values, _errors, contracts, roll_info = await _resolve_mg(two)
        v = np.asarray(values, dtype=np.float64)

        # (1) BOUNDED fetch count: exactly ONE open-probe call for the whole
        # leading gap (per-day probing issued one per missing day → 4).
        assert len(two.bulk_calls) == 1, (
            f"expected 1 ranged open-probe, got {len(two.bulk_calls)} "
            f"single-date probes: {[c['dates'] for c in two.bulk_calls]}"
        )
        # The single probe is BOUNDED to segment 2's own window (no full-history
        # scan) and covers the still-uncached leading gap up to the open date.
        # (02-28, the nominal first date, is pre-seeded empty by Phase 1, so it is
        # already cached and correctly excluded — but the run of missing days AFTER
        # it, 03-02 / 03-03, and the open 03-04 must be in the one ranged call.)
        probe_dates = set(two.bulk_calls[0]["dates"])
        assert probe_dates <= set(_MG_GAPS + _MG_SEG2_LIVE)
        assert {date(2020, 3, 2), date(2020, 3, 3)} <= probe_dates
        assert _MG_OPEN in probe_dates
        assert two.bulk_calls[0]["expiration_min"] == _E2

        # (2) SAME open date + marks as the per-day loop (byte-identical selection):
        # the segment opens on the first quotable date 03-04 …
        assert roll_info["is_roll"][_mg_idx(_MG_OPEN)] == 1.0
        assert roll_info["roll_premium"][_mg_idx(_MG_OPEN)] == 93.5
        # … the gap days stay NaN (genuinely missing) …
        for g in _MG_GAPS:
            assert np.isnan(v[_mg_idx(g)]), f"gap {g} not NaN: {v[_mg_idx(g)]}"
        # … and the post-gap interior marks track the E2 held put's actual closes.
        assert v[_mg_idx(date(2020, 3, 5))] == 300.0
        assert v[_mg_idx(date(2020, 3, 20))] == 694.25
        held_ids = {c.contract_id for c in contracts if c is not None}
        assert any("2900" in cid and "2020-05-15" in cid for cid in held_ids)

    async def test_happy_path_issues_no_open_probe(self):
        """No leading gap → the open loop breaks at pos 0 with ZERO extra probes
        (byte-identical to the pre-fix happy path)."""
        two = _TwoPhaseReader(_gap_chains_no_gap())
        _values, _errors, _contracts, roll_info = await _resolve_happy(two)
        assert two.bulk_calls == [], (
            f"happy path issued open-probes: {[c['dates'] for c in two.bulk_calls]}"
        )
        assert roll_info["is_roll"][_HP_DATES.index(_HP_ROLL)] == 1.0


# Happy-path (no-gap) scenario: segment 2 opens on its nominal first date.
_HP_SEG1 = [date(2020, 2, 25), date(2020, 2, 26), date(2020, 2, 27)]
_HP_ROLL = date(2020, 3, 2)
_HP_SEG2 = [_HP_ROLL, date(2020, 3, 5), date(2020, 3, 20)]
_HP_DATES = _HP_SEG1 + _HP_SEG2
_HP_BY_DATE = {
    **{d: [_E1] for d in _HP_SEG1},
    **{d: [_E2] for d in _HP_SEG2},
}
_HP_E2_CLOSE = {_HP_ROLL: 93.5, date(2020, 3, 5): 300.0, date(2020, 3, 20): 694.25}


def _gap_chains_no_gap() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in _HP_SEG1:
        chains[d] = _ladder_rows(d, _E1, {3000.0: _E1_CLOSE, 2900.0: _E1_CLOSE, 2700.0: _E1_CLOSE})
    for d in _HP_SEG2:
        held = _HP_E2_CLOSE[d]
        chains[d] = _ladder_rows(d, _E2, {3000.0: held * 0.3, 2900.0: held, 2700.0: held * 2.0})
    return chains


async def _resolve_happy(reader):
    roll_info: dict = {}
    values, errors, contracts = await resolve_option_stream(
        dates=_HP_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=40),
        selection=ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream="close",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=[_E1, _E2],
        available_expirations_by_date=_HP_BY_DATE,
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
    )
    return values, errors, contracts, roll_info
