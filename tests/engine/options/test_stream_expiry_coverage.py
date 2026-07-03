"""Reproduction + regression tests for the option EXPIRY-SELECTION coverage bug.

Mechanism (live-diagnosed on OPT_SP_500 = options on the E-MINI future):

  A ~2-month ``NearestToTarget`` + ``ByDelta(-0.10)`` put signal reproduces the
  ground-truth sim in early years (corr 0.94-0.98) but COLLAPSES/INVERTS in later
  years (corr ~0 to -0.5).  Root cause: the engine's EXPIRY selection lands on a
  poorly-covered expiry in later years, so ``ByDelta(-0.10)`` picks a deep-OTM
  garbage strike (moneyness ~0.17-0.30) instead of a true 10-delta put
  (moneyness ~0.88).

  The upstream cause is that ``available_expirations`` is filtered by
  ``expiration_cycle = cycle`` (``list_expirations_filtered``).  When a caller
  selects ``cycle='M'`` and the M-cycle has a COVERAGE GAP around the ~2mo target
  in later years, ``NearestToTarget`` snaps to the nearest LISTED M expiry — a far
  contract whose only greeked strikes are deep-OTM garbage — even though a proper
  10-delta put exists at the target DTE in a DIFFERENT cycle (which the cycle
  filter excludes entirely).

These tests use synthetic chains that encode exactly that structure so the fix can
be verified WITHOUT the dwh:

  * ``test_coverage_aware_expiry_skips_gappy_target`` — the primary regression:
    an expiry at the exact target DTE exists but has NO delta-bearing strikes near
    the target; a well-covered neighbor exists a bit further out.  The resolver
    must select the WELL-COVERED expiry's true 10-delta strike, not the gappy
    target's garbage.
  * ``test_single_good_expiry_unchanged`` — early-era control: one well-covered
    expiry at the target → selected unchanged (no behaviour drift).
  * ``test_all_cycles_sees_good_expiry`` — with ``cycle=None`` (all cycles) the
    proper 10-delta put in the neighbouring cycle is reachable, proving the cycle
    filter is the exclusion knob.

Harness: the shared ``_stream_fakes`` bulk reader filters each date's chain by
type / ``expiration_min<=exp<=expiration_max`` / cycle — exactly the production
gate.  ``ByDelta`` here uses the DEFAULT non-strict match (closest delta wins),
which is what makes the bug visible: with a gappy expiry the "closest" delta is a
garbage deep-OTM strike.
"""

from __future__ import annotations

from datetime import date

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import ByDelta, NearestToTarget

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row


# ── Chain geometry ─────────────────────────────────────────────────────────
#
# ref/trade date and two candidate expirations for a ~2-month (60 DTE) target:
#   * TARGET_EXP  — at ~60 DTE, the arithmetic "nearest" pick, but its only
#                   listed strikes are deep-OTM garbage (delta ~ -0.01), i.e. NO
#                   strike anywhere near the -0.10 target.  This models the
#                   later-year M-cycle coverage gap.
#   * GOOD_EXP    — a bit further out (~75 DTE) but WELL COVERED: it has a real
#                   -0.10 delta put at moneyness ~0.88.
#
# Spot ~ 4500.  A 10-delta 2mo put sits around K/S ~ 0.88 → K ~ 3960.
_TRADE = date(2023, 6, 15)
_TARGET_EXP = date(2023, 8, 14)  # 60 DTE — the gappy target month
_GOOD_EXP = date(2023, 8, 29)  # 75 DTE — well-covered neighbour
_SPOT = 4500.0


def _garbage_target_chain(d: date, cycle: str) -> list:
    """The gappy target-month chain: only a deep-OTM put (delta ~ -0.012).

    No strike anywhere near the -0.10 target — so a delta match here is garbage
    (moneyness ~0.17).  Models the later-year M-cycle coverage hole.
    """
    k_garbage = _contract(
        strike=750.0, expiration=_TARGET_EXP, type_="P", cycle=cycle
    )  # K/S ~ 0.167
    return [(k_garbage, _row(row_date=d, mid=0.05, iv=0.40, delta=-0.012))]


def _good_chain(d: date, cycle: str) -> list:
    """A well-covered chain with a real -0.10 delta put at moneyness ~0.88."""
    # A small ladder so match_by_delta has a genuine nearest pick.
    k_atm = _contract(strike=4500.0, expiration=_GOOD_EXP, type_="P", cycle=cycle)
    k_10d = _contract(strike=3960.0, expiration=_GOOD_EXP, type_="P", cycle=cycle)
    k_5d = _contract(strike=3600.0, expiration=_GOOD_EXP, type_="P", cycle=cycle)
    return [
        (k_atm, _row(row_date=d, mid=40.0, iv=0.20, delta=-0.50)),
        (k_10d, _row(row_date=d, mid=8.0, iv=0.28, delta=-0.10)),  # the target
        (k_5d, _row(row_date=d, mid=3.0, iv=0.33, delta=-0.05)),
    ]


