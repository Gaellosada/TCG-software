"""Futures-notional sizing for the hold-mode option $-P&L accumulator.

Wave-1 backend feature.  Exercises ``_compound_with_hold`` / ``_HoldPnLSpec`` in
``futures_notional`` mode DIRECTLY against a hand-computed oracle (no dwh), plus:

  * SC2 hand-validation for OPT_SP_500 (M_opt == M_fut == 50) and OPT_VIX
    (M_opt == 100 != M_fut == 1000) — the numbers documented in
    ``output/backend-iter1.md``;
  * a synthetic futures oracle mirroring ``tests/_hold_pnl_oracle.oracle_ratio``
    but sizing off ``F_ref·M_fut`` and booking ``qty·Δprem·M_opt``;
  * the tail carry-forward when a roll has no covering future (F_ref NaN);
  * premium_notional stays byte-identical when the new fields default.
"""

from __future__ import annotations

import numpy as np

from tcg.engine.hold_pnl import _compound_with_hold, _HoldPnLSpec

from _hold_pnl_oracle import oracle_ratio_futures


def _run(spec: _HoldPnLSpec, T: int) -> np.ndarray:
    ratio, _scale, _contrib = _compound_with_hold(
        np.zeros(max(T - 1, 0), dtype=np.float64), [spec]
    )
    return ratio


# ── SC2 hand-validation: OPT_SP_500 (M_opt == M_fut == 50) ──────────────────
def test_sc2_sp500_hand_validation() -> None:
    """Short SP_500 put, single segment, hand-computed.

    NAV0=1, F_ref=4500, M_fut=M_opt=50, nav_times=1, short (sign=-1).
    qty = 1/(4500·50) = 4.4444e-6 (base-1).
    premium 30 -> 28 -> 26 (held).
    day1 dprem=-2  → contrib = -qty·(-2)·50 = +4.4444e-4 → ratio 1.00044444
    day2 dprem=-2  → contrib = -qty·(-2)·50 = +4.4444e-4 → ratio 1.00088889 (approx;
    compounding uses ratio[s] but net==0 elsewhere so it is exactly additive here).
    """
    premium = np.array([30.0, 28.0, 26.0])
    is_roll = np.array([True, False, False])
    roll_premium = np.array([30.0, np.nan, np.nan])
    roll_fref = np.array([4500.0, np.nan, np.nan])
    spec = _HoldPnLSpec(
        ref_id="sp",
        sign=-1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(3, dtype=np.bool_),
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=50.0,
        mult_opt=50.0,
    )
    ratio = _run(spec, 3)

    # Hand-computed expected (qty fixed for the single segment):
    qty = 1.0 / (4500.0 * 50.0)
    exp1 = 1.0 + (-1.0) * qty * (-2.0) * 50.0
    exp2 = exp1 + (-1.0) * qty * (-2.0) * 50.0  # ratio[s]==exp1, but contrib formula
    # divides by ratio[s]; the joint pass multiplies back, so the ADDITIVE hand form
    # matches only when we route through the same recurrence — assert vs the oracle.
    oracle = oracle_ratio_futures(
        owner_prev=np.array([np.nan, 30.0, 28.0]),
        owner_cur=np.array([np.nan, 28.0, 26.0]),
        is_roll=is_roll.astype(np.float64),
        roll_future_ref=roll_fref,
        nav_times=1.0,
        weight=-1.0,
        m_fut=50.0,
        m_opt=50.0,
    )
    np.testing.assert_allclose(ratio, oracle, rtol=1e-12, atol=1e-12)
    # And the first step matches the pure hand number exactly:
    assert abs(ratio[1] - exp1) < 1e-12
    assert abs(ratio[1] - 1.0004444444444) < 1e-9
    # exp2 is the additive form; the true ratio[2] compounds via ratio[1] — assert
    # against the oracle value which is the ground truth (they agree to ~1e-6):
    assert abs(ratio[2] - oracle[2]) < 1e-12
    _ = exp2  # documented, not asserted directly (see oracle)


