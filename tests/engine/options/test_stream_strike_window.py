"""Strike-window narrowing must track spot PER EXPIRATION GROUP.

Regression for the ByDelta selection collapse: the Phase-B strike window used to
be computed ONCE from the FIRST trade date's spot, so on a multi-decade resolve
with large spot drift (SPX ~1250 in 2005 → ~5000+ in 2024) every later date's
bulk fetch was capped at the first date's band.  The true ~10Δ strike then fell
outside the window and ``match_by_delta`` (strict=False) returned the deepest-OTM
admitted strike (the window top, |delta|≈0) instead of the real ~10Δ contract —
which wiped a short-put P&L (bs_mid) and flat-lined it (mid).

These tests are dwh-free: synthetic chains + a strike-HONOURING bulk reader (the
shared ``FakeBulkChainReader`` deliberately ignores the strike window, so it can't
exercise this) + a fake per-date underlying resolver.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByDelta,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeChainReader, _contract, _row

# ── Scenario: ONE resolve spanning two far-apart eras ──────────────────────
# Early era (spot 1250) and late era (spot 5000).  Grid of put strikes; stored
# delta rises in magnitude with strike so the ~10Δ strike is 4500 in the late era
# (delta exactly -0.10) and the deepest-OTM strikes have |delta|→0.  A first-date
# (2006) global window is ~[500, 2000], which EXCLUDES the late-era 4500.
_EXP_EARLY = date(2006, 2, 17)
_EXP_LATE = date(2024, 9, 20)
_DATE_EARLY = date(2006, 1, 17)
_DATE_LATE = date(2024, 8, 16)
_STRIKES = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
_SPOT = {_DATE_EARLY: 1250.0, _DATE_LATE: 5000.0}
_TARGET_DELTA = -0.10


def _delta(strike: float, spot: float) -> float:
    """A monotone put delta: -0.10 exactly at K=0.9*spot, |delta| shrinking to ~0
    for deep-OTM (low) strikes and growing past 0.10 above 0.9*spot."""
    return -min(0.999, 0.10 * strike / (0.9 * spot))


def _chain_for(d: date, exp: date) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
    spot = _SPOT[d]
    return [
        (
            _contract(strike=float(k), expiration=exp, type_="P"),
            _row(row_date=d, mid=5.0, iv=0.20, delta=_delta(float(k), spot)),
        )
        for k in _STRIKES
    ]


def _build_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    return {
        _DATE_EARLY: _chain_for(_DATE_EARLY, _EXP_EARLY),
        _DATE_LATE: _chain_for(_DATE_LATE, _EXP_LATE),
    }


class StrikeFilteringBulkReader:
    """Bulk reader that HONOURS strike_min/strike_max (records every call).

    The shared ``FakeBulkChainReader`` ignores the strike window, so it cannot
    exercise the window bug; this one applies it, exactly as the real dwh reader
    would, so the selected contract depends on the window the resolver passes.
    """

    def __init__(self, chains_by_date):
        self._chains = chains_by_date
        self.bulk_calls: list[dict] = []

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
    ):
        self.bulk_calls.append(
            {
                "expiration_min": expiration_min,
                "strike_min": strike_min,
                "strike_max": strike_max,
                "dates": list(dates),
            }
        )
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates:
            filtered = [
                (c, r)
                for (c, r) in self._chains.get(d, [])
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and (strike_min is None or c.strike >= strike_min)
                and (strike_max is None or c.strike <= strike_max)
            ]
            if filtered:
                result[d] = filtered
        return result


def _make_underlying_resolver(spot_map):
    async def resolver(contract, d):
        return spot_map.get(d)

    return resolver


async def _run(spot_map=None):
    chains = _build_chains()
    bulk = StrikeFilteringBulkReader(chains)
    values, errors, contracts = await resolve_option_stream(
        dates=[_DATE_EARLY, _DATE_LATE],
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByDelta(target_delta=_TARGET_DELTA, tolerance=0.05, strict=False),
        stream="iv",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(spot_map or _SPOT),
        bulk_chain_reader=bulk,
        available_expirations=[_EXP_EARLY, _EXP_LATE],
    )
    return values, errors, contracts, bulk


def _late(contracts, bulk):
    """(late-date contract, late-group bulk-call) — index 1 is the late date."""
    late_contract = contracts[1]
    late_call = next(c for c in bulk.bulk_calls if c["expiration_min"] == _EXP_LATE)
    return late_contract, late_call


async def test_late_date_selects_10delta_not_window_top():
    """FAILS pre-fix (global first-date window ~[500,2000] excludes 4500 → picks
    the window-top ~2000); PASSES post-fix (per-group window includes 4500)."""
    _values, errors, contracts, bulk = await _run()
    assert errors[1] is None
    late_contract, _ = _late(contracts, bulk)
    assert late_contract is not None
    assert late_contract.strike == pytest.approx(4500.0), (
        f"late date should select the ~10Δ strike 4500, got {late_contract.strike} "
        f"(window-top collapse = the selection bug)"
    )


async def test_per_group_window_tracks_spot():
    """The late group's strike window must track the LATE spot (5000), not the
    first date's spot (1250) — and, for a PUT, be OPTION-TYPE-AWARE: it spans the
    put wing ``[0.40, 1.30]·spot`` (deep-OTM well below spot through deep-ITM just
    above it), not a band symmetric around spot."""
    _values, _errors, _contracts, bulk = await _run()
    late_call = next(c for c in bulk.bulk_calls if c["expiration_min"] == _EXP_LATE)
    assert late_call["strike_min"] is not None and late_call["strike_max"] is not None
    # PUT window = [0.40, 1.30]·spot (spot=5000) → [2000, 6500]; admits 4500.
    assert late_call["strike_min"] == pytest.approx(5000.0 * 0.40)
    assert late_call["strike_max"] == pytest.approx(5000.0 * 1.30)
    assert late_call["strike_min"] <= 4500.0 <= late_call["strike_max"]
    # Low bound reaches well below spot (covers the deep-OTM put wing).
    assert late_call["strike_min"] < 5000.0 * 0.5


async def test_put_window_covers_deep_crash_strike():
    """An extreme-vol 10Δ PUT strike ≈0.65·spot must be INSIDE the option-type-aware
    PUT window ``[0.40, 1.30]·spot`` and be selected.  A symmetric ±30% band
    (``[0.70, 1.30]·spot``) would CLIP it (0.65 < 0.70) → the engine would hold a
    less-OTM strike and under-capture the crash (the residual 2008-10 gap)."""
    d = date(2008, 10, 15)
    exp = date(2008, 11, 21)
    spot = 1000.0

    def _dcrash(k: float) -> float:
        # 10Δ put at K=650 (=0.65·spot); |delta| grows with strike (toward spot).
        if k <= 650.0:
            return -0.10 * k / 650.0
        return -(0.10 + 0.5 * (k - 650.0) / 650.0)

    strikes = [400, 500, 600, 650, 700, 800, 900, 1000, 1100]
    chain = [
        (
            _contract(strike=float(k), expiration=exp, type_="P"),
            _row(row_date=d, mid=5.0, iv=0.90, delta=_dcrash(float(k))),
        )
        for k in strikes
    ]
    chains = {d: chain}
    bulk = StrikeFilteringBulkReader(chains)
    _values, errors, contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByDelta(target_delta=_TARGET_DELTA, tolerance=0.05, strict=False),
        stream="iv",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver({d: spot}),
        bulk_chain_reader=bulk,
        available_expirations=[exp],
    )
    call = bulk.bulk_calls[0]
    assert call["strike_min"] == pytest.approx(spot * 0.40)
    assert call["strike_max"] == pytest.approx(spot * 1.30)
    # The deep 10Δ strike 650 (=0.65·spot) is INSIDE the put window and IS selected
    # (a symmetric [700,1300] band would exclude it → pick the less-OTM 700).
    assert call["strike_min"] <= 650.0 <= call["strike_max"]
    assert errors[0] is None
    assert contracts[0] is not None
    assert contracts[0].strike == pytest.approx(650.0)


async def test_put_window_covers_deep_itm_target():
    """A deep-ITM target (−0.90 put, strike ≈1.15·spot) must be INSIDE the PUT
    window ``[0.40, 1.30]·spot`` and be selected — the ITM wing.  The narrower
    ``[0.40, 1.10]`` band would CLIP it (1.15 > 1.10 → pick the less-ITM 1.10)."""
    d = date(2024, 6, 21)
    exp = date(2024, 7, 19)
    spot = 1000.0
    # put delta → -1 as strike rises above spot; -0.90 at K=1150 (=1.15·spot).
    delta_by_k = {
        900: -0.50,
        1000: -0.70,
        1100: -0.85,
        1150: -0.90,
        1200: -0.93,
        1300: -0.97,
    }
    chain = [
        (
            _contract(strike=float(k), expiration=exp, type_="P"),
            _row(row_date=d, mid=5.0, iv=0.20, delta=dv),
        )
        for k, dv in delta_by_k.items()
    ]
    chains = {d: chain}
    bulk = StrikeFilteringBulkReader(chains)
    _values, errors, contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByDelta(target_delta=-0.90, tolerance=0.05, strict=False),
        stream="iv",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver({d: spot}),
        bulk_chain_reader=bulk,
        available_expirations=[exp],
    )
    call = bulk.bulk_calls[0]
    assert call["strike_max"] == pytest.approx(spot * 1.30)
    assert call["strike_min"] <= 1150.0 <= call["strike_max"]  # ITM strike admitted
    assert errors[0] is None
    assert contracts[0] is not None and contracts[0].strike == pytest.approx(1150.0)


async def test_call_window_covers_deep_itm_target():
    """A deep-ITM CALL target (+0.90 call, strike ≈0.88·spot) must be INSIDE the
    CALL window ``[0.70, 1.60]·spot`` and be selected.  The narrower ``[0.90, 1.60]``
    band would CLIP it (0.88 < 0.90 → pick the less-ITM 0.95)."""
    d = date(2024, 6, 21)
    exp = date(2024, 7, 19)
    spot = 1000.0
    # call delta → 1 as strike falls below spot; +0.90 at K=880 (=0.88·spot).
    delta_by_k = {
        700: 0.97,
        800: 0.93,
        880: 0.90,
        950: 0.70,
        1050: 0.50,
        1200: 0.20,
    }
    chain = [
        (
            _contract(strike=float(k), expiration=exp, type_="C"),
            _row(row_date=d, mid=5.0, iv=0.20, delta=dv),
        )
        for k, dv in delta_by_k.items()
    ]
    chains = {d: chain}
    bulk = StrikeFilteringBulkReader(chains)
    _values, errors, contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByDelta(target_delta=0.90, tolerance=0.05, strict=False),
        stream="iv",
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver({d: spot}),
        bulk_chain_reader=bulk,
        available_expirations=[exp],
    )
    call = bulk.bulk_calls[0]
    assert call["strike_min"] == pytest.approx(spot * 0.70)
    assert call["strike_max"] == pytest.approx(spot * 1.60)
    assert call["strike_min"] <= 880.0 <= call["strike_max"]  # ITM call strike admitted
    assert errors[0] is None
    assert contracts[0] is not None and contracts[0].strike == pytest.approx(880.0)


async def test_spot_none_falls_back_to_full_chain():
    """When a group's spot cannot be resolved (None), pass NO strike bounds
    (full chain) — never a None/degenerate-bounded window that silently caps."""
    spot_map = {_DATE_EARLY: 1250.0, _DATE_LATE: None}
    _values, errors, contracts, bulk = await _run(spot_map=spot_map)
    late_call = next(c for c in bulk.bulk_calls if c["expiration_min"] == _EXP_LATE)
    assert late_call["strike_min"] is None and late_call["strike_max"] is None
    # Full chain → the correct ~10Δ strike is still selected.
    assert errors[1] is None
    assert contracts[1] is not None
    assert contracts[1].strike == pytest.approx(4500.0)
