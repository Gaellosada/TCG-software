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

import importlib.util
import sys
from datetime import date
from pathlib import Path

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

from _hold_pnl_oracle import (
    DATES_INT as _DATES_INT,
    HELD_PREMIUM as _HELD_PREMIUM,
    IS_ROLL as _IS_ROLL,
    OWNER_CUR as _OWNER_CUR,
    OWNER_PREV as _OWNER_PREV,
    ROLL_PREMIUM as _ROLL_PREMIUM,
    make_hold_fetch,
    oracle_ratio as _oracle_ratio,
)

# Async tests auto-marked (asyncio_mode="auto").


# ---------------------------------------------------------------------------
# The REAL ground-truth oracle — loaded from the Wave-B validation output
# (outside the ``tcg`` package, so imported by file path).  This is the actual
# ``java_faithful_s1`` the engine is measured against — NOT an engine-mirror.
# It touches no data source and imports only numpy.  Used by the interior-NaN
# carry-forward test (review "TEST 4") so we assert against the oracle's OWN
# carry-forward accounting, not the resolver-step mirror ``_oracle_ratio`` below.
# ---------------------------------------------------------------------------


def _load_java_faithful_s1():
    """Import the real ``java_faithful_s1`` oracle by absolute file path.

    The oracle lives in ``workspace/tasks/engine-groundtruth-validation/output/
    waveB0/`` (a sibling of the TCG-software repo), so it is not on the package
    path.  Registering the module in ``sys.modules`` BEFORE ``exec_module`` is
    required on Python 3.14: the oracle's ``@dataclass`` with an ``npt.NDArray``
    annotation makes the dataclass machinery look up ``cls.__module__`` in
    ``sys.modules`` at class-creation time.
    """
    repo_root = Path(__file__).resolve().parents[3]  # …/TCG-software
    oracle_path = (
        repo_root.parent
        / "workspace"
        / "tasks"
        / "engine-groundtruth-validation"
        / "output"
        / "waveB0"
        / "java_faithful_s1.py"
    )
    if not oracle_path.is_file():
        pytest.skip(f"ground-truth oracle not found at {oracle_path}")
    spec = importlib.util.spec_from_file_location(
        "java_faithful_s1_oracle", oracle_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # 3.14 dataclass-annotation fix (see docstring)
    spec.loader.exec_module(module)
    return module.java_faithful_s1


# ---------------------------------------------------------------------------
# The reference oracle (``_oracle_ratio``) and the APR→MAY roll fixture
# (``_HELD_PREMIUM`` / ``_IS_ROLL`` / ``_ROLL_PREMIUM`` / ``_OWNER_PREV`` /
# ``_OWNER_CUR``) are the SHARED hold-P&L helpers imported from
# ``tests/_hold_pnl_oracle`` (byte-for-byte the Java-faithful ``java_faithful_s1``
# accounting: size once per roll off the compounding NAV, hold fixed, book
# ``sign(weight)·qty·(cur-prev)`` daily, realise+resize at each roll, normalise to
# a base-1 ratio).  Expressed as the resolver's hold-mode OUTPUT (held premium
# LEVEL + is_roll + roll_premium) so these tests drive ``signal_exec`` directly.
# ---------------------------------------------------------------------------


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

    Delegates to the shared ``make_hold_fetch`` builder: ``fetch`` returns the held
    premium LEVEL as the option's close series, and ``fetch.fetch_hold_roll_info``
    returns the (dates, is_roll, roll_premium) side-channel signal_exec consults for
    hold-mode option inputs (spot defaults to a flat 100.0 series)."""
    return make_hold_fetch(
        held_premium=held_premium, is_roll=is_roll, roll_premium=roll_premium, spx=spx
    )


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


# ---------------------------------------------------------------------------
# Review TEST 4 — interior no-quote day inside a held segment. The oracle
# CARRIES the last finite premium forward as the P&L base; the engine must too
# (so the full move across the gap is booked, not dropped). Drives the REAL
# ``java_faithful_s1`` oracle AND the real ``_compound_with_hold`` (via the full
# ``evaluate_signal`` path) on the SAME single-segment interior-gap series.
# ---------------------------------------------------------------------------

# Single held segment (only the initial open is a roll), one interior NaN gap.
# premium = [10, 9, NaN, 8, 7.5]; short.  For a single segment the engine's
# owner-of-step premium series and the oracle's premium_used_for_pnl COINCIDE
# (no OLD/NEW seam), so the two consume identical arrays and the mapping is 1:1.
_GAP_PREMIUM = np.array([10.0, 9.0, np.nan, 8.0, 7.5])
_GAP_IS_ROLL = np.array([1.0, 0.0, 0.0, 0.0, 0.0])
_GAP_ROLL_PREMIUM = np.array([10.0, np.nan, np.nan, np.nan, np.nan])
_GAP_DATES_INT = np.array(
    [20240102, 20240103, 20240104, 20240105, 20240108], dtype=np.int64
)


def _make_gap_fetcher():
    """Fetcher for the 5-day interior-gap single-segment series (a short put +
    an always-on spot input latching the entry from bar 0)."""
    spx = np.full(len(_GAP_DATES_INT), 100.0, dtype=np.float64)

    async def fetch(instrument, field):
        if isinstance(instrument, InstrumentSpot):
            return _GAP_DATES_INT, spx
        if isinstance(instrument, InstrumentOptionStream):
            return _GAP_DATES_INT, _GAP_PREMIUM.copy()
        raise KeyError(f"no data for {instrument!r} ({field})")

    async def fetch_hold_roll_info(instrument):
        assert isinstance(instrument, InstrumentOptionStream)
        return (
            _GAP_DATES_INT,
            _GAP_IS_ROLL.copy(),
            _GAP_ROLL_PREMIUM.copy(),
        )

    fetch.fetch_hold_roll_info = fetch_hold_roll_info  # type: ignore[attr-defined]
    return fetch


async def test_hold_pnl_interior_nan_gap_carries_forward_matches_real_oracle():
    """Interior-NaN carry-forward (review N1 / TEST 4): the engine NAV ratio must
    equal the REAL ``java_faithful_s1`` ``equity_base100/100`` to ~1e-12.

    Before the fix the engine DROPPED the P&L across the gap: it produced
    [1, 1.1, 1.1, 1.1, 1.15] vs the oracle [1, 1.1, 1.1, 1.2, 1.25].  After the
    carry-forward fix (base = last finite premium on an interior day) they match.
    """
    java_faithful_s1 = _load_java_faithful_s1()

    # Oracle: nav_times=1.0; the oracle's implicit direction is the short (falling
    # premium → gain), which is exactly the engine's weight<0 (short) convention.
    oracle = java_faithful_s1(
        {
            "date": [date(2024, 1, d) for d in (2, 3, 4, 5, 8)],
            "premium_used_for_pnl": _GAP_PREMIUM.copy(),
            "is_roll": _GAP_IS_ROLL.astype(bool),
        },
        nav_times=1.0,
    )
    oracle_ratio = oracle.equity_base100 / 100.0

    # Engine: the full evaluate_signal path (drives the real _compound_with_hold).
    fetch = _make_gap_fetcher()
    res = await evaluate_signal(
        _short_put_signal(hold=True, weight=-10.0, nav_times=1.0), {}, fetch
    )

    # The fix must reproduce the oracle's carry-forward EXACTLY.
    np.testing.assert_allclose(res.equity_ratio, oracle_ratio, rtol=1e-12, atol=1e-14)
    # Pin the concrete expected values (guards against a coincidental match).
    np.testing.assert_allclose(
        res.equity_ratio, np.array([1.0, 1.1, 1.1, 1.2, 1.25]), rtol=1e-12, atol=1e-14
    )


async def test_hold_pnl_interior_nan_gap_reconciliation_holds():
    """Σ per-input realized_pnl == equity_ratio − 1 STILL holds across the gap
    after the carry-forward change (the reconciliation invariant is preserved)."""
    fetch = _make_gap_fetcher()
    res = await evaluate_signal(
        _short_put_signal(hold=True, weight=-10.0, nav_times=2.0), {}, fetch
    )
    total = np.zeros_like(res.equity_ratio)
    for p in res.positions:
        total = total + p.realized_pnl
    np.testing.assert_allclose(total, res.equity_ratio - 1.0, rtol=1e-11, atol=1e-13)