# ── SC2 hand-validation: OPT_VIX (M_opt == 100 != M_fut == 1000) ────────────
def test_sc2_vix_hand_validation() -> None:
    """Short VIX put — the multiplier split (opt 100, fut 1000) is the whole test.

    NAV0=1, F_ref=18.0, M_fut=1000, M_opt=100, nav_times=1, short (sign=-1).
    qty = 1/(18·1000) = 5.55556e-5 (base-1).
    premium 2.0 -> 1.5 -> 1.2 (held).
    day1 dprem=-0.5 → contrib = -qty·(-0.5)·100 = +2.77778e-3 → ratio 1.00277778
    day2 dprem=-0.3 → contrib compounds off ratio[1].
    """
    premium = np.array([2.0, 1.5, 1.2])
    is_roll = np.array([True, False, False])
    roll_premium = np.array([2.0, np.nan, np.nan])
    roll_fref = np.array([18.0, np.nan, np.nan])
    spec = _HoldPnLSpec(
        ref_id="vix",
        sign=-1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(3, dtype=np.bool_),
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=1000.0,
        mult_opt=100.0,
    )
    ratio = _run(spec, 3)

    qty = 1.0 / (18.0 * 1000.0)
    exp1 = 1.0 + (-1.0) * qty * (-0.5) * 100.0
    assert abs(ratio[1] - exp1) < 1e-12
    assert abs(ratio[1] - 1.0027777777778) < 1e-9

    oracle = oracle_ratio_futures(
        owner_prev=np.array([np.nan, 2.0, 1.5]),
        owner_cur=np.array([np.nan, 1.5, 1.2]),
        is_roll=is_roll.astype(np.float64),
        roll_future_ref=roll_fref,
        nav_times=1.0,
        weight=-1.0,
        m_fut=1000.0,
        m_opt=100.0,
    )
    np.testing.assert_allclose(ratio, oracle, rtol=1e-12, atol=1e-12)

    # Sanity: the SAME premiums under premium_notional (denominator = premium, no
    # M_opt) give a DIFFERENT curve — proves the multiplier split is really applied.
    prem_spec = _HoldPnLSpec(
        ref_id="vix",
        sign=-1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(3, dtype=np.bool_),
    )
    prem_ratio = _run(prem_spec, 3)
    assert not np.allclose(prem_ratio, ratio)


# ── Multi-segment (APR→MAY roll) futures oracle ─────────────────────────────
def test_futures_multi_segment_matches_oracle() -> None:
    from _hold_pnl_oracle import (
        HELD_PREMIUM,
        IS_ROLL,
        OWNER_CUR,
        OWNER_PREV,
        ROLL_PREMIUM,
    )

    # F_ref at the two rolls (idx 0 and 3): SPX-ish 4500 then 4520.
    roll_fref = np.array([4500.0, np.nan, np.nan, 4520.0, np.nan, np.nan])
    spec = _HoldPnLSpec(
        ref_id="opt",
        sign=-1.0,
        nav_times=2.0,
        premium=HELD_PREMIUM,
        is_roll=IS_ROLL > 0.5,
        roll_premium=ROLL_PREMIUM,
        pos_active=np.ones(6, dtype=np.bool_),
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=50.0,
        mult_opt=50.0,
    )
    ratio = _run(spec, 6)
    oracle = oracle_ratio_futures(
        owner_prev=OWNER_PREV,
        owner_cur=OWNER_CUR,
        is_roll=IS_ROLL,
        roll_future_ref=roll_fref,
        nav_times=2.0,
        weight=-1.0,
        m_fut=50.0,
        m_opt=50.0,
    )
    np.testing.assert_allclose(ratio, oracle, rtol=1e-12, atol=1e-12)