async def _resolve(dates, *, cycle, available_expirations, chains, coverage_aware=True):
    return await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="P",
        cycle=cycle,
        maturity=NearestToTarget(target_dte_days=60),
        selection=ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream="delta",  # read delta back so the assertion is on the SELECTED strike
        chain_reader=FakeChainReader(chains),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader(chains),
        available_expirations=available_expirations,
        coverage_aware=coverage_aware,
    )


# ---------------------------------------------------------------------------
# Primary regression: coverage-aware expiry must skip the gappy target
# ---------------------------------------------------------------------------


async def test_coverage_aware_expiry_skips_gappy_target():
    """Both expirations listed under the SAME cycle; the nearest-DTE one is gappy
    (only a deep-OTM garbage strike) and the slightly-further one is well covered.

    A coverage-BLIND resolver picks _TARGET_EXP (nearest DTE) and its garbage
    strike (delta ~ -0.012, moneyness 0.167).  The FIX must instead land on
    _GOOD_EXP's real -0.10 delta put.
    """
    dates = [_TRADE]
    cycle = "M"
    chains = {_TRADE: _garbage_target_chain(_TRADE, cycle) + _good_chain(_TRADE, cycle)}
    values, errors, contracts = await _resolve(
        dates,
        cycle=cycle,
        available_expirations=[_TARGET_EXP, _GOOD_EXP],
        chains=chains,
    )
    assert contracts[0] is not None, errors[0]
    # The SELECTED contract must be the well-covered expiry's 10-delta strike,
    # NOT the gappy target's deep-OTM garbage.
    assert contracts[0].expiration == _GOOD_EXP, (
        f"selected expiry {contracts[0].expiration} (strike {contracts[0].strike}) "
        f"— expected the well-covered {_GOOD_EXP}"
    )
    assert abs(contracts[0].strike - 3960.0) < 1e-6
    assert abs(values[0] - (-0.10)) < 1e-6  # the delta stream of the 10-delta put
    # A strictly-nearer (gappy) expiry was skipped → a traceable success-side note.
    assert errors[0] == f"coverage_skipped:{_TARGET_EXP.isoformat()}"


async def test_coverage_off_is_unchanged_and_picks_garbage():
    """Default OFF (``coverage_aware=False``): the SAME gappy geometry still picks
    the nearest-DTE expiry's deep-OTM garbage — proving the fix is opt-in and does
    not alter existing behaviour (golden-preserving)."""
    dates = [_TRADE]
    cycle = "M"
    chains = {_TRADE: _garbage_target_chain(_TRADE, cycle) + _good_chain(_TRADE, cycle)}
    values, errors, contracts = await _resolve(
        dates,
        cycle=cycle,
        available_expirations=[_TARGET_EXP, _GOOD_EXP],
        chains=chains,
        coverage_aware=False,
    )
    # Coverage-blind: nearest-DTE expiry (gappy target) + its garbage strike.
    assert contracts[0] is not None
    assert contracts[0].expiration == _TARGET_EXP
    assert abs(contracts[0].strike - 750.0) < 1e-6


# ---------------------------------------------------------------------------
# Control: a single well-covered expiry at target is unchanged (no drift)
# ---------------------------------------------------------------------------


async def test_single_good_expiry_unchanged():
    """Early-era shape: only ONE well-covered expiry at the target DTE.  The
    resolver selects its 10-delta put exactly as before — the fix must not perturb
    the common good-coverage case."""
    dates = [_TRADE]
    cycle = "M"
    good_at_target = [
        (
            _contract(strike=4500.0, expiration=_TARGET_EXP, type_="P", cycle=cycle),
            _row(row_date=_TRADE, mid=40.0, iv=0.20, delta=-0.50),
        ),
        (
            _contract(strike=3960.0, expiration=_TARGET_EXP, type_="P", cycle=cycle),
            _row(row_date=_TRADE, mid=8.0, iv=0.28, delta=-0.10),
        ),
    ]
    chains = {_TRADE: good_at_target}
    values, errors, contracts = await _resolve(
        dates, cycle=cycle, available_expirations=[_TARGET_EXP], chains=chains
    )
    assert contracts[0] is not None, errors[0]
    assert contracts[0].expiration == _TARGET_EXP
    assert abs(contracts[0].strike - 3960.0) < 1e-6
    assert abs(values[0] - (-0.10)) < 1e-6


# ---------------------------------------------------------------------------
# The cycle filter is the exclusion knob (all-cycles reaches the good expiry)
# ---------------------------------------------------------------------------


