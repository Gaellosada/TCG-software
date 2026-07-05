"""Tests for the MONTHLY 3rd-Friday cycle-tag-set fix (root-cause of the
delta-selected option landing on a poorly-covered expiry in later years).

Live-confirmed root cause (team lead): the dwh tags the SAME monthly 3rd-Friday
contract ``"M"`` in early years but ``"W3 Friday"`` in later years, leaving
``"M"`` for QUARTERLIES only.  A selection filtered on ``"M"`` alone therefore
tracks the monthly in early years but silently falls back to sparse quarterlies
later → ``ByDelta(-0.10)`` lands on a deep-OTM garbage strike.

The fix: ``expand_cycle("M")`` → ``("M", "W3 Friday")`` (the full monthly
3rd-Friday series) applied on the option-STREAM selection path (signals +
option-stream series), NOT the raw chain browser.  Every other cycle is
unchanged.

Coverage:
  * ``test_expand_cycle_*`` — the pure domain helper.
  * ``test_cycle_predicate_*`` — the SQL WHERE-fragment builder (scalar stays
    byte-identical; a set becomes ``= ANY(%s)``).
  * ``test_monthly_series_reaches_w3_tagged_expiry`` — through the resolver with
    the EXPANDED cycle: the well-covered ~2mo 3rd-Friday is ``W3 Friday``-tagged
    and ``M`` is only a far quarterly → selection lands on the W3 monthly's 10Δ.
  * ``test_hold_mode_monthly_series`` — SAME, in HOLD mode (bs-mid/hold path,
    which S1 uses) — the tag-set fix is hold-compatible.
  * ``test_plain_M_alone_misses_the_monthly`` — control: WITHOUT expansion
    (cycle="M" scalar), the resolver sees only the far quarterly and picks its
    garbage — proving expansion is what fixes it.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from hypothesis import given
from hypothesis import strategies as st

from tcg.data._sql.options import _cycle_predicate
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    MONTHLY_CYCLE_TAGS,
    WEEKLY_CYCLE_TAGS,
    ByDelta,
    NearestToTarget,
    expand_cycle,
)

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row


# ---------------------------------------------------------------------------
# Pure domain helper
# ---------------------------------------------------------------------------


def test_expand_cycle_M_expands_to_series():
    assert expand_cycle("M") == MONTHLY_CYCLE_TAGS
    assert set(MONTHLY_CYCLE_TAGS) == {"M", "W3 Friday"}


def test_expand_cycle_W_expands_to_union():
    # 'W' is the generic-weekly UI choice.  Its ROBUST meaning is "all weeklies
    # across BOTH tagging conventions": crypto/VIX literal 'W' + the index-root
    # per-week 'W1/2/3/4 Friday' tags.  Each tag is a no-op for the other root
    # family, so this is safe for every root.
    assert expand_cycle("W") == WEEKLY_CYCLE_TAGS
    assert set(WEEKLY_CYCLE_TAGS) == {
        "W",
        "W1 Friday",
        "W2 Friday",
        "W3 Friday",
        "W4 Friday",
    }
    # The literal 'W' tag (crypto/VIX) is preserved in the union, so a crypto
    # weekly stream still matches its 'W'-tagged contracts unchanged.
    assert "W" in WEEKLY_CYCLE_TAGS


def test_expand_cycle_passes_through_others():
    assert expand_cycle(None) is None
    assert expand_cycle("W3 Friday") == "W3 Friday"
    assert expand_cycle("Q") == "Q"
    assert expand_cycle("W1 Friday") == "W1 Friday"
    assert expand_cycle("D") == "D"
    assert expand_cycle("") == ""


@given(
    st.sampled_from(
        [
            None,
            "",
            "M",
            "W",
            "Q",
            "D",
            "W1 Friday",
            "W2 Friday",
            "W3 Friday",
            "W4 Friday",
            "X",
            "weekly",
            "m",
        ]
    )
)
def test_expand_cycle_property(cycle):
    """Property: only 'M' and 'W' are broadened (to their exact tag unions);
    every other value — including None — is returned IDENTICALLY."""
    result = expand_cycle(cycle)
    if cycle == "M":
        assert result == MONTHLY_CYCLE_TAGS
    elif cycle == "W":
        assert result == WEEKLY_CYCLE_TAGS
    else:
        assert result == cycle
        assert result is cycle  # untouched pass-through, not a copy


# ---------------------------------------------------------------------------
# SQL WHERE-fragment builder — scalar byte-identical, set → ANY
# ---------------------------------------------------------------------------


def test_cycle_predicate_scalar_is_unchanged():
    frag, val = _cycle_predicate("M")
    assert frag == "expiration_cycle = %s"
    assert val == "M"


def test_cycle_predicate_none():
    assert _cycle_predicate(None) == (None, None)


def test_cycle_predicate_sequence_uses_any():
    frag, val = _cycle_predicate(("M", "W3 Friday"))
    assert frag == "expiration_cycle = ANY(%s)"
    assert val == ["M", "W3 Friday"]


def test_cycle_predicate_single_element_sequence_collapses_to_scalar():
    frag, val = _cycle_predicate(["M"])
    assert frag == "expiration_cycle = %s"
    assert val == "M"  # not a 1-element list → keeps the historical bind shape


def test_cycle_predicate_empty_sequence_no_filter():
    assert _cycle_predicate([]) == (None, None)


# ---------------------------------------------------------------------------
# Through the resolver: the monthly SERIES reaches the W3-tagged 3rd-Friday
# ---------------------------------------------------------------------------

# Later-era geometry: the well-covered ~2mo 3rd-Friday (Aug-18-2023) is tagged
# 'W3 Friday'; the only 'M'-tagged expiry near the target is a far quarterly
# (Sep-15-2023) whose listed strikes are deep-OTM garbage.
_TRADE = date(2023, 6, 15)
_W3_MONTHLY = date(2023, 8, 18)  # ~64 DTE, 'W3 Friday' — the real monthly
_M_QUARTERLY = date(2023, 9, 15)  # ~92 DTE, 'M' — quarterly, garbage strikes only


def _later_era_chain(d: date) -> list:
    # The good 10Δ put lives on the W3-tagged monthly.
    good = [
        (
            _contract(
                strike=4000.0, expiration=_W3_MONTHLY, type_="P", cycle="W3 Friday"
            ),
            _row(row_date=d, mid=40.0, iv=0.20, delta=-0.50),
        ),
        (
            _contract(
                strike=3500.0, expiration=_W3_MONTHLY, type_="P", cycle="W3 Friday"
            ),
            _row(row_date=d, mid=8.0, iv=0.28, delta=-0.10),  # the 10Δ target
        ),
    ]
    # The 'M' quarterly only lists a deep-OTM garbage strike (delta ~ -0.01).
    garbage_M = [
        (
            _contract(strike=800.0, expiration=_M_QUARTERLY, type_="P", cycle="M"),
            _row(row_date=d, mid=0.05, iv=0.40, delta=-0.012),
        )
    ]
    return good + garbage_M


async def _run(cycle_value, *, hold=False, stream="delta"):
    """Resolve with a given (already wiring-expanded) cycle value.

    ``available_expirations`` is filtered by the SAME cycle semantics the real
    ``list_expirations_filtered`` applies, so this exercises the end-to-end gate.
    """
    dates = [_TRADE]
    chains = {_TRADE: _later_era_chain(_TRADE)}
    all_exps = sorted(
        {
            c.expiration
            for (c, _r) in chains[_TRADE]
            if (
                cycle_value is None
                or (
                    c.expiration_cycle == cycle_value
                    if isinstance(cycle_value, str)
                    else c.expiration_cycle in tuple(cycle_value)
                )
            )
        }
    )
    roll_info: dict | None = {} if hold else None
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="P",
        cycle=cycle_value,
        maturity=NearestToTarget(target_dte_days=60),
        selection=ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream=stream,
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=all_exps,
        hold_between_rolls=hold,
        hold_roll_info_out=roll_info,
    )


async def test_monthly_series_reaches_w3_tagged_expiry():
    """With the EXPANDED monthly series ({'M','W3 Friday'}), the resolver sees the
    W3-tagged 3rd-Friday and selects its real 10Δ put — not the M quarterly's
    garbage."""
    values, errors, contracts = await _run(expand_cycle("M"))
    assert contracts[0] is not None, errors[0]
    assert contracts[0].expiration == _W3_MONTHLY
    assert contracts[0].expiration_cycle == "W3 Friday"
    assert abs(contracts[0].strike - 3500.0) < 1e-6
    assert abs(values[0] - (-0.10)) < 1e-6


async def test_plain_M_alone_misses_the_monthly():
    """Control: WITHOUT expansion (scalar 'M'), only the M quarterly is visible,
    so the resolver picks its deep-OTM garbage — the bug the fix removes."""
    values, errors, contracts = await _run("M")
    assert contracts[0] is not None
    assert contracts[0].expiration == _M_QUARTERLY
    assert contracts[0].expiration_cycle == "M"
    assert abs(contracts[0].strike - 800.0) < 1e-6  # garbage


async def test_hold_mode_monthly_series():
    """The tag-set fix is HOLD-MODE compatible (S1 uses hold_between_rolls=True +
    bs_mid).  In hold mode the per-segment selection also lands on the W3 monthly
    when the cycle is the expanded series."""
    # Use stream="mid" (hold mode emits the held-contract premium LEVEL).
    values, errors, contracts = await _run(expand_cycle("M"), hold=True, stream="mid")
    assert contracts[0] is not None, errors[0]
    assert contracts[0].expiration == _W3_MONTHLY
    assert contracts[0].expiration_cycle == "W3 Friday"
    assert abs(contracts[0].strike - 3500.0) < 1e-6
    # The held contract's mid LEVEL is surfaced (8.0), not NaN.
    assert np.isfinite(values[0]) and abs(values[0] - 8.0) < 1e-6