# ── Tail carry-forward: a roll with no covering future (F_ref NaN) ──────────
def test_tail_carry_forward_missing_future_at_roll() -> None:
    """When the 2nd roll's F_ref is NaN, the qty from the 1st roll is carried
    forward (keeps accruing option $ P&L on the new contract), never re-sized off
    missing data and never silently 0/1.0-sized."""
    premium = np.array([30.0, 28.0, 26.0, 24.0, 20.0, 19.0])
    is_roll = np.array([True, False, False, True, False, False])
    roll_premium = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
    # 2nd roll (idx 3) has NO covering future → NaN → carry-forward the idx-0 qty.
    roll_fref = np.array([4500.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    spec = _HoldPnLSpec(
        ref_id="opt",
        sign=-1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(6, dtype=np.bool_),
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=50.0,
        mult_opt=50.0,
    )
    ratio = _run(spec, 6)

    # Reference: qty stays frozen at the idx-0 sizing across the whole series,
    # with the premium base re-anchoring to the NEW contract open at the roll
    # (roll_premium[3]=18) so the roll seam is NOT booked.
    base_nav = 1_000_000.0
    qty = 1.0 * base_nav / (4500.0 * 50.0)
    owner_prev = np.array([np.nan, 30.0, 28.0, 26.0, 18.0, 20.0])
    owner_cur = np.array([np.nan, 28.0, 26.0, 24.0, 20.0, 19.0])
    nav = np.empty(6)
    nav[0] = base_nav
    for t in range(1, 6):
        dprem = owner_cur[t] - owner_prev[t]
        nav[t] = nav[t - 1] + (-1.0) * qty * dprem * 50.0
        # NO resize at idx 3 because F_ref is NaN → qty carried forward.
    exp = nav / nav[0]
    np.testing.assert_allclose(ratio, exp, rtol=1e-10, atol=1e-10)
    # Curve must be finite everywhere (no NaN leaked from the missing future).
    assert np.all(np.isfinite(ratio))


# ── Off-roll re-entry: a futures leg that exits then re-latches mid-segment ──
def test_futures_offroll_reentry_resizes_and_books_pnl() -> None:
    """A ``futures_notional`` hold leg that goes flat then re-opens on a NON-roll
    bar must re-size off the CURRENT segment's frozen reference (carried forward
    from the roll) and book correctly-sized P&L on the re-entry window.

    Regression for ENG-SIZING-1: ``roll_future_ref`` is finite only at roll bars,
    so an off-roll re-open read a NaN reference, failed ``_futures_denom_ok``, and
    the leg silently booked ZERO until the next roll (premium mode re-sized fine on
    the same bar — a silent asymmetry).
    """
    # Single segment (roll at idx 0 only).  Held [0,1], flat [2,3], re-open [4,5].
    premium = np.array([30.0, 29.0, 28.0, 27.0, 26.0, 24.0])
    is_roll = np.array([True, False, False, False, False, False])
    roll_premium = np.array([30.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    roll_fref = np.array([4500.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    pos_active = np.array([True, True, False, False, True, True])
    spec = _HoldPnLSpec(
        ref_id="opt",
        sign=-1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=pos_active,
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=50.0,
        mult_opt=50.0,
    )
    ratio, _scale, contrib = _compound_with_hold(np.zeros(5, dtype=np.float64), [spec])

    # Closed window (steps 1,2,3) books exactly 0.
    assert contrib["opt"][1] == 0.0
    assert contrib["opt"][2] == 0.0
    assert contrib["opt"][3] == 0.0

    # Re-entry step (bar 4→5): sized off the frozen F_ref=4500 (carried forward),
    # premium base re-anchored to the re-open mid premium[4]=26 → dprem=24-26=-2.
    # seg_er == ratio[4] on the re-open bar so the equity-coupling cancels to 1.
    expected = (-1.0) * 1.0 * ((-2.0) * 50.0) / (4500.0 * 50.0)
    assert contrib["opt"][4] != 0.0
    assert abs(contrib["opt"][4] - expected) < 1e-12
    # And it flows into the equity curve.
    assert abs((ratio[5] / ratio[4] - 1.0) - expected) < 1e-12
    assert np.all(np.isfinite(ratio))


# ── P-OFFROLL-SIZING: an off-roll FIRST open, never held at a roll ───────────
def test_offroll_first_open_sized_via_daily_future_ref() -> None:
    """A ``futures_notional`` hold leg whose signal entry FIRST latches on a
    NON-roll bar — and that was NOT held at any prior roll — must be sized off the
    per-date reference-future price (``daily_future_ref``) and book non-zero,
    correctly-sized P&L on the held window.

    Regression for P-OFFROLL-SIZING: ``roll_future_ref`` is finite ONLY at roll
    bars, and ``seg_fref`` starts NaN, so a leg that opens off-roll before any
    roll-while-held read NaN, failed ``_futures_denom_ok``, and silently booked
    ZERO across the whole hold (this is what dropped the §5.5 Aug-2024 spike gain).
    The daily front-future reference rescues ONLY that previously-unsized case.
    """
    # Single option segment (roll at idx 0), but the leg is FLAT at the roll and
    # first latches at idx 2 (off-roll).  Held [2,3,4,5]; long (sign=+1) call whose
    # premium RISES through the window (mirrors a VIX call in a spike).
    premium = np.array([10.0, 10.0, 10.0, 12.0, 15.0, 20.0])
    is_roll = np.array([True, False, False, False, False, False])
    roll_premium = np.array([10.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    roll_fref = np.array([4500.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    # Leg NOT active at the idx-0 roll → seg_fref is never initialised.
    pos_active = np.array([False, False, True, True, True, True])
    # Per-date reference-future price, finite on every bar (the rescue source).
    daily_fref = np.array([4400.0, 4500.0, 4600.0, 4600.0, 4600.0, 4600.0])
    spec = _HoldPnLSpec(
        ref_id="opt",
        sign=1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=pos_active,
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=50.0,
        mult_opt=50.0,
        daily_future_ref=daily_fref,
    )
    ratio, _scale, contrib = _compound_with_hold(np.zeros(5, dtype=np.float64), [spec])

    # Flat window (steps 0,1) books exactly 0.
    assert contrib["opt"][0] == 0.0
    assert contrib["opt"][1] == 0.0
    # First HELD step (bar 2→3): sized off daily_future_ref[2]=4600 (open bar), NOT
    # the idx-0 roll_future_ref (leg was flat there).  seg_er==ratio[2] so the
    # equity coupling cancels to 1; dprem = premium[3]-premium[2] = 12-10 = 2.
    expected = 1.0 * 1.0 * (2.0 * 50.0) / (4600.0 * 50.0)
    assert contrib["opt"][2] != 0.0, "off-roll first open still booked ZERO"
    assert abs(contrib["opt"][2] - expected) < 1e-12
    # Subsequent held steps also accrue (the leg is sized for the whole window).
    assert contrib["opt"][3] != 0.0
    assert contrib["opt"][4] != 0.0
    # A rising long-call premium sized long ⇒ the equity curve rises and is finite.
    assert ratio[-1] > ratio[2]
    assert np.all(np.isfinite(ratio))


# ── P-OFFROLL-SIZING byte-identity: roll_future_ref WINS over daily ─────────
def test_daily_future_ref_ignored_when_roll_ref_present() -> None:
    """When the leg IS held at a roll (``roll_future_ref`` finite) the daily
    reference is NEVER consulted — the result is byte-identical to ``daily=None``.

    This is the byte-identity gate: the rescue may ONLY size the previously-NaN
    case; a roll-aligned leg (the common case, e.g. §5.6's 02-03 roll and every
    always-on option leg) must be UNCHANGED even if a (deliberately wrong) daily
    reference is supplied.
    """
    premium = np.array([30.0, 28.0, 26.0, 24.0, 20.0, 19.0])
    is_roll = np.array([True, False, False, True, False, False])
    roll_premium = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
    roll_fref = np.array([4500.0, np.nan, np.nan, 4520.0, np.nan, np.nan])
    common = dict(
        ref_id="opt",
        sign=-1.0,
        nav_times=1.0,
        premium=premium,
        is_roll=is_roll,
        roll_premium=roll_premium,
        pos_active=np.ones(6, dtype=np.bool_),
        sizing_mode="futures_notional",
        roll_future_ref=roll_fref,
        mult_fut=50.0,
        mult_opt=50.0,
    )
    ratio_none = _run(_HoldPnLSpec(**common), 6)
    # A deliberately WRONG daily reference on every bar — must be ignored because
    # roll_future_ref / seg_fref are finite on every sizing point.
    wrong_daily = np.full(6, 1.0, dtype=np.float64)
    ratio_daily = _run(_HoldPnLSpec(**common, daily_future_ref=wrong_daily), 6)
    np.testing.assert_array_equal(ratio_none, ratio_daily)


# ── premium_notional unchanged when the new fields default ──────────────────
def test_premium_notional_byte_identical_default() -> None:
    from _hold_pnl_oracle import (
        HELD_PREMIUM,
        IS_ROLL,
        OWNER_CUR,
        OWNER_PREV,
        ROLL_PREMIUM,
        oracle_ratio,
    )

    spec = _HoldPnLSpec(
        ref_id="opt",
        sign=-1.0,
        nav_times=1.5,
        premium=HELD_PREMIUM,
        is_roll=IS_ROLL > 0.5,
        roll_premium=ROLL_PREMIUM,
        pos_active=np.ones(6, dtype=np.bool_),
    )
    ratio = _run(spec, 6)
    oracle = oracle_ratio(
        OWNER_PREV, OWNER_CUR, IS_ROLL, ROLL_PREMIUM, nav_times=1.5, weight=-1.0
    )
    np.testing.assert_allclose(ratio, oracle, rtol=1e-12, atol=1e-12)
