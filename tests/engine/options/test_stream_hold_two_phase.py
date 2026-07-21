"""Wave 15 — hold-leg TWO-PHASE pushdown (``query_held_rows``) in ``_resolve_bulk``.

When a HOLD leg's bulk reader supports BOTH the year-chunk multi capability AND
the Phase-2 held-symbol identity fetch, the resolver:

  * Phase 1 — fetches candidates on ROLL (segment-open) dates ONLY (delta
    pushdown for stored-delta ByDelta; full chain for ByStrike/ByMoneyness);
  * selects + freezes the held contract per segment (unchanged Python);
  * Phase 2 — ``query_held_rows`` fetches every physical row of the frozen held
    symbols over their held windows (``hi`` includes the NEXT roll date);
  * marks the per-date held-contract value + roll info.

These tests pin BYTE-IDENTITY vs the full-chain hold path, the eligibility gate,
and the sub-chunk safety net — all without a live warehouse.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Literal, Sequence

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import (
    _MAX_EXPS_PER_SUBCHUNK,
    resolve_option_stream,
)
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    RollOffset,
)

from _stream_fakes import _contract, _cycle_matches, _row


# --------------------------------------------------------------------------- #
# Fake reader implementing query_chain_bulk_multi + query_held_rows
# --------------------------------------------------------------------------- #
class _TwoPhaseReader:
    """Bulk reader with the year-chunk multi AND held-symbol identity fetch.

    ``query_held_rows`` is symbol-keyed: it returns every stored (contract, row)
    whose ``contract_id`` is a requested held symbol and whose date falls in that
    symbol's ``[lo, hi]`` window — mirroring the SQL identity keyset.
    """

    def __init__(
        self,
        chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        self._chains = chains
        self.multi_calls: list[dict] = []
        self.held_calls: list[list[tuple[str, date, date]]] = []
        self.held_cycles: list[object] = []
        self.bulk_calls: list[dict] = []

    async def query_chain_bulk_multi(
        self,
        *,
        root: str,
        type: Literal["C", "P", "both"],
        groups: Sequence[tuple[date, Sequence[date]]],
        strike_windows=None,
        expiration_cycle=None,
        delta_pushdown=None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        groups = list(groups)
        self.multi_calls.append(
            {
                "years": {exp.year for exp, _ in groups},
                "n_exp": len(groups),
                "dates": sorted({d for _e, ds in groups for d in ds}),
                "delta_pushdown": delta_pushdown,
            }
        )
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for exp, dates in groups:
            for d in dates:
                result.setdefault(d, [])
                matched = [
                    (c, r)
                    for c, r in self._chains.get(d, [])
                    if (c.type == type or type == "both")
                    and c.expiration == exp
                    and _cycle_matches(c.expiration_cycle, expiration_cycle)
                ]
                if delta_pushdown is not None:
                    target, k = delta_pushdown
                    ranked = sorted(
                        matched,
                        key=lambda cr: (
                            cr[1].delta_stored is None,
                            abs((cr[1].delta_stored or 0.0) - target),
                            cr[0].strike,
                        ),
                    )
                    matched = ranked[:k]
                result[d].extend(matched)
        return result

    async def query_held_rows(
        self,
        *,
        root: str,
        type: Literal["C", "P", "both"],
        held_windows: Sequence[tuple[str, date, date]],
        expiration_cycle=None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        # Mirror the SQL identity keyset: symbol + type + CYCLE filter (the cycle
        # predicate disambiguates the ~2.68% cross-cycle duplicate-instrument_id
        # symbols so the surviving physical rows match the full-chain path).
        self.held_calls.append(list(held_windows))
        self.held_cycles.append(expiration_cycle)
        syms = {sym: (lo, hi) for sym, lo, hi in held_windows}
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d, chain in self._chains.items():
            for c, r in chain:
                win = syms.get(c.contract_id)
                if win is None:
                    continue
                lo, hi = win
                if (
                    lo <= d <= hi
                    and (c.type == type or type == "both")
                    and _cycle_matches(c.expiration_cycle, expiration_cycle)
                ):
                    result.setdefault(d, []).append((c, r))
        return result

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
        expiration_cycle=None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        self.bulk_calls.append({"expiration_min": expiration_min, "dates": list(dates)})
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates:
            rows = [
                (c, r)
                for (c, r) in self._chains.get(d, [])
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and _cycle_matches(c.expiration_cycle, expiration_cycle)
            ]
            if rows:
                result[d] = rows
        return result


class _FullChainHoldReader(_TwoPhaseReader):
    """Same data + multi capability, but WITHOUT the held-symbol capability →
    the resolver stays on the byte-identical full-chain hold path."""

    query_held_rows = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Scenario: 3 monthly hold segments (2 true rolls) + interior drift days
# --------------------------------------------------------------------------- #
_E1 = date(2023, 1, 20)
_E2 = date(2023, 2, 17)
_E3 = date(2023, 3, 17)
_EXPS = [_E1, _E2, _E3]

_SEG1 = [date(2023, 1, 3), date(2023, 1, 6), date(2023, 1, 10)]
_SEG2 = [date(2023, 2, 1), date(2023, 2, 6), date(2023, 2, 10)]
_SEG3 = [date(2023, 3, 1), date(2023, 3, 6)]
_HOLD_DATES = _SEG1 + _SEG2 + _SEG3
_BY_DATE = {
    **{d: [_E1] for d in _SEG1},
    **{d: [_E2] for d in _SEG2},
    **{d: [_E3] for d in _SEG3},
}

# A 5-rung put ladder; -0.10 delta winner is the 4300 strike.
_LADDER = [
    (4000.0, -0.05),
    (4300.0, -0.10),
    (4600.0, -0.25),
    (4900.0, -0.50),
    (5200.0, -0.80),
]


def _hold_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    """Every trade date lists ALL three expirations' ladders (a superset — the
    resolver filters per expiration / per symbol), so a roll day carries both the
    OLD and NEW chains and each held contract is present on all its days.  ``close``
    is a distinct positive value per (strike, date) so no false-zero fallback."""
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for di, d in enumerate(_HOLD_DATES):
        rows: list[tuple[OptionContractDoc, OptionDailyRow]] = []
        for exp in _EXPS:
            for k, dlt in _LADDER:
                close = round(k / 1000.0 + 0.01 * di + 0.001 * exp.month, 4)
                rows.append(
                    (
                        _contract(strike=k, expiration=exp, type_="P"),
                        _row(row_date=d, mid=close, delta=dlt, close=close),
                    )
                )
        chains[d] = rows
    return chains


async def _resolve_hold(reader, *, selection):
    roll_info: dict = {}
    values, errors, contracts = await resolve_option_stream(
        dates=_HOLD_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=20),
        selection=selection,
        stream="close",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=_EXPS,
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
        available_expirations_by_date=_BY_DATE,
    )
    return values, errors, contracts, roll_info


def _arr_eq(a, b) -> bool:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return a.shape == b.shape and bool(np.all((a == b) | (np.isnan(a) & np.isnan(b))))


_BYDELTA = ByDelta(target_delta=-0.10, tolerance=0.05, strict=False)
_BYSTRIKE = ByStrike(strike=4300.0)


class TestTwoPhaseHoldByteIdentity:
    async def test_bydelta_values_and_roll_info_identical(self):
        full = _FullChainHoldReader(_hold_chains())
        two = _TwoPhaseReader(_hold_chains())
        v_full, e_full, c_full, ri_full = await _resolve_hold(full, selection=_BYDELTA)
        v_two, e_two, c_two, ri_two = await _resolve_hold(two, selection=_BYDELTA)

        # Sanity: the two paths really differed.  The full-chain hold path uses
        # the year-chunk multi over ALL dates with NO pushdown and never fetches
        # held symbols; the two-phase path pushes down + fetches held symbols.
        assert full.held_calls == []
        assert full.multi_calls and all(
            c["delta_pushdown"] is None for c in full.multi_calls
        )
        full_dates = {d for c in full.multi_calls for d in c["dates"]}
        assert len(full_dates) == len(_HOLD_DATES)  # every held/drift day fetched
        assert two.held_calls and two.multi_calls
        two_dates = {d for c in two.multi_calls for d in c["dates"]}
        assert two_dates == {_SEG1[0], _SEG2[0], _SEG3[0]}  # roll dates only

        assert _arr_eq(v_full, v_two), "hold values diverged"
        assert e_full == e_two
        assert [None if c is None else c.contract_id for c in c_full] == [
            None if c is None else c.contract_id for c in c_two
        ]
        assert set(ri_full) == set(ri_two)
        for key in ri_full:
            assert _arr_eq(ri_full[key], ri_two[key]), f"roll_info[{key}] diverged"

    async def test_bystrike_values_and_roll_info_identical(self):
        full = _FullChainHoldReader(_hold_chains())
        two = _TwoPhaseReader(_hold_chains())
        v_full, e_full, _c, ri_full = await _resolve_hold(full, selection=_BYSTRIKE)
        v_two, e_two, _c2, ri_two = await _resolve_hold(two, selection=_BYSTRIKE)
        assert _arr_eq(v_full, v_two)
        assert e_full == e_two
        for key in ri_full:
            assert _arr_eq(ri_full[key], ri_two[key])

    async def test_nonnan_values_and_two_true_rolls(self):
        two = _TwoPhaseReader(_hold_chains())
        values, errors, _c, roll_info = await _resolve_hold(two, selection=_BYDELTA)
        assert all(not np.isnan(v) for v in values)
        assert all(e is None for e in errors)
        # is_roll marks each segment open: seg1 open + 2 true rolls = 3 markers.
        assert int(roll_info["is_roll"].sum()) == 3


class TestTwoPhaseEligibilityGate:
    async def test_hold_bydelta_engages_pushdown_on_roll_dates_only(self):
        two = _TwoPhaseReader(_hold_chains())
        await _resolve_hold(two, selection=_BYDELTA)
        # Phase 1 pushdown engaged.
        assert two.multi_calls
        assert all(c["delta_pushdown"] is not None for c in two.multi_calls)
        # Phase 1 fetched ONLY the 3 roll (segment-open) dates.
        fetched = sorted({d for c in two.multi_calls for d in c["dates"]})
        assert fetched == sorted([_SEG1[0], _SEG2[0], _SEG3[0]])
        # Phase 2 held-symbol fetch happened, with windows covering the roll seam.
        assert len(two.held_calls) == 1
        syms = {s for s, _lo, _hi in two.held_calls[0]}
        assert len(syms) == 3  # one held contract per segment

    async def test_hold_bystrike_full_chain_phase1_plus_held_fetch(self):
        two = _TwoPhaseReader(_hold_chains())
        await _resolve_hold(two, selection=_BYSTRIKE)
        # ByStrike Phase 1 is full chain (no delta pushdown) but still roll-dates
        # only, and Phase 2 still fetches held symbols by identity.
        assert all(c["delta_pushdown"] is None for c in two.multi_calls)
        assert len(two.held_calls) == 1

    async def test_held_window_includes_next_roll_date(self):
        two = _TwoPhaseReader(_hold_chains())
        await _resolve_hold(two, selection=_BYDELTA)
        windows = {s: (lo, hi) for s, lo, hi in two.held_calls[0]}
        # The first two segments' windows must extend to the NEXT segment's open.
        his = sorted(hi for _s, (lo, hi) in windows.items())
        assert _SEG2[0] in his  # seg1 window reaches into seg2's open (roll seam)
        assert _SEG3[0] in his  # seg2 window reaches into seg3's open


class TestSubChunkSafetyNet:
    """Non-hold full-chain (ByStrike) year with >24 expirations sub-chunks."""

    def _many_exp_scenario(self, n_exp: int):
        # ``n_exp`` weekly expirations in 2024, each with ONE trade date ~7d prior.
        exps = [date(2024, 1, 5) + timedelta(days=7 * i) for i in range(n_exp)]
        dates = [e - timedelta(days=4) for e in exps]
        by_date = {d: [e] for e, d in zip(exps, dates)}
        chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for e, d in zip(exps, dates):
            chains[d] = [
                (
                    _contract(strike=k, expiration=e, type_="P"),
                    _row(row_date=d, mid=k / 1000.0, delta=dlt, close=k / 1000.0),
                )
                for k, dlt in _LADDER
            ]
        return exps, dates, by_date, chains

    async def _resolve_strike(self, reader, *, dates, exps, by_date):
        return await resolve_option_stream(
            dates=dates,
            collection="OPT_SP_500",
            option_type="P",
            cycle=None,
            maturity=NearestToTarget(target_dte_days=7),
            selection=_BYSTRIKE,
            stream="close",
            roll_offset=RollOffset(),
            chain_reader=reader,
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=reader,
            available_expirations=exps,
            available_expirations_by_date=by_date,
        )

    async def test_dense_year_splits_into_subchunks_of_cap(self):
        exps, dates, by_date, chains = self._many_exp_scenario(47)
        reader = _TwoPhaseReader(chains)
        await self._resolve_strike(reader, dates=dates, exps=exps, by_date=by_date)
        # 47 > 24 → 2 sub-chunks, each within the cap.
        assert len(reader.multi_calls) == 2
        assert all(c["n_exp"] <= _MAX_EXPS_PER_SUBCHUNK for c in reader.multi_calls)
        assert sum(c["n_exp"] for c in reader.multi_calls) == 47

    async def test_sparse_year_single_chunk(self):
        exps, dates, by_date, chains = self._many_exp_scenario(12)
        reader = _TwoPhaseReader(chains)
        await self._resolve_strike(reader, dates=dates, exps=exps, by_date=by_date)
        assert len(reader.multi_calls) == 1
        assert reader.multi_calls[0]["n_exp"] == 12

    async def test_subchunk_values_byte_identical_to_full_chain(self):
        exps, dates, by_date, chains = self._many_exp_scenario(47)
        split = _TwoPhaseReader(chains)
        whole = _TwoPhaseReader(chains)
        # Force ``whole`` to NOT sub-chunk by lifting the cap via a monthly-count
        # scenario is awkward; instead compare split output to the per-expiration
        # path (a reader without multi), which is the byte-identity anchor.
        v_split, e_split, c_split = await self._resolve_strike(
            split, dates=dates, exps=exps, by_date=by_date
        )
        no_multi = _FullChainNoMulti(chains)
        v_slow, e_slow, c_slow = await self._resolve_strike(
            no_multi, dates=dates, exps=exps, by_date=by_date
        )
        assert _arr_eq(v_split, v_slow)
        assert e_split == e_slow
        assert [None if c is None else c.contract_id for c in c_split] == [
            None if c is None else c.contract_id for c in c_slow
        ]


class _FullChainNoMulti(_TwoPhaseReader):
    """No multi capability at all → per-expiration ``query_chain_bulk`` path."""

    query_chain_bulk_multi = None  # type: ignore[assignment]
    query_held_rows = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Regression: cross-cycle DUPLICATE-instrument_id held symbol (the ~2.68% quirk)
# --------------------------------------------------------------------------- #
# The dwh tags the SAME physical option symbol under TWO ``expiration_cycle``
# values in overlapping eras (e.g. the 3rd-Friday contract is both ``"M"`` and
# ``"W3 Friday"``).  Both siblings share ONE ``contract_id`` (== symbol) but carry
# DIFFERENT quotes.  The full-chain path filters the chain by the resolver's
# (expanded) cycle, so a weekly leg only ever sees the ``"W3 Friday"`` sibling.
# Phase-2 ``query_held_rows`` MUST apply the SAME cycle filter, or it re-admits the
# ``"M"`` sibling and ``_row_for_contract`` (first-by-instrument_id) surfaces the
# WRONG physical row (the live 4970_P/2024-03-06 blocker: 7.40 vs 4.15).
_XC_EXP = date(2024, 3, 15)
_XC_DATES = [date(2024, 3, 5), date(2024, 3, 6), date(2024, 3, 7)]
_XC_CID = "OPT_FUT_SP_500_EMINI_20240315_4500_P"
# Per-date (M-sibling close, W3-sibling close): the M sibling is the one the
# weekly cycle filter must EXCLUDE; distinct values so a wrong pick is visible.
_XC_QUOTES = {
    _XC_DATES[0]: (7.40, 4.15),
    _XC_DATES[1]: (3.25, 2.35),
    _XC_DATES[2]: (3.90, 2.70),
}


def _xc_contract(cycle: str) -> OptionContractDoc:
    # Both siblings share ONE contract_id but differ in expiration_cycle — the
    # real dup shape (``_contract`` bakes cycle into the id, so override it).
    c = _contract(strike=4500.0, expiration=_XC_EXP, type_="P", cycle=cycle)
    return replace(c, contract_id=_XC_CID)


def _xc_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in _XC_DATES:
        m_close, w_close = _XC_QUOTES[d]
        chains[d] = [
            # M sibling FIRST so a cycle-blind first-by-iid pick lands on it.
            (
                _xc_contract("M"),
                _row(row_date=d, mid=m_close, delta=-0.10, close=m_close),
            ),
            (
                _xc_contract("W3 Friday"),
                _row(row_date=d, mid=w_close, delta=-0.10, close=w_close),
            ),
        ]
    return chains


async def _resolve_xc(reader, *, cycle):
    roll_info: dict = {}
    values, errors, contracts = await resolve_option_stream(
        dates=_XC_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=cycle,
        maturity=NearestToTarget(target_dte_days=9),
        selection=_BYSTRIKE_4500,
        stream="close",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=[_XC_EXP],
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
        available_expirations_by_date={d: [_XC_EXP] for d in _XC_DATES},
    )
    return values, errors, contracts, roll_info


_BYSTRIKE_4500 = ByStrike(strike=4500.0)
# The generic-weekly cycle expands to this tuple at the wiring layer; the resolver
# receives the expanded value.  ``"M"`` is deliberately absent → excluded.
_WEEKLY_CYCLE = ("W", "W1 Friday", "W2 Friday", "W3 Friday", "W4 Friday")


class TestTwoPhaseCrossCycleDupRow:
    """A held symbol with an ``M`` and a ``W3 Friday`` sibling under a WEEKLY leg:
    Phase-2 must mark the SAME physical row the full-chain path marks."""

    async def test_held_row_matches_full_chain_under_weekly_filter(self):
        full = _FullChainHoldReader(_xc_chains())
        two = _TwoPhaseReader(_xc_chains())
        v_full, _e, _c, _ri = await _resolve_xc(full, cycle=_WEEKLY_CYCLE)
        v_two, _e2, _c2, _ri2 = await _resolve_xc(two, cycle=_WEEKLY_CYCLE)

        # The full-chain path only ever sees the W3 sibling → marks its closes.
        assert list(v_full) == [4.15, 2.35, 2.70]
        # Two-phase MUST match it (pre-fix it re-admitted the M sibling → 7.40...).
        assert _arr_eq(v_full, v_two), (
            f"two-phase marked the wrong dup sibling: {list(v_two)} != {list(v_full)}"
        )
        # Structural: Phase-2 was told the same cycle the full-chain path filters on.
        assert two.held_cycles and all(c == _WEEKLY_CYCLE for c in two.held_cycles)

    async def test_no_cycle_still_byte_identical(self):
        # cycle=None (all cycles): both paths keep BOTH siblings, same iid order →
        # first-by-iid pick identical.  Guards against the fix over-filtering.
        full = _FullChainHoldReader(_xc_chains())
        two = _TwoPhaseReader(_xc_chains())
        v_full, _e, _c, _ri = await _resolve_xc(full, cycle=None)
        v_two, _e2, _c2, _ri2 = await _resolve_xc(two, cycle=None)
        assert _arr_eq(v_full, v_two)


# --------------------------------------------------------------------------- #
# M3: ByMoneyness + bs_mid hold two-phase byte-identity (in the gate, untested
#     at the value level before this).
# --------------------------------------------------------------------------- #
async def _resolve_hold_ex(reader, *, selection, stream, underlying):
    roll_info: dict = {}
    values, errors, contracts = await resolve_option_stream(
        dates=_HOLD_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=20),
        selection=selection,
        stream=stream,
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=underlying,
        bulk_chain_reader=reader,
        available_expirations=_EXPS,
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
        available_expirations_by_date=_BY_DATE,
    )
    return values, errors, contracts, roll_info


def _const_underlying(price: float):
    async def _resolver(contract, d):
        return price

    return _resolver


class TestTwoPhaseHoldByteIdentityByMoneynessAndBsMid:
    async def test_bymoneyness_close_identical(self):
        # S=4300 → K/S=1.0 selects the 4300 rung; generous tolerance.
        sel = ByMoneyness(target_K_over_S=1.0, tolerance=0.30)
        und = _const_underlying(4300.0)
        full = _FullChainHoldReader(_hold_chains())
        two = _TwoPhaseReader(_hold_chains())
        v_full, e_full, _c, ri_full = await _resolve_hold_ex(
            full, selection=sel, stream="close", underlying=und
        )
        v_two, e_two, _c2, ri_two = await _resolve_hold_ex(
            two, selection=sel, stream="close", underlying=und
        )
        assert two.held_calls  # two-phase path actually engaged
        assert _arr_eq(v_full, v_two)
        assert e_full == e_two
        for key in ri_full:
            assert _arr_eq(ri_full[key], ri_two[key]), f"roll_info[{key}] diverged"

    async def test_bsmid_stream_identical(self):
        # bs_mid is COMPUTED (Black-76 from the row IV + the underlying future);
        # both paths must feed the kernel the SAME row → identical prices.
        und = _const_underlying(4500.0)
        full = _FullChainHoldReader(_hold_chains())
        two = _TwoPhaseReader(_hold_chains())
        v_full, e_full, _c, ri_full = await _resolve_hold_ex(
            full, selection=_BYDELTA, stream="bs_mid", underlying=und
        )
        v_two, e_two, _c2, ri_two = await _resolve_hold_ex(
            two, selection=_BYDELTA, stream="bs_mid", underlying=und
        )
        assert two.held_calls
        # Non-trivial: at least one finite computed price (guards against an
        # all-NaN degenerate "identity").
        assert np.any(np.isfinite(np.asarray(v_full, dtype=np.float64)))
        assert _arr_eq(v_full, v_two)
        assert e_full == e_two
        for key in ri_full:
            assert _arr_eq(ri_full[key], ri_two[key]), f"roll_info[{key}] diverged"
