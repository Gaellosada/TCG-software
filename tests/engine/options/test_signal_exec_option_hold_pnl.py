"""Fixed-contract DOLLAR P&L for held option positions in ``signal_exec``.

This pins the NEW option-P&L accounting path that a hold-mode
(``hold_between_rolls=True``) option input takes.  It is the load-bearing
correctness suite: it reproduces the ground-truth Java short-put simulation
(the ORACLE ``java_faithful_s1``) EXACTLY on the oracle's own accounting.

The model (see the resolver + the oracle docstring)
---------------------------------------------------
At each roll the held quantity is sized off the compounding NAV and the roll
premium: ``qty = nav_times · NAV_at_roll / premium_at_roll``.  Held FIXED within
the roll, the daily $ P&L is ``sign · qty · Δpremium`` (short: falling premium →
gain).  NAV compounds; at each roll the position is realised (seam-free, since
unrealised == 0 at the new open) and re-sized.  The contribution as a fraction of
current NAV — so it composes with the engine's existing compounding — is

    contrib[t] = sign · nav_times · (equity_ratio[roll] / equity_ratio[t-1])
                        · (premium[t] − premium[t-1]) / premium[roll]

with ``sign = sign(block weight)`` (the ENGINE convention: a long gains on rising
premium, a short on falling — identical to the price path's ``pos·Δprice/price``).
This COUPLES to ``equity_ratio[t-1]`` (path-dependent), so it is accumulated
SEQUENTIALLY and combined with any other inputs' returns in ONE joint pass that
also applies the engine's ruin clamp.  The invariant ``Σ realized_pnl ==
equity_ratio − 1`` is preserved.

The resolver hold-mode output (held premium LEVEL + ``is_roll`` / ``roll_premium``)
is fed through a fetcher side-channel (``fetch_hold_roll_info``); these tests wire
a synthetic fetcher directly so they are dwh-free and deterministic.
"""

from __future__ import annotations

import numpy as np

import pytest

from tcg.engine.signal_exec import SignalDataError, evaluate_signal
from tcg.types.options import ByDelta, NearestToTarget
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

# Async tests auto-marked (asyncio_mode="auto").


# ---------------------------------------------------------------------------
# Reference oracle — the Java-faithful fixed-contract dollar-P&L accounting.
# Byte-for-byte the accounting of ``java_faithful_s1`` (recon §1-§5): size once
# per roll off the compounding NAV, hold fixed, book qty·(premium_{t-1}-premium_t)
# for a SHORT, realise+resize at each roll, normalise NAV to a base-1 ratio.
# ``sign`` folds direction: the Java model books qty·(prev-cur) which is the SHORT
# pnl; a LONG is the mirror qty·(cur-prev).  We pass sign=+1 for the short (to
# match java_faithful_s1's implicit short) and -1 for a long — see the mapping to
# the engine's weight sign in ``_oracle_ratio``.
# ---------------------------------------------------------------------------


def _oracle_ratio(
    owner_prev: np.ndarray,
    owner_cur: np.ndarray,
    is_roll: np.ndarray,
    roll_premium: np.ndarray,
    *,
    nav_times: float,
    weight: float,
    base_nav: float = 1_000_000.0,
) -> np.ndarray:
    """Dollar-NAV oracle → base-1 ratio, from the resolver's step representation.

    ``owner_prev[t]`` / ``owner_cur[t]`` are the step-owner contract's mids on
    days t-1 / t (same contract per step; OLD contract for the step ending on a
    roll day).  ``roll_premium`` at each ``is_roll`` date is the NEW segment's
    roll-day open mid.  The engine weight sign maps to the oracle's dollar P&L as:
    a LONG (weight>0) gains on rising premium → dPnL = +qty·(cur-prev); a SHORT
    (weight<0) gains on falling premium → dPnL = qty·(prev-cur).  Both are
    ``sign(weight)·qty·(cur-prev)``.
    """
    T = len(owner_cur)
    nav = np.empty(T, dtype=np.float64)
    nav[0] = base_nav
    sign = 1.0 if weight > 0 else -1.0
    qty = nav_times * base_nav / roll_premium[0]
    for t in range(1, T):
        dprem = owner_cur[t] - owner_prev[t]
        if not np.isfinite(dprem):
            dprem = 0.0
        nav[t] = nav[t - 1] + sign * qty * dprem
        if bool(is_roll[t]):
            qty = nav_times * nav[t] / roll_premium[t]
    return nav / nav[0]


