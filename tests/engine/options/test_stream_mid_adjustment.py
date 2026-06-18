"""Tests for MID roll back-adjustment in
``tcg.engine.options.series.stream_resolver``.

Two layers:

* Direct unit tests on the pure helper ``_back_adjust_mid`` (and ``_seam_gap``)
  — synthetic ``values`` / ``contracts`` / ``mid_at`` with no I/O.  These pin
  the cascade math, the same-day-vs-adjacent gap preference, NaN preservation,
  and the zero/NaN seam-skip guard.

* End-to-end tests through ``resolve_option_stream`` with a faked bulk chain
  reader (mirroring ``test_stream_resolver.py``), exercising a real two-segment
  rolled mid series across a ``NextThirdFriday`` roll boundary.

Convention mirrored from ``tcg/data/_rolling/adjustment.py``: backward cascade
from the most-recent (unadjusted) segment; ratio multiplies pre-seam mids by
``new/old``; difference adds ``new-old``; a 0/NaN reference mid leaves the gap
unadjusted; non-price streams ignore ``adjustment`` entirely.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import (
    _back_adjust_mid,
    _seam_gap,
    resolve_option_stream,
)
from tcg.types.options import (
    ByDelta,
    ByStrike,
    NextThirdFriday,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row


# Two contracts standing in for a roll: April (old) → May (new).
_APR = date(2024, 4, 19)
_MAY = date(2024, 5, 17)
_OLD = _contract(strike=4500, expiration=_APR)
_NEW = _contract(strike=4500, expiration=_MAY)


def _two_segment_contracts() -> list[OptionContractDoc | None]:
    """OLD, OLD, NEW, NEW — one seam at index 2."""
    return [_OLD, _OLD, _NEW, _NEW]


# ── Pure-helper unit tests ──────────────────────────────────────────────


def test_helper_ratio_seam_continuous_recent_unchanged():
    """Ratio back-adjust: adjacent-mark gap makes the seam continuous and
    leaves the most-recent segment byte-for-byte unchanged."""
    values = np.array([10.0, 10.0, 13.0, 14.0])
    contracts = _two_segment_contracts()
    # Empty mid_at → forces the adjacent-observed-marks fallback (candidate 3):
    # old = values[1] = 10, new = values[2] = 13, factor = 1.3.
    out = _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=lambda cid, i: None,
        mode="ratio",
    )
    # Pre-seam (idx 0,1) multiplied by 1.3; most-recent (idx 2,3) untouched.
    assert out[0] == pytest.approx(13.0)
    assert out[1] == pytest.approx(13.0)
    assert out[2] == pytest.approx(13.0)  # unchanged anchor
    assert out[3] == pytest.approx(14.0)  # unchanged
    # Seam continuity: adjusted last-old == first-new.
    assert out[1] == pytest.approx(out[2])
    # Proportional relationship preserved within the old segment.
    assert out[0] / out[1] == pytest.approx(values[0] / values[1])
    # Input not mutated.
    assert values[0] == 10.0


def test_helper_difference_seam_continuous_recent_unchanged():
    """Difference back-adjust: additive gap; seam continuous; recent segment
    unchanged; within-segment spacing preserved."""
    values = np.array([10.0, 11.0, 13.0, 14.0])
    contracts = _two_segment_contracts()
    # old = values[1] = 11, new = values[2] = 13, offset = +2.
    out = _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=lambda cid, i: None,
        mode="difference",
    )
    assert out[0] == pytest.approx(12.0)
    assert out[1] == pytest.approx(13.0)
    assert out[2] == pytest.approx(13.0)
    assert out[3] == pytest.approx(14.0)
    assert out[1] == pytest.approx(out[2])  # continuous
    # Additive spacing within the old segment preserved.
    assert out[1] - out[0] == pytest.approx(values[1] - values[0])
    assert values[1] == 11.0  # input not mutated


def test_helper_prefers_same_day_lookup_over_adjacent_marks():
    """When the chain DOES hold the off-contract on the seam day, the same-day
    gap (candidate 1) is used in preference to the adjacent-day marks."""
    values = np.array([10.0, 10.0, 13.0, 13.0])
    contracts = _two_segment_contracts()

    # mid_at returns OLD's mid on the seam day (idx 2) = 12.0 (NOT 10.0), so
    # the same-day gap is new/old = 13/12, distinct from the adjacent 13/10.
    def mid_at(cid: str, i: int) -> float | None:
        if cid == _OLD.contract_id and i == 2:
            return 12.0
        return None

    out = _back_adjust_mid(
        values=values, contracts=contracts, mid_at=mid_at, mode="ratio"
    )
    # Same-day factor 13/12 applied to pre-seam, NOT 13/10.
    assert out[0] == pytest.approx(10.0 * (13.0 / 12.0))
    assert out[1] == pytest.approx(10.0 * (13.0 / 12.0))
    assert out[2] == pytest.approx(13.0)  # unchanged


def test_helper_preserves_nan_in_old_segment():
    """A NaN mid earlier in the series stays NaN after adjustment (no
    fabrication); finite neighbours are still adjusted."""
    values = np.array([np.nan, 10.0, 13.0, 13.0])
    contracts = _two_segment_contracts()
    out = _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=lambda cid, i: None,
        mode="ratio",
    )
    assert np.isnan(out[0])  # NaN preserved
    assert out[1] == pytest.approx(13.0)  # 10 * 1.3
    assert out[2] == pytest.approx(13.0)


def test_helper_skips_seam_on_nan_reference_marks():
    """If both reference marks at a seam are unusable (NaN here), the seam is
    skipped — the gap is left unadjusted, no crash, no NaN poisoning of the
    rest of history."""
    # OLD's last mark (idx 1) is NaN and no same-day lookup → all candidates
    # fail → seam skipped.  Earlier finite value (idx 0) stays raw.
    values = np.array([10.0, np.nan, 13.0, 13.0])
    contracts = _two_segment_contracts()
    out = _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=lambda cid, i: None,
        mode="ratio",
    )
    assert out[0] == pytest.approx(10.0)  # unadjusted (seam skipped)
    assert np.isnan(out[1])
    assert out[2] == pytest.approx(13.0)
    assert out[3] == pytest.approx(13.0)


def test_helper_skips_seam_on_zero_reference_mark():
    """A zero reference mid is treated like NaN (can't form a meaningful ratio
    or a trustworthy difference) — seam skipped, symmetric for both modes."""
    contracts = _two_segment_contracts()
    for mode in ("ratio", "difference"):
        values = np.array([10.0, 0.0, 13.0, 13.0])
        out = _back_adjust_mid(
            values=values,
            contracts=contracts,
            mid_at=lambda cid, i: None,
            mode=mode,  # type: ignore[arg-type]
        )
        assert out[0] == pytest.approx(10.0), mode  # unadjusted
        assert out[1] == pytest.approx(0.0), mode
        assert out[2] == pytest.approx(13.0), mode


def test_helper_no_seam_returns_copy_unchanged():
    """No contract transition → series returned unchanged (a copy)."""
    values = np.array([10.0, 11.0, 12.0])
    contracts: list[OptionContractDoc | None] = [_OLD, _OLD, _OLD]
    out = _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=lambda cid, i: None,
        mode="ratio",
    )
    assert np.array_equal(out, values)
    assert out is not values  # a copy, not the same object


def test_helper_multi_seam_backward_cascade():
    """Two seams: factors cascade backward so the earliest segment carries the
    product of all later gaps; each segment is continuous with the next."""
    # segA (idx 0,1) OLD, segB (idx 2,3) MID, segC (idx 4) NEW.
    c_a = _OLD
    c_b = _NEW
    c_c = _contract(strike=4500, expiration=date(2024, 6, 21))
    contracts: list[OptionContractDoc | None] = [c_a, c_a, c_b, c_b, c_c]
    # Raw marks; seams at idx 2 (A→B) and idx 4 (B→C).
    values = np.array([10.0, 10.0, 20.0, 20.0, 40.0])
    out = _back_adjust_mid(
        values=values,
        contracts=contracts,
        mid_at=lambda cid, i: None,
        mode="ratio",
    )
    # Seam idx 4: factor = values[4]/values[3] = 40/20 = 2 → applied to idx<4.
    # Seam idx 2: factor = values[2]/values[1] = 20/10 = 2 → applied to idx<2.
    # idx 2,3 see only the idx-4 factor (×2): 40, 40.
    # idx 0,1 see both (×2 ×2 = ×4): 40, 40.
    assert out[4] == pytest.approx(40.0)  # most recent, unchanged
    assert out[2] == pytest.approx(40.0)
    assert out[3] == pytest.approx(40.0)
    assert out[0] == pytest.approx(40.0)
    assert out[1] == pytest.approx(40.0)


def test_seam_gap_prefers_same_day_then_adjacent():
    """``_seam_gap`` returns the same-day pair when available, else the
    adjacent observed marks."""
    values = np.array([10.0, 10.0, 13.0, 13.0])
    # Same-day available (OLD on seam day): old=12, new=values[2]=13.
    gap = _seam_gap(
        seam_idx=2,
        old_cid=_OLD.contract_id,
        new_cid=_NEW.contract_id,
        values=values,
        mid_at=lambda cid, i: 12.0 if (cid == _OLD.contract_id and i == 2) else None,
    )
    assert gap == (12.0, 13.0)
    # No same-day → adjacent marks old=values[1]=10, new=values[2]=13.
    gap2 = _seam_gap(
        seam_idx=2,
        old_cid=_OLD.contract_id,
        new_cid=_NEW.contract_id,
        values=values,
        mid_at=lambda cid, i: None,
    )
    assert gap2 == (10.0, 13.0)


# ── End-to-end through resolve_option_stream (bulk path) ────────────────


def _rolled_mid_chains(
    *,
    old_mid: float,
    new_mid_first: float,
    new_mid_second: float,
) -> tuple[list[date], dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]]:
    """Build a 4-date scenario rolling APR→MAY at 2024-04-19.

    Dates: 04-17, 04-18 select the APR contract; 04-19, 04-22 select MAY
    (verified via DefaultMaturityResolver in the test below).  Each date's
    chain contains ONLY its selected expiration (mirrors the production bulk
    path, which fetches one expiration per date).
    """
    d0, d1, d2, d3 = (
        date(2024, 4, 17),
        date(2024, 4, 18),
        date(2024, 4, 19),
        date(2024, 4, 22),
    )
    chains = {
        d0: [(_OLD, _row(row_date=d0, mid=old_mid))],
        d1: [(_OLD, _row(row_date=d1, mid=old_mid))],
        d2: [(_NEW, _row(row_date=d2, mid=new_mid_first))],
        d3: [(_NEW, _row(row_date=d3, mid=new_mid_second))],
    }
    return [d0, d1, d2, d3], chains


async def _resolve(dates, chains, *, adjustment, stream="mid"):
    reader = FakeChainReader(chains)
    bulk_reader = FakeBulkChainReader(chains)
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream=stream,
        adjustment=adjustment,
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )


async def test_e2e_none_is_raw_identity():
    """adjustment='none' returns the raw rolled mid series unchanged."""
    dates, chains = _rolled_mid_chains(
        old_mid=10.0, new_mid_first=13.0, new_mid_second=14.0
    )
    values, errors, contracts = await _resolve(dates, chains, adjustment="none")
    assert all(e is None for e in errors)
    np.testing.assert_allclose(values, [10.0, 10.0, 13.0, 14.0])
    # Sanity: a real roll seam exists (contract_id changes at idx 2).
    assert contracts[1].contract_id != contracts[2].contract_id


async def test_e2e_ratio_back_adjust_seam_continuous():
    """adjustment='ratio': the seam jump is removed (continuous series), the
    most-recent segment is unchanged, the old segment is scaled."""
    dates, chains = _rolled_mid_chains(
        old_mid=10.0, new_mid_first=13.0, new_mid_second=14.0
    )
    values, errors, _c = await _resolve(dates, chains, adjustment="ratio")
    assert all(e is None for e in errors)
    # factor = new/old = 13/10 = 1.3 applied to the OLD segment (idx 0,1).
    np.testing.assert_allclose(values, [13.0, 13.0, 13.0, 14.0])
    # Seam continuous: last adjusted old == first new.
    assert values[1] == pytest.approx(values[2])


async def test_e2e_difference_back_adjust_seam_continuous():
    """adjustment='difference': additive seam removal; recent segment fixed."""
    dates, chains = _rolled_mid_chains(
        old_mid=10.0, new_mid_first=13.0, new_mid_second=14.0
    )
    values, errors, _c = await _resolve(dates, chains, adjustment="difference")
    assert all(e is None for e in errors)
    # offset = new - old = 13 - 10 = +3 applied to the OLD segment.
    np.testing.assert_allclose(values, [13.0, 13.0, 13.0, 14.0])
    assert values[1] == pytest.approx(values[2])


async def test_e2e_roll_boundary_is_where_expected():
    """Pin that the maturity resolver rolls APR→MAY exactly between 04-18 and
    04-19 (the assumption the e2e gap tests rely on)."""
    r = DefaultMaturityResolver()
    rule = NextThirdFriday(offset_months=0)
    assert r.resolve(ref_date=date(2024, 4, 18), rule=rule) == _APR
    assert r.resolve(ref_date=date(2024, 4, 19), rule=rule) == _MAY


async def test_e2e_non_price_stream_ignores_adjustment():
    """A non-price stream (iv) with adjustment='ratio' returns the RAW series
    (adjustment ignored, no error)."""
    # iv differs per segment so "unchanged" is a meaningful assertion.
    d0, d1, d2, d3 = (
        date(2024, 4, 17),
        date(2024, 4, 18),
        date(2024, 4, 19),
        date(2024, 4, 22),
    )
    chains = {
        d0: [(_OLD, _row(row_date=d0, iv=0.20))],
        d1: [(_OLD, _row(row_date=d1, iv=0.20))],
        d2: [(_NEW, _row(row_date=d2, iv=0.30))],
        d3: [(_NEW, _row(row_date=d3, iv=0.30))],
    }
    dates = [d0, d1, d2, d3]
    values, errors, _c = await _resolve(dates, chains, adjustment="ratio", stream="iv")
    assert all(e is None for e in errors)
    # Raw iv, NOT back-adjusted (no ratio applied across the seam).
    np.testing.assert_allclose(values, [0.20, 0.20, 0.30, 0.30])


async def test_e2e_zero_mid_at_seam_leaves_gap_no_crash():
    """A zero reference mid at the seam → that gap is left unadjusted, the
    resolver does not crash, and the rest of the series is intact."""
    # OLD segment mid = 0.0 (the only reference for the gap) → seam skipped.
    dates, chains = _rolled_mid_chains(
        old_mid=0.0, new_mid_first=13.0, new_mid_second=14.0
    )
    values, errors, _c = await _resolve(dates, chains, adjustment="ratio")
    assert all(e is None for e in errors)
    # Seam skipped → OLD segment left at its raw 0.0; NEW segment untouched.
    np.testing.assert_allclose(values, [0.0, 0.0, 13.0, 14.0])


async def test_e2e_nan_mid_at_seam_leaves_gap_no_crash():
    """A NaN reference mid at the seam (OLD has no quoted mid) → gap left
    unadjusted, no crash."""
    # OLD rows carry mid=None → values NaN there; the seam's only reference
    # marks are NaN → seam skipped.  NEW segment present.
    d0, d1, d2, d3 = (
        date(2024, 4, 17),
        date(2024, 4, 18),
        date(2024, 4, 19),
        date(2024, 4, 22),
    )
    chains = {
        d0: [(_OLD, _row(row_date=d0, mid=None))],
        d1: [(_OLD, _row(row_date=d1, mid=None))],
        d2: [(_NEW, _row(row_date=d2, mid=13.0))],
        d3: [(_NEW, _row(row_date=d3, mid=14.0))],
    }
    dates = [d0, d1, d2, d3]
    values, errors, _c = await _resolve(dates, chains, adjustment="ratio")
    # OLD mid missing → those dates NaN with missing_mid; NEW dates real.
    assert np.isnan(values[0]) and np.isnan(values[1])
    assert errors[0] == "missing_mid" and errors[1] == "missing_mid"
    assert values[2] == pytest.approx(13.0)
    assert values[3] == pytest.approx(14.0)


async def test_e2e_skipped_seam_emits_warning(caplog):
    """When a seam is skipped (no finite, non-zero reference mid), the resolver
    emits a WARNING naming the skip and the unadjusted gap — the operator's
    only signal that a roll was left uncorrected.  Same zero-mid scenario as
    ``test_e2e_zero_mid_at_seam_leaves_gap_no_crash``, here asserting the log.
    """
    _resolver_logger = "tcg.engine.options.series.stream_resolver"
    dates, chains = _rolled_mid_chains(
        old_mid=0.0, new_mid_first=13.0, new_mid_second=14.0
    )
    with caplog.at_level(logging.WARNING, logger=_resolver_logger):
        values, errors, _c = await _resolve(dates, chains, adjustment="ratio")

    # The series is intact (seam left unadjusted), as the sibling test pins.
    assert all(e is None for e in errors)
    np.testing.assert_allclose(values, [0.0, 0.0, 13.0, 14.0])

    # Exactly the seam-skip WARNING fired, from the resolver's logger, carrying
    # the two diagnostic substrings the brief calls out.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == _resolver_logger
    ]
    assert len(warnings) == 1, f"expected one seam-skip warning, got {warnings!r}"
    msg = warnings[0].getMessage()
    assert "roll skipped" in msg
    assert "Unadjusted gap remains" in msg


async def test_e2e_clean_roll_emits_no_warning(caplog):
    """Negative control: a roll whose seam IS resolvable adjusts silently — no
    seam-skip WARNING — so the warning above is specific to the skip path."""
    _resolver_logger = "tcg.engine.options.series.stream_resolver"
    dates, chains = _rolled_mid_chains(
        old_mid=10.0, new_mid_first=13.0, new_mid_second=14.0
    )
    with caplog.at_level(logging.WARNING, logger=_resolver_logger):
        _values, errors, _c = await _resolve(dates, chains, adjustment="ratio")
    assert all(e is None for e in errors)
    assert not [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == _resolver_logger
    ]


async def test_e2e_same_expiration_strike_shift_uses_same_day_mid():
    """Same-expiration strike-shift seam: the rolled-OUT contract IS present in
    the bulk ``chain_index`` on the seam day (same expiration, within the
    strike window), so the SAME-DAY gap is used (Tier 1), NOT the adjacent-day
    marks.

    This is the seam shape the team-lead's note describes ("contract A is still
    in the chain that day, just not selected").  It holds for STRIKE-SHIFT
    seams; for a maturity (expiration) roll the off-expiration contract is in a
    different bulk-fetch group and is absent from ``chain_index`` that day, so
    those seams fall back to adjacent observed marks (see the ratio/difference
    e2e tests above).  Both are correct; this test pins the same-day path.

    Setup (one expiration EXP on every date; ByDelta ATM selection):
      day0,1: spot 4500 → ATM = K4500 (delta .50), mid 10.0; K4400 also quoted.
      day2:   spot 4400 → ATM = K4400 (delta .50), mid 12.0; K4500 STILL quoted
              that day at mid 11.0 (delta .38).
    Seam at idx 2 (K4500 → K4400).  Same-day gap = new/old = 12/11 (K4400@day2 /
    K4500@day2).  Adjacent-marks gap would be 12/10.  We assert 12/11 fires.
    """
    exp = date(2024, 5, 17)
    k4400 = _contract(strike=4400, expiration=exp)
    k4500 = _contract(strike=4500, expiration=exp)
    d0, d1, d2 = date(2024, 4, 15), date(2024, 4, 16), date(2024, 4, 17)
    chains = {
        d0: [
            (k4500, _row(row_date=d0, mid=10.0, delta=0.50)),
            (k4400, _row(row_date=d0, mid=14.0, delta=0.62)),
        ],
        d1: [
            (k4500, _row(row_date=d1, mid=10.0, delta=0.50)),
            (k4400, _row(row_date=d1, mid=14.0, delta=0.62)),
        ],
        d2: [
            # K4500 still quoted on the seam day though no longer ATM-selected.
            (k4500, _row(row_date=d2, mid=11.0, delta=0.38)),
            (k4400, _row(row_date=d2, mid=12.0, delta=0.50)),
        ],
    }
    dates = [d0, d1, d2]

    async def spot(_contract_doc, on_date):
        return 4500.0 if on_date in (d0, d1) else 4400.0

    reader = FakeChainReader(chains)
    bulk_reader = FakeBulkChainReader(chains)
    values, errors, contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        # offset_months=1 keeps the resolved expiration constant (EXP) across
        # all three dates so they share one bulk-fetch group → chain_index[d2]
        # contains BOTH strikes.
        maturity=NextThirdFriday(offset_months=1),
        selection=ByDelta(target_delta=0.50, tolerance=0.2, strict=False),
        stream="mid",
        adjustment="ratio",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=spot,
        bulk_chain_reader=bulk_reader,
    )
    assert all(e is None for e in errors)
    # Seam is a strike shift within one expiration.
    assert contracts[1].contract_id == k4500.contract_id
    assert contracts[2].contract_id == k4400.contract_id
    # SAME-DAY factor 12/11 applied to the old segment — distinct from the
    # adjacent-marks factor 12/10.
    same_day_factor = 12.0 / 11.0
    assert values[0] == pytest.approx(10.0 * same_day_factor)
    assert values[1] == pytest.approx(10.0 * same_day_factor)
    assert values[2] == pytest.approx(12.0)  # most-recent segment unchanged
    # Guard against the adjacent-marks fallback silently being used instead.
    assert values[0] != pytest.approx(10.0 * (12.0 / 10.0))