async def test_all_cycles_sees_good_expiry():
    """When the good 10-delta put lives in a DIFFERENT cycle, ``cycle=None`` (all
    cycles) reaches it; ``cycle='M'`` (M-only) would exclude it.  This pins the
    exclusion mechanism the live diagnosis identified.

    Here the gappy target is cycle 'M' and the well-covered neighbour is cycle
    'W3 Friday'.  With ``cycle=None`` both are visible and the resolver (with the
    coverage-aware fix) lands on the good 10-delta put.
    """
    dates = [_TRADE]
    chains = {
        _TRADE: _garbage_target_chain(_TRADE, "M") + _good_chain(_TRADE, "W3 Friday")
    }
    values, errors, contracts = await _resolve(
        dates,
        cycle=None,
        available_expirations=[_TARGET_EXP, _GOOD_EXP],
        chains=chains,
    )
    assert contracts[0] is not None, errors[0]
    assert contracts[0].expiration == _GOOD_EXP
    assert contracts[0].expiration_cycle == "W3 Friday"
    assert abs(values[0] - (-0.10)) < 1e-6


# ---------------------------------------------------------------------------
# Fallback: no covered candidate → nearest-DTE best-effort (never all-NaN)
# ---------------------------------------------------------------------------


async def test_no_covered_candidate_falls_back_to_nearest_best_effort():
    """When NONE of the candidate expiries has an in-tolerance delta strike, the
    coverage-aware path must NOT return all-NaN — it degrades to the nearest-DTE
    best-effort match (== the coverage-blind result).  Here BOTH expiries are
    gappy (only deep-OTM garbage); the resolver falls back to the nearest-DTE one
    (the target) and picks its garbage, exactly as coverage-off would."""
    dates = [_TRADE]
    cycle = "M"
    garbage_near = _garbage_target_chain(_TRADE, cycle)  # target exp, delta -0.012
    garbage_far = [
        (
            _contract(strike=800.0, expiration=_GOOD_EXP, type_="P", cycle=cycle),
            _row(row_date=_TRADE, mid=0.06, iv=0.40, delta=-0.013),
        )
    ]
    chains = {_TRADE: garbage_near + garbage_far}
    values, errors, contracts = await _resolve(
        dates,
        cycle=cycle,
        available_expirations=[_TARGET_EXP, _GOOD_EXP],
        chains=chains,
    )
    # Fallback picked the nearest-DTE (target) expiry — not None, not the far one.
    assert contracts[0] is not None
    assert contracts[0].expiration == _TARGET_EXP
    assert abs(contracts[0].strike - 750.0) < 1e-6
    # No coverage_skipped note — no strictly-nearer covered expiry was skipped.
    assert errors[0] != f"coverage_skipped:{_TARGET_EXP.isoformat()}"


# ---------------------------------------------------------------------------
# Rolling: the candidate list is rebuilt per date (the roll advances)
# ---------------------------------------------------------------------------


async def test_candidate_list_advances_across_dates():
    """Across two trade dates a month apart, the nearest-DTE candidate window
    advances, so the covered expiry each date is resolved relative to THAT date —
    proving the candidate list is per-date, not frozen from the first date."""
    d0 = date(2023, 6, 15)
    d1 = date(2023, 7, 17)  # ~1 month later
    # Two expiry pairs; on d0 the good ~2mo expiry is E0_GOOD, on d1 it is E1_GOOD.
    e0_gap = date(2023, 8, 14)  # ~60 DTE from d0 (gappy)
    e0_good = date(2023, 8, 29)  # ~75 DTE from d0 (covered)
    e1_gap = date(2023, 9, 15)  # ~60 DTE from d1 (gappy)
    e1_good = date(2023, 9, 29)  # ~74 DTE from d1 (covered)

    def _gap(exp, d):
        return [
            (
                _contract(strike=750.0, expiration=exp, type_="P", cycle="M"),
                _row(row_date=d, mid=0.05, iv=0.4, delta=-0.012),
            )
        ]

    def _good(exp, d):
        return [
            (
                _contract(strike=3960.0, expiration=exp, type_="P", cycle="M"),
                _row(row_date=d, mid=8.0, iv=0.28, delta=-0.10),
            )
        ]

    chains = {
        d0: _gap(e0_gap, d0)
        + _good(e0_good, d0)
        + _gap(e1_gap, d0)
        + _good(e1_good, d0),
        d1: _gap(e0_gap, d1)
        + _good(e0_good, d1)
        + _gap(e1_gap, d1)
        + _good(e1_good, d1),
    }
    values, errors, contracts = await _resolve(
        [d0, d1],
        cycle="M",
        available_expirations=[e0_gap, e0_good, e1_gap, e1_good],
        chains=chains,
    )
    assert contracts[0] is not None and contracts[1] is not None, errors
    # d0 resolves to e0_good; d1 rolls forward to e1_good (per-date candidate set).
    assert contracts[0].expiration == e0_good
    assert contracts[1].expiration == e1_good
    assert abs(values[0] - (-0.10)) < 1e-6
    assert abs(values[1] - (-0.10)) < 1e-6