# ---------------------------------------------------------------------------
# Fixture — the SAME APR→MAY roll fixture the resolver tests use, but expressed
# as the resolver's hold-mode OUTPUT (held premium LEVEL + is_roll + roll_premium)
# so we drive signal_exec directly.
#   APR K4400 held mids: 30,28,26,24(roll-day OLD mid)
#   MAY K4450 held mids: 18(roll-day open),20,19
#   values[t] = owner-of-step mid LEVEL (OLD on roll day) = [30,28,26,24,20,19]
#   is_roll = [1,0,0,1,0,0]; roll_premium = [30,·,·,18,·,·]
# ---------------------------------------------------------------------------
_DATES_INT = np.array(
    [20240327, 20240328, 20240329, 20240401, 20240402, 20240403], dtype=np.int64
)
_HELD_PREMIUM = np.array([30.0, 28.0, 26.0, 24.0, 20.0, 19.0])
_IS_ROLL = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
_ROLL_PREMIUM = np.array([30.0, np.nan, np.nan, 18.0, np.nan, np.nan])
# Owner arrays for the oracle (same contract per step; OLD into the roll):
#   t1 APR 30->28, t2 28->26, t3 26->24 (OLD into roll), t4 MAY 18->20, t5 20->19
_OWNER_PREV = np.array([np.nan, 30.0, 28.0, 26.0, 18.0, 20.0])
_OWNER_CUR = np.array([np.nan, 28.0, 26.0, 24.0, 20.0, 19.0])


def _opt(*, hold: bool, nav_times: float = 1.0) -> InstrumentOptionStream:
    return InstrumentOptionStream(
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=35),
        selection=ByDelta(target_delta=-0.10, tolerance=0.20),
        stream="mid",
        hold_between_rolls=hold,
        nav_times=nav_times,
    )


def _make_fetcher(*, held_premium, is_roll, roll_premium, spx=None):
    """Synthetic fetcher for a hold-mode option input + a spot 'always-on' input.

    ``fetch`` returns the held premium LEVEL as the option's close series.
    ``fetch.fetch_hold_roll_info`` returns the (dates, is_roll, roll_premium)
    side-channel signal_exec consults for hold-mode option inputs.
    """
    if spx is None:
        spx = np.full(len(_DATES_INT), 100.0, dtype=np.float64)

    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            return _DATES_INT, spx
        if isinstance(instrument, InstrumentOptionStream):
            return _DATES_INT, np.asarray(held_premium, dtype=np.float64)
        raise KeyError(f"no data for {instrument!r} ({field})")

    async def fetch_hold_roll_info(instrument):
        assert isinstance(instrument, InstrumentOptionStream)
        return (
            _DATES_INT,
            np.asarray(is_roll, dtype=np.float64),
            np.asarray(roll_premium, dtype=np.float64),
        )

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
    return fetch


