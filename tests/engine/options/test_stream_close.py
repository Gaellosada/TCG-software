"""Tests for the ``close`` (settlement) option stream in the stream resolver.

For a held-to-roll option the exchange EOD **settlement** ``close`` is the
faithful realized mark (it reproduces the parent-platform ground truth to the
cent, while the bid-ask ``mid`` is materially STALE — see
``output/price_source_and_multiplier.md``).  ``close`` is a plain row-attribute
stream (like ``mid``) read off ``OptionDailyRow.close`` — with ONE caveat: a
non-positive / absent settlement (illiquid contracts sometimes settle 0.0 /
NULL on iVolatility, especially far-OTM puts on month-end roll dates) FALLS
BACK to the row's derived ``mid`` ((bid+ask)/2) so a false-zero settlement
neither em-dashes the cell nor poisons the P&L series.  When the mid is ALSO
missing the bar stays a loud NaN + ``missing_close``.  A per-bar/per-roll
``close_mid_fallback`` / ``roll_premium_fallback`` marker (via
``hold_roll_info_out``) records WHERE the fallback fired.

dwh-free: synthetic chains (``_stream_fakes``).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByStrike,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row

_APR = date(2024, 4, 19)
_DATES = [
    date(2024, 3, 20),
    date(2024, 3, 21),
    date(2024, 3, 22),
]
_STRIKE = 4000.0
_APR_P = _contract(strike=_STRIKE, expiration=_APR, type_="P")

# Per-date settlement close on the row (distinct from mid so a test that
# accidentally read mid would fail).
_CLOSE = {
    _DATES[0]: 93.25,
    _DATES[1]: 88.00,
    _DATES[2]: 100.00,
}

_MATURITY = NearestToTarget(target_dte_days=30)
_SELECTION = ByStrike(strike=_STRIKE)


def _build_chains(close_map=None):
    close_map = close_map or _CLOSE
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for d in _DATES:
        # mid is a WRONG placeholder (5.0) so reading mid instead of close fails.
        chains[d] = [
            (_APR_P, _row(row_date=d, mid=5.0, delta=-0.10, close=close_map[d])),
        ]
    return chains


async def _resolve(stream, *, chains=None):
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream=stream,
        chain_reader=FakeChainReader(chains or _build_chains()),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains or _build_chains()),
        available_expirations=[_APR],
    )


async def test_close_stream_reads_the_close_column() -> None:
    v, e, _c = await _resolve("close")
    assert all(err is None for err in e), e
    np.testing.assert_allclose(v, [_CLOSE[d] for d in _DATES])
    # NOT the raw row mid (5.0) — proves it read close, not mid.
    assert not np.any(np.isclose(v, 5.0))


async def test_close_zero_falls_back_to_mid() -> None:
    """A 0.0 settlement (illiquid contract) with a valid bid/ask → falls back to
    the row mid (5.0), NO error; the other days keep their own close."""
    close_hole = dict(_CLOSE)
    close_hole[_DATES[1]] = 0.0
    v, e, _c = await _resolve("close", chains=_build_chains(close_hole))
    assert e[1] is None
    assert np.isclose(v[1], 5.0)  # the row mid, not NaN
    # Untouched days still read their own (valid) settlement close, not the mid.
    np.testing.assert_allclose([v[0], v[2]], [_CLOSE[_DATES[0]], _CLOSE[_DATES[2]]])


async def test_close_negative_falls_back_to_mid() -> None:
    close_hole = dict(_CLOSE)
    close_hole[_DATES[0]] = -1.0
    v, e, _c = await _resolve("close", chains=_build_chains(close_hole))
    assert e[0] is None
    assert np.isclose(v[0], 5.0)


async def test_close_null_falls_back_to_mid() -> None:
    """A NULL settlement (row.close is None) → falls back to the row mid."""
    close_hole = dict(_CLOSE)
    close_hole[_DATES[2]] = None
    v, e, _c = await _resolve("close", chains=_build_chains(close_hole))
    assert e[2] is None
    assert np.isclose(v[2], 5.0)


async def test_close_and_mid_both_missing_stays_missing() -> None:
    """False-zero settlement AND no bid/ask (mid None) → NaN + ``missing_close``
    (the em-dash is preserved when there is nothing to fall back to)."""
    chains: dict = {}
    for d in _DATES:
        close_val = 0.0 if d == _DATES[1] else _CLOSE[d]
        # mid=None ⇒ no bid/ask/mid on the row, so no fallback is possible.
        mid_val = None if d == _DATES[1] else 5.0
        chains[d] = [
            (_APR_P, _row(row_date=d, mid=mid_val, delta=-0.10, close=close_val)),
        ]
    v, e, _c = await _resolve("close", chains=chains)
    assert np.isnan(v[1])
    assert e[1] == "missing_close"
    assert not np.isnan(v[0]) and not np.isnan(v[2])


async def test_close_hold_mode_emits_held_close_and_roll_premium() -> None:
    """HOLD mode with stream='close': the held-premium LEVEL and the segment's
    roll_premium are the settlement CLOSE (single ByStrike segment, no roll)."""
    roll_info: dict = {}
    v, e, _c = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream="close",
        chain_reader=FakeChainReader(_build_chains()),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(_build_chains()),
        available_expirations=[_APR],
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
    )
    assert all(err is None for err in e), e
    np.testing.assert_allclose(v, [_CLOSE[d] for d in _DATES])
    is_roll = np.asarray(roll_info["is_roll"], dtype=bool)
    roll_premium = np.asarray(roll_info["roll_premium"], dtype=np.float64)
    assert bool(is_roll[0])
    np.testing.assert_allclose(roll_premium[0], _CLOSE[_DATES[0]])
    # All settlements are valid (>0) → NO fallback fired anywhere.
    assert np.all(np.asarray(roll_info["close_mid_fallback"]) == 0.0)
    assert np.all(np.asarray(roll_info["roll_premium_fallback"]) == 0.0)


async def test_close_hold_mode_marks_fallback_on_false_zero_open() -> None:
    """HOLD mode: a false-zero settlement on the segment OPEN date falls back to
    the row mid (5.0) in BOTH the value series and the roll-day open premium, and
    the per-date markers flag exactly that bar."""
    close_hole = dict(_CLOSE)
    close_hole[_DATES[0]] = 0.0  # the segment open (index 0) is a false zero
    roll_info: dict = {}
    v, e, _c = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=_MATURITY,
        selection=_SELECTION,
        stream="close",
        chain_reader=FakeChainReader(_build_chains(close_hole)),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(_build_chains(close_hole)),
        available_expirations=[_APR],
        hold_between_rolls=True,
        hold_roll_info_out=roll_info,
    )
    assert all(err is None for err in e), e
    # Index 0's value AND roll premium fell back to the mid (5.0); later bars keep
    # their own valid settlements.
    np.testing.assert_allclose(v, [5.0, _CLOSE[_DATES[1]], _CLOSE[_DATES[2]]])
    roll_premium = np.asarray(roll_info["roll_premium"], dtype=np.float64)
    np.testing.assert_allclose(roll_premium[0], 5.0)
    close_fb = np.asarray(roll_info["close_mid_fallback"])
    roll_fb = np.asarray(roll_info["roll_premium_fallback"])
    # Fallback flagged only at index 0 (the value series); the open premium too.
    np.testing.assert_allclose(close_fb, [1.0, 0.0, 0.0])
    np.testing.assert_allclose(roll_fb, [1.0, 0.0, 0.0])