def _short_put_signal(*, hold: bool, weight: float = -10.0, nav_times: float = 1.0):
    """Always-latched option position (weight sign = direction) + a spot input
    whose always-true condition latches the entry from bar 0."""
    return Signal(
        id="s_hold",
        name="hold pnl",
        inputs=(
            Input(id="P", instrument=_opt(hold=hold, nav_times=nav_times)),
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
                    weight=weight,
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


async def test_hold_pnl_matches_oracle_exactly_short():
    """The fixed-contract $-P&L equity equals the Java-faithful oracle EXACTLY."""
    fetch = _make_fetcher(
        held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    res = await evaluate_signal(_short_put_signal(hold=True, weight=-10.0), {}, fetch)

    # nav_times default 1.0; block weight -10 → the engine size is nav_times scaled
    # by |weight|/100? NO — in hold mode the SIZE is nav_times and the sign is
    # sign(weight); |weight| does NOT scale the notional (that is the whole point
    # of nav_times).  So the oracle uses nav_times=1.0 * sign(weight)=-1.
    expected = _oracle_ratio(
        _OWNER_PREV,
        _OWNER_CUR,
        _IS_ROLL,
        _ROLL_PREMIUM,
        nav_times=1.0,
        weight=-10.0,
    )
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-12, atol=1e-14)


async def test_hold_pnl_matches_oracle_navtimes_and_long():
    """nav_times > 1 (leverage) and a LONG (weight>0) both match the oracle."""
    for weight, nav_times in [(+5.0, 2.5), (-100.0, 0.5), (+1.0, 3.0)]:
        fetch = _make_fetcher(
            held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
        )
        res = await evaluate_signal(
            _short_put_signal(hold=True, weight=weight, nav_times=nav_times), {}, fetch
        )
        expected = _oracle_ratio(
            _OWNER_PREV,
            _OWNER_CUR,
            _IS_ROLL,
            _ROLL_PREMIUM,
            nav_times=nav_times,
            weight=weight,
        )
        np.testing.assert_allclose(
            res.equity_ratio,
            expected,
            rtol=1e-12,
            atol=1e-14,
            err_msg=f"weight={weight} nav_times={nav_times}",
        )


async def test_hold_pnl_reconciliation_invariant_holds():
    """Σ per-input realized_pnl == equity_ratio − 1 (the subtle NAV-coupling risk)."""
    fetch = _make_fetcher(
        held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    res = await evaluate_signal(
        _short_put_signal(hold=True, weight=-10.0, nav_times=2.0), {}, fetch
    )
    total = np.zeros_like(res.equity_ratio)
    for p in res.positions:
        total = total + p.realized_pnl
    np.testing.assert_allclose(total, res.equity_ratio - 1.0, rtol=1e-11, atol=1e-13)


async def test_hold_pnl_does_not_explode_on_premium_decay():
    """The live-observed failure: a held short put decaying toward zero premium.

    The %-return model produced a +17,900% single day (equity → 0).  The
    fixed-contract $-P&L is BOUNDED: qty·Δpremium can never exceed qty·premium_roll
    within a hold, so equity stays finite and positive (the short GAINS as the
    premium decays)."""
    # Premium decays 30 → 0.5 within ONE hold (no roll).
    prem = np.array([30.0, 24.0, 18.0, 10.0, 4.0, 0.5])
    is_roll = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    roll_prem = np.array([30.0, np.nan, np.nan, np.nan, np.nan, np.nan])
    owner_prev = np.array([np.nan, 30.0, 24.0, 18.0, 10.0, 4.0])
    owner_cur = np.array([np.nan, 24.0, 18.0, 10.0, 4.0, 0.5])
    fetch = _make_fetcher(held_premium=prem, is_roll=is_roll, roll_premium=roll_prem)
    res = await evaluate_signal(_short_put_signal(hold=True, weight=-10.0), {}, fetch)
    assert np.all(np.isfinite(res.equity_ratio))
    # SHORT gains on decay: final equity > 1, and NO explosive spike.
    assert res.equity_ratio[-1] > 1.0
    assert np.max(np.abs(np.diff(res.equity_ratio))) < 1.0  # no +179x day
    expected = _oracle_ratio(
        owner_prev, owner_cur, is_roll, roll_prem, nav_times=1.0, weight=-10.0
    )
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-12, atol=1e-14)


async def test_hold_pnl_position_latched_short_throughout():
    """Premise guard: the option position is latched (sign(weight)) all window."""
    fetch = _make_fetcher(
        held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    res = await evaluate_signal(_short_put_signal(hold=True, weight=-10.0), {}, fetch)
    (p,) = [pr for pr in res.positions if pr.input_id == "P"]
    # position VALUE is the usual sign*|w|/100 latch (for display/latch trace); the
    # $-P&L SIZE is nav_times, not this — but the latch must be open & short.
    assert np.all(p.values < 0.0)


async def test_default_off_takes_price_return_path_unchanged():
    """hold=False → the ordinary weight-only %-return path (no $-P&L branch)."""
    # Give a plain daily-reselect premium LEVEL series (no roll info consulted).
    prem = np.array([30.0, 42.0, 60.0, 46.0, 47.0, 48.0])
    fetch = _make_fetcher(
        held_premium=prem, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    res = await evaluate_signal(_short_put_signal(hold=False, weight=-10.0), {}, fetch)
    # Ordinary path: contrib_step[s] = pos·(p[s+1]-p[s])/p[s], pos=-0.10.
    pos = -0.10
    step = pos * (prem[1:] - prem[:-1]) / prem[:-1]
    expected = np.concatenate([[1.0], np.cumprod(1.0 + step)])
    np.testing.assert_allclose(res.equity_ratio, expected, rtol=1e-12, atol=1e-14)


async def test_hold_and_default_differ():
    """The fix CHANGES the P&L: hold-mode equity != the default %-return equity for
    the same premium series."""
    fetch_hold = _make_fetcher(
        held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    fetch_off = _make_fetcher(
        held_premium=_HELD_PREMIUM, is_roll=_IS_ROLL, roll_premium=_ROLL_PREMIUM
    )
    r_hold = await evaluate_signal(_short_put_signal(hold=True), {}, fetch_hold)
    r_off = await evaluate_signal(_short_put_signal(hold=False), {}, fetch_off)
    assert not np.allclose(r_hold.equity_ratio, r_off.equity_ratio)


async def test_hold_without_roll_info_fetcher_raises_loudly():
    """A hold-mode option input whose fetcher LACKS ``fetch_hold_roll_info`` must
    fail LOUDLY (SignalDataError) — the fixed-contract dollar-P&L path cannot run
    without the resolver's roll structure, so it must never silently mis-handle
    (book garbage / fall back to a %-return).  DEFAULT-OFF is unaffected: the same
    bare fetcher runs fine when hold is off."""

    async def bare_fetch(instrument, field):
        # A fetcher that returns series but exposes NO fetch_hold_roll_info attr.
        if isinstance(instrument, InstrumentSpot):
            return _DATES_INT, np.full(len(_DATES_INT), 100.0, dtype=np.float64)
        if isinstance(instrument, InstrumentOptionStream):
            return _DATES_INT, _HELD_PREMIUM.copy()
        raise KeyError(instrument)

    assert not hasattr(bare_fetch, "fetch_hold_roll_info")

    with pytest.raises(SignalDataError, match="fetch_hold_roll_info"):
        await evaluate_signal(_short_put_signal(hold=True), {}, bare_fetch)

    # Same bare fetcher, hold OFF → no roll info needed → runs cleanly.
    res_off = await evaluate_signal(_short_put_signal(hold=False), {}, bare_fetch)
    assert np.all(np.isfinite(res_off.equity_ratio))
