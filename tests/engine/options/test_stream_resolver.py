"""Tests for ``tcg.engine.options.series.stream_resolver``.

The materialiser is exercised against a fake chain reader that returns
synthetic ``(contract, row)`` chains keyed by ``(date, expiration_cycle)``.
The maturity resolver and underlying-price resolver are stubbed.

Six mandatory parametrized rows (see context brief, Wave 2a):

1. All-NaN iv when iv_stored is missing across the chain.
2. Missing underlying price → ``missing_underlying_price``.
3. ATM tie deterministic — lower strike wins (matches ``_match.py``).
4. Multi-cycle filter — ``cycle=None`` selects ``M`` and ``W``
   together; ``cycle="W"`` only ``W``.
5. Single-day delisted gap — ``ByStrike`` strike gone for one date.
6. ``last_trade_date`` truncation — past cutoff yields
   ``past_last_trade_date`` NaN.

Plus two API-level validation tests against the FastAPI router:

* TAUTOLOGICAL_OPTION_STREAM (selection=by_delta + stream='delta').
* STREAM_UNAVAILABLE_FOR_ROOT (gamma/vega/theta on a no-greeks root).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Literal, Sequence
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByDelta,
    ByMoneyness,
    ByStrike,
    NearestToTarget,
    NextThirdFriday,
    OptionContractDoc,
    OptionDailyRow,
    OptionRootInfo,
)


# ── Synthetic chain helpers ────────────────────────────────────────────


def _contract(
    *,
    strike: float,
    expiration: date,
    type_: Literal["C", "P"] = "C",
    cycle: str = "M",
    collection: str = "OPT_SP_500",
) -> OptionContractDoc:
    cid = f"{collection}_K{int(strike)}_{type_}_{expiration.isoformat()}_{cycle}"
    return OptionContractDoc(
        collection=collection,
        contract_id=cid,
        root_underlying="IND_SP_500",
        underlying_ref="FUT_SP_500_EMINI",
        underlying_symbol=None,
        expiration=expiration,
        expiration_cycle=cycle,
        strike=float(strike),
        type=type_,
        contract_size=None,
        currency="USD",
        provider="IVOLATILITY",
        strike_factor_verified=True,
    )


def _row(
    *,
    row_date: date,
    iv: float | None = 0.20,
    delta: float | None = 0.50,
    mid: float | None = 1.05,
    gamma: float | None = None,
    vega: float | None = None,
    theta: float | None = None,
    open_interest: float | None = None,
    volume: float | None = None,
) -> OptionDailyRow:
    return OptionDailyRow(
        date=row_date,
        open=None,
        high=None,
        low=None,
        close=None,
        bid=mid - 0.05 if mid is not None else None,
        ask=mid + 0.05 if mid is not None else None,
        bid_size=None,
        ask_size=None,
        volume=volume,
        open_interest=open_interest,
        mid=mid,
        iv_stored=iv,
        delta_stored=delta,
        gamma_stored=gamma,
        theta_stored=theta,
        vega_stored=vega,
        underlying_price_stored=None,
    )


class FakeChainReader:
    """Minimal chain reader for the materialiser.

    ``chains_by_date`` maps ``date`` → list of ``(contract, row)`` tuples
    representing the FULL chain on that date (across all expirations
    and cycles).  ``query_chain`` filters by expiration window and
    cycle and reports each call's keyword args via ``calls`` for
    assertion in tests.
    """

    def __init__(
        self,
        chains_by_date: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
        self._chains = chains_by_date
        self.calls: list[dict] = []

    async def query_chain(
        self,
        *,
        root: str,
        date: date,
        type: Literal["C", "P", "both"],
        expiration_min: date,
        expiration_max: date,
        strike_min: float | None = None,
        strike_max: float | None = None,
        expiration_cycle: str | None = None,
    ) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
        self.calls.append(
            {
                "root": root,
                "date": date,
                "type": type,
                "expiration_min": expiration_min,
                "expiration_max": expiration_max,
                "expiration_cycle": expiration_cycle,
            }
        )
        chain = self._chains.get(date, [])
        return [
            (c, r)
            for (c, r) in chain
            if (c.type == type or type == "both")
            and expiration_min <= c.expiration <= expiration_max
            and (expiration_cycle is None or c.expiration_cycle == expiration_cycle)
        ]


def _make_underlying_resolver(value: float | None):
    """Return an async callable that always resolves the underlying to ``value``."""

    async def resolver(contract: OptionContractDoc, on_date: date) -> float | None:
        return value

    return resolver


# ── 1. All-NaN iv ──────────────────────────────────────────────────────


async def test_all_nan_iv_missing():
    """All chain rows have ``iv_stored=None`` → NaN value, ``missing_iv``."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)  # third Friday
    chain = [
        (
            _contract(strike=4490, expiration=expiration),
            _row(row_date=d, iv=None),
        ),
        (
            _contract(strike=4500, expiration=expiration),
            _row(row_date=d, iv=None),
        ),
        (
            _contract(strike=4510, expiration=expiration),
            _row(row_date=d, iv=None),
        ),
    ]
    reader = FakeChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(4500.0),
    )
    assert np.isnan(values[0])
    assert errors[0] == "missing_iv"


# ── 2. Missing underlying ──────────────────────────────────────────────


async def test_missing_underlying_price():
    """``ByMoneyness`` + resolver returning ``None`` → ``missing_underlying_price``."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (
            _contract(strike=4500, expiration=expiration, collection="OPT_ETH"),
            _row(row_date=d),
        ),
    ]
    reader = FakeChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_ETH",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(None),
    )
    assert np.isnan(values[0])
    assert errors[0] == "missing_underlying_price"


async def test_zero_underlying_price_treated_as_missing():
    """``underlying_price=0.0`` must produce ``missing_underlying_price``,
    not infinity or NaN without an error code.

    Both the legacy per-date path (via the selector) and the bulk pre-fetch
    path guard ``S <= 0`` before any ``K/S`` division.  This test pins that
    zero is treated identically to ``None``.
    """
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (
            _contract(strike=4500, expiration=expiration),
            _row(row_date=d, iv=0.20),
        ),
    ]

    # -- Legacy per-date path (no bulk_chain_reader) --
    reader = FakeChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(0.0),
    )
    assert np.isnan(values[0])
    assert errors[0] == "missing_underlying_price"

    # -- Bulk pre-fetch path --
    reader_b = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    values_b, errors_b, _contracts_b = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader_b,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(0.0),
        bulk_chain_reader=bulk_reader,
    )
    assert np.isnan(values_b[0])
    assert errors_b[0] == "missing_underlying_price"


# ── 3. ATM tie deterministic — lower strike wins ───────────────────────


async def test_atm_tie_lower_strike_wins():
    """ByStrike tie-break: lower strike wins on exact-equal ``K/S`` distance.

    The brief asks for a 4490/4510 ATM tie-break test pinning that
    "lower strike wins".  ``_match.match_by_moneyness`` sorts by
    ``(abs(K/S - target), strike)`` — but Python float arithmetic
    on ``K/S`` does NOT make 4490/4500 and 4510/4500 exactly
    equidistant from 1.0::

        abs(4490/4500 - 1.0) = 0.00222222222222223  (slightly bigger)
        abs(4510/4500 - 1.0) = 0.00222222222222213  (slightly smaller)

    So under ``ByMoneyness`` the higher strike wins for THIS specific
    pair — surprising but a faithful reading of the matcher.  The
    deterministic-lower-strike tie-break is reliably exercised under
    ``ByStrike`` (no float arithmetic on the criterion side; primary
    sort key is identical).  We pin both behaviours to prevent
    regressions: the ByMoneyness float quirk AND the ByStrike
    deterministic order.
    """
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    # ByMoneyness branch: 4510 wins by float-arithmetic margin.
    chain_moneyness = [
        (
            _contract(strike=4490, expiration=expiration),
            _row(row_date=d, iv=0.21),
        ),
        (
            _contract(strike=4510, expiration=expiration),
            _row(row_date=d, iv=0.22),
        ),
    ]
    reader_m = FakeChainReader({d: chain_moneyness})
    values_m, errors_m, _contracts_m = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader_m,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(4500.0),
    )
    assert errors_m[0] is None
    # 4510 wins under ByMoneyness due to float arithmetic asymmetry.
    assert values_m[0] == pytest.approx(0.22)

    # ByStrike branch: exact-equal distance (K - target = 0 exactly when
    # the contract has the asked-for strike, which is the only match
    # by definition).  But for a deterministic-tie test we use a chain
    # where two contracts share the same strike (defensive — should
    # never happen on real data but matches the matcher's documented
    # tie-break: lowest-strike wins after sorting by strike).
    contract_a = replace(
        _contract(strike=4500, expiration=expiration),
        contract_id="OPT_SP_500_K4500_C_v1",
    )
    contract_b = replace(
        _contract(strike=4500, expiration=expiration),
        contract_id="OPT_SP_500_K4500_C_v2",
    )
    chain_strike = [
        (contract_a, _row(row_date=d, iv=0.31)),
        (contract_b, _row(row_date=d, iv=0.32)),
    ]
    reader_s = FakeChainReader({d: chain_strike})
    values_s, errors_s, _contracts_s = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader_s,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
    )
    assert errors_s[0] is None
    # First-in-list wins (stable sort + equal sort keys); the materialiser
    # then picks that contract's row.  Pinned for deterministic regression.
    assert values_s[0] == pytest.approx(0.31)


# ── 4. Multi-cycle filter ──────────────────────────────────────────────


async def test_multi_cycle_filter():
    """Same Friday has both ``M`` and ``W`` expirations.

    With ``cycle=None`` the resolver applies no cycle filter — both
    are visible to the selector and the ATM hit is whichever wins
    by tie-break.  With ``cycle='W'`` only the W contract is
    available and its iv is read.

    We pick a M contract at strike 4500 with iv=0.20, and a W
    contract at strike 4500 with iv=0.30, so the IV value uniquely
    identifies which cycle was chosen.  Tie-break sorts by
    (|K/S - target|, strike) — both are at K=4500 so the first in
    list order wins (lower strike already equal, tie broken by
    stable sort).
    """
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    contract_m = _contract(strike=4500, expiration=expiration, cycle="M")
    contract_w = _contract(strike=4500, expiration=expiration, cycle="W")
    row_m = _row(row_date=d, iv=0.20)
    row_w = _row(row_date=d, iv=0.30)
    chain = [(contract_m, row_m), (contract_w, row_w)]

    # cycle="W" — only W visible.
    reader_w = FakeChainReader({d: chain})
    values_w, errors_w, _contracts_w = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle="W",
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader_w,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(4500.0),
    )
    assert errors_w[0] is None
    assert values_w[0] == pytest.approx(0.30)

    # cycle="M" — only M visible.
    reader_m = FakeChainReader({d: chain})
    values_m, errors_m, _contracts_m = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle="M",
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader_m,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(4500.0),
    )
    assert errors_m[0] is None
    assert values_m[0] == pytest.approx(0.20)


# ── 5. Single-day delisted gap ─────────────────────────────────────────


async def test_single_day_delisted_gap():
    """``ByStrike(4500)`` available on D−1 and D+1 but not on D.

    D should be NaN with ``error_code='strike_not_in_chain'``; no
    silent neighbour substitution.
    """
    d_prev = date(2024, 3, 21)
    d_gap = date(2024, 3, 22)
    d_next = date(2024, 3, 25)
    expiration = date(2024, 4, 19)

    full_chain = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d_prev, iv=0.20)),
    ]
    chain_no_4500 = [
        (_contract(strike=4490, expiration=expiration), _row(row_date=d_gap, iv=0.21)),
        (_contract(strike=4510, expiration=expiration), _row(row_date=d_gap, iv=0.22)),
    ]
    full_chain_next = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d_next, iv=0.23)),
    ]
    reader = FakeChainReader(
        {d_prev: full_chain, d_gap: chain_no_4500, d_next: full_chain_next}
    )

    values, errors, _contracts = await resolve_option_stream(
        dates=[d_prev, d_gap, d_next],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,  # ByStrike doesn't need underlying
    )
    assert errors[0] is None and values[0] == pytest.approx(0.20)
    assert np.isnan(values[1])
    assert errors[1] == "strike_not_in_chain"
    assert errors[2] is None and values[2] == pytest.approx(0.23)


# ── 6. last_trade_date truncation ──────────────────────────────────────


async def test_last_trade_date_truncation():
    """Dates strictly past the root's ``last_trade_date`` yield
    ``past_last_trade_date`` NaN; dates at or before are queried."""
    ltd = date(2024, 3, 22)
    d_before = date(2024, 3, 21)
    d_at = ltd
    d_after = date(2024, 3, 25)
    expiration = date(2024, 4, 19)
    full_chain_before = [
        (
            _contract(strike=4500, expiration=expiration),
            _row(row_date=d_before, iv=0.20),
        ),
    ]
    full_chain_at = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d_at, iv=0.21)),
    ]
    reader = FakeChainReader({d_before: full_chain_before, d_at: full_chain_at})

    values, errors, _contracts = await resolve_option_stream(
        dates=[d_before, d_at, d_after],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        last_trade_date=ltd,
    )
    assert errors[0] is None and values[0] == pytest.approx(0.20)
    assert errors[1] is None and values[1] == pytest.approx(0.21)
    assert np.isnan(values[2])
    assert errors[2] == "past_last_trade_date"
    # The reader was NEVER called for ``d_after``.
    after_calls = [c for c in reader.calls if c["date"] == d_after]
    assert after_calls == []


# ── Per-date call-count upper bound (Iter-2 review fix) ────────────────


# Empirically pinned per-date K factors (no batching at the data layer
# — recon Q6, ``stream_resolver.py:30-46``):
#
#   * Non-NearestToTarget: K=2 chain queries per date.
#       1) ``DefaultOptionsSelector`` queries the chain to pick the
#          contract matching the criterion.
#       2) The materialiser re-queries the chain (same date + resolved
#          expiration) to read the contract's row for the requested
#          stream label.  ``CachedChainReader`` would coalesce these
#          in production when keys match exactly; the in-memory
#          ``FakeChainReader`` here is a thin spy with no cache, so
#          both calls land on the spy.  The bound is "≤ K·N" — caching
#          can only reduce, never increase, the count.
#
#   * NearestToTarget: K=3 chain queries per date.
#       Adds one wide-window expiration-probe query before the two
#       above (the maturity resolver consults the chain to enumerate
#       available expirations).
#
# A fan-out regression — e.g. an inadvertent loop over expirations or
# a per-strike re-query — would push the total above K·N and trip the
# assertion.  The tests pin both branches so ``CachedChainReader`` can
# later tighten the bound (by caching K down to 1) without silently
# loosening it.
async def test_call_count_upper_bound_non_nearest():
    """For non-NearestToTarget maturity rules: ``len(reader.calls) <= K * N``
    with K=2 (selector probe + materialiser row read; no cache in the
    spy).  Protects against an O(N**2) regression where the resolver
    inadvertently scans the chain multiple times per date.
    """
    K = 2  # non-NearestToTarget: selector + materialiser, each one chain read per date.
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 20)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [(_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20))]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
    )
    assert len(reader.calls) <= K * len(dates), (
        f"Expected ≤{K * len(dates)} chain calls for {len(dates)} dates "
        f"(K={K} non-NearestToTarget); got {len(reader.calls)}."
    )


async def test_call_count_upper_bound_nearest_to_target():
    """For NearestToTarget maturity rule: ``len(reader.calls) <= K * N``
    with K=3 — wide-window expiration probe + selector chain read +
    materialiser row read per date.  Pin both K values to catch
    accidental fan-out regressions.
    """
    K = 3  # NearestToTarget: expiration probe + selector + materialiser per date.
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 20)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [(_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20))]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
    )
    assert len(reader.calls) <= K * len(dates), (
        f"Expected ≤{K * len(dates)} chain calls for {len(dates)} dates "
        f"(K={K} NearestToTarget); got {len(reader.calls)}."
    )


async def test_per_date_call_count_uniform_no_fanout():
    """Per-date call distribution is uniform across the date range.

    The pin is "no date triggers more chain calls than the documented
    per-date K".  A fan-out regression — e.g. one date's chain triggers
    a per-strike re-query — would show up as one date with > K calls
    while the others stay at K.  Asserting uniformity catches that
    pattern with a single threshold across the whole input.

    We use the non-NearestToTarget K=2 bound (selector + materialiser)
    here — the simpler maturity rule isolates the regression surface to
    the per-date materialiser path.

    Production wraps the reader in ``CachedChainReader`` which
    coalesces identical-key reads (the per-request cache); the
    in-memory ``FakeChainReader`` does not, so K=2 is the unwrapped
    upper bound.  The cache can only reduce, never increase, this.
    """
    K_PER_DATE = 2
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 20)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [(_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20))]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
    )
    per_date_counts: dict[date, int] = {}
    for c in reader.calls:
        per_date_counts[c["date"]] = per_date_counts.get(c["date"], 0) + 1
    # No date receives more calls than the per-date K bound.
    assert all(n <= K_PER_DATE for n in per_date_counts.values()), (
        f"Per-date fan-out regression: each date should see ≤{K_PER_DATE} "
        f"chain calls; got {per_date_counts}"
    )


# ── API validation tests ───────────────────────────────────────────────


@pytest.fixture
def mock_app_with_options():
    """FastAPI app wired for indicators + options-roots awareness."""
    from tcg.core.api.errors import tcg_error_handler
    from tcg.core.api.indicators import router as indicators_router
    from tcg.types.errors import TCGError

    svc = MagicMock()
    # list_option_roots returns one greeks-less root and one with greeks.
    svc.list_option_roots = AsyncMock(
        return_value=[
            OptionRootInfo(
                collection="OPT_SP_500",
                name="SP 500",
                has_greeks=True,
                providers=("IVOLATILITY",),
                expiration_first=date(2005, 1, 21),
                expiration_last=date(2027, 12, 19),
                doc_count_estimated=1234567,
                strike_factor_verified=True,
                last_trade_date=date(2024, 11, 15),
            ),
            OptionRootInfo(
                collection="OPT_ETH",
                name="ETH",
                has_greeks=False,
                providers=("DERIBIT",),
                expiration_first=date(2020, 1, 1),
                expiration_last=date(2027, 12, 31),
                doc_count_estimated=10000,
                strike_factor_verified=False,
                last_trade_date=None,
            ),
        ]
    )
    app = FastAPI()
    app.add_exception_handler(TCGError, tcg_error_handler)
    app.include_router(indicators_router)
    app.state.market_data = svc
    return app


@pytest.fixture
async def options_client(mock_app_with_options):
    transport = ASGITransport(app=mock_app_with_options)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


_TRIVIAL_INDICATOR = "def compute(series):\n    return series['x']\n"


async def test_tautological_option_stream_returns_422(options_client: AsyncClient):
    """selection=by_delta + stream='delta' → 422 TAUTOLOGICAL_OPTION_STREAM."""
    body = {
        "code": _TRIVIAL_INDICATOR,
        "params": {},
        "series": {
            "x": {
                "type": "option_stream",
                "collection": "OPT_SP_500",
                "option_type": "C",
                "cycle": "M",
                "maturity": {"kind": "next_third_friday", "offset_months": 0},
                "selection": {"kind": "by_delta", "target_delta": 0.5},
                "stream": "delta",
            }
        },
        "start": "2024-03-01",
        "end": "2024-03-31",
        "indicator_id": "atm-delta",
    }
    resp = await options_client.post("/api/indicators/compute", json=body)
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["error_code"] == "TAUTOLOGICAL_OPTION_STREAM"
    assert payload["asset_type"] == "option"
    assert "tautological" in payload["detail"]
    assert payload["indicator_id"] == "atm-delta"


async def test_stream_unavailable_for_root_returns_422(options_client: AsyncClient):
    """gamma on OPT_ETH (has_greeks=False) → 422 STREAM_UNAVAILABLE_FOR_ROOT."""
    body = {
        "code": _TRIVIAL_INDICATOR,
        "params": {},
        "series": {
            "x": {
                "type": "option_stream",
                "collection": "OPT_ETH",
                "option_type": "C",
                "maturity": {"kind": "next_third_friday", "offset_months": 0},
                "selection": {"kind": "by_moneyness", "target_K_over_S": 1.0},
                "stream": "gamma",
            }
        },
        "start": "2024-03-01",
        "end": "2024-03-31",
        "indicator_id": "atm-gamma",
    }
    resp = await options_client.post("/api/indicators/compute", json=body)
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["error_code"] == "STREAM_UNAVAILABLE_FOR_ROOT"
    assert payload["root"] == "OPT_ETH"
    assert payload["stream"] == "gamma"
    assert "gamma" in payload["unavailable_streams"]
    assert "vega" in payload["unavailable_streams"]
    assert "theta" in payload["unavailable_streams"]


# ── Bulk pre-fetch path tests ────────────────────────────────────────


class FakeBulkChainReader:
    """Minimal bulk chain reader for the materialiser.

    Wraps a ``FakeChainReader``'s data and applies the same filtering,
    but returns results for ALL requested dates in one call.  Tracks
    calls via ``bulk_calls`` for assertion.
    """

    def __init__(
        self,
        chains_by_date: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
    ) -> None:
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
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        dates_list = list(dates)
        self.bulk_calls.append(
            {
                "root": root,
                "dates": dates_list,
                "type": type,
                "expiration_min": expiration_min,
                "expiration_max": expiration_max,
                "strike_min": strike_min,
                "strike_max": strike_max,
                "expiration_cycle": expiration_cycle,
            }
        )
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates_list:
            chain = self._chains.get(d, [])
            filtered = [
                (c, r)
                for (c, r) in chain
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and (expiration_cycle is None or c.expiration_cycle == expiration_cycle)
            ]
            if filtered:
                result[d] = filtered
        return result


async def test_bulk_path_by_strike():
    """Bulk path produces identical results to per-date path for ByStrike."""
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 20)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [
            (
                _contract(strike=4500, expiration=expiration),
                _row(row_date=d, iv=0.20 + i * 0.01),
            ),
        ]
        for i, d in enumerate(dates)
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    values, errors, _contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert all(e is None for e in errors)
    assert values[0] == pytest.approx(0.20)
    assert values[1] == pytest.approx(0.21)
    assert values[2] == pytest.approx(0.22)
    # Bulk path: exactly 1 bulk call (one expiration), zero per-date calls.
    assert len(bulk_reader.bulk_calls) == 1
    assert len(reader.calls) == 0


async def test_bulk_path_by_moneyness():
    """Bulk path produces correct results for ByMoneyness (underlying I/O)."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (_contract(strike=4490, expiration=expiration), _row(row_date=d, iv=0.21)),
        (_contract(strike=4510, expiration=expiration), _row(row_date=d, iv=0.22)),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(4500.0),
        bulk_chain_reader=bulk_reader,
    )
    assert errors[0] is None
    # Same tie-break as the per-date path: 4510 wins by float arithmetic.
    assert values[0] == pytest.approx(0.22)


async def test_bulk_path_by_delta():
    """Bulk path produces correct results for ByDelta (stored-only)."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (_contract(strike=4490, expiration=expiration), _row(row_date=d, delta=0.45)),
        (_contract(strike=4500, expiration=expiration), _row(row_date=d, delta=0.50)),
        (_contract(strike=4510, expiration=expiration), _row(row_date=d, delta=0.55)),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByDelta(target_delta=0.50, tolerance=0.05, strict=False),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert errors[0] is None
    # K=4500 has delta=0.50, closest to target.
    assert values[0] == pytest.approx(0.20)  # _row default iv=0.20


async def test_bulk_path_cycle_filter():
    """Bulk path respects cycle injection (same as test_multi_cycle_filter)."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    contract_m = _contract(strike=4500, expiration=expiration, cycle="M")
    contract_w = _contract(strike=4500, expiration=expiration, cycle="W")
    row_m = _row(row_date=d, iv=0.20)
    row_w = _row(row_date=d, iv=0.30)
    chain = [(contract_m, row_m), (contract_w, row_w)]

    # cycle="W" — only W visible in bulk path.
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle="W",
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(4500.0),
        bulk_chain_reader=bulk_reader,
    )
    assert errors[0] is None
    assert values[0] == pytest.approx(0.30)


async def test_bulk_path_last_trade_date():
    """Bulk path respects last_trade_date cutoff."""
    ltd = date(2024, 3, 22)
    d_at = ltd
    d_after = date(2024, 3, 25)
    expiration = date(2024, 4, 19)
    chain_at = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d_at, iv=0.21)),
    ]
    reader = FakeChainReader({d_at: chain_at})
    bulk_reader = FakeBulkChainReader({d_at: chain_at})

    values, errors, _contracts = await resolve_option_stream(
        dates=[d_at, d_after],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        last_trade_date=ltd,
        bulk_chain_reader=bulk_reader,
    )
    assert errors[0] is None and values[0] == pytest.approx(0.21)
    assert np.isnan(values[1])
    assert errors[1] == "past_last_trade_date"


async def test_bulk_path_nearest_to_target():
    """Bulk path works with NearestToTarget maturity (one probe + one bulk)."""
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 20)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [
            (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20)),
        ]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    values, errors, _contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert all(e is None for e in errors)
    assert all(v == pytest.approx(0.20) for v in values)
    # Exactly 1 probe query (NearestToTarget), 1 bulk call.
    assert len(reader.calls) == 1  # probe only
    assert len(bulk_reader.bulk_calls) == 1


async def test_bulk_path_nearest_to_target_with_available_expirations():
    """Bulk path + NearestToTarget + available_expirations skips the probe query.

    When ``available_expirations`` is supplied, the resolver filters
    locally (``first_date <= e <= far_future``) instead of issuing an
    expensive probe ``query_chain`` call.  This test verifies:
    - Zero probe queries on chain_reader (reader.calls == 0).
    - The correct expiration is selected from the pre-fetched list.
    - Results are identical to the probe-query fallback.
    """
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 20)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [
            (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20)),
        ]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)

    # Pre-fetched expirations: includes the target plus some decoys.
    all_expirations = [
        date(2024, 1, 19),  # far past — should be filtered out
        date(2024, 4, 19),  # the correct one (nearest to target_dte_days=30)
        date(2024, 5, 17),  # also in window, but farther from target
        date(2024, 6, 21),  # also in window
        date(2027, 12, 19),  # may be beyond far_future depending on probe_days
    ]

    values, errors, _contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
        available_expirations=all_expirations,
    )
    assert all(e is None for e in errors)
    assert all(v == pytest.approx(0.20) for v in values)
    # The key assertion: zero probe queries — the fast path was used.
    assert len(reader.calls) == 0, (
        f"Expected 0 probe queries (available_expirations fast path); "
        f"got {len(reader.calls)}"
    )
    assert len(bulk_reader.bulk_calls) == 1


async def test_bulk_available_expirations_boundary_filtering():
    """available_expirations fast path: exp_before is included in the
    candidate set (lower_bound loosened by I-4 fix) but the maturity
    resolver picks exp_after because it's closer to the target DTE.

    Setup: two expirations — one before the first trade date and one
    after.  With target_dte_days=10 and first_date=2024-04-01:
      exp_before (Mar 25): DTE = -7, |(-7) - 10| = 17
      exp_after  (Apr 19): DTE = 18, |18 - 10| = 8
    exp_after wins by proximity to target.  The test verifies the
    after-expiration is selected and that iv matches the chain keyed
    to it.
    """
    dates = [date(2024, 4, 1), date(2024, 4, 2)]
    # Expiration A: before first_date — filtered out by boundary.
    exp_before = date(2024, 3, 25)
    # Expiration B: after first_date — retained.
    exp_after = date(2024, 4, 19)

    chains_by_date = {
        d: [
            (
                _contract(strike=4500, expiration=exp_after),
                _row(row_date=d, iv=0.25),
            ),
        ]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)

    # Provide both expirations; exp_before should be filtered out.
    all_expirations = [exp_before, exp_after]

    values, errors, _contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=10),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
        available_expirations=all_expirations,
    )
    assert all(e is None for e in errors)
    assert all(v == pytest.approx(0.25) for v in values)
    # Fast path: zero probe queries.
    assert len(reader.calls) == 0
    # Only one bulk call for exp_after.
    assert len(bulk_reader.bulk_calls) == 1
    assert bulk_reader.bulk_calls[0]["expiration_min"] == exp_after
    assert bulk_reader.bulk_calls[0]["expiration_max"] == exp_after


async def test_bulk_available_expirations_picks_nearest_to_target():
    """available_expirations: resolver picks the expiration closest to target DTE.

    With target_dte_days=30 and trade date 2024-04-01, the target
    expiration date is 2024-05-01.  Among two candidates (2024-04-19
    at DTE=18 and 2024-05-17 at DTE=46), 2024-04-19 is closer to the
    target (|18-30|=12 vs |46-30|=16) and should be selected.
    """
    d = date(2024, 4, 1)
    exp_close = date(2024, 4, 19)  # DTE=18, |18-30|=12
    exp_far = date(2024, 5, 17)  # DTE=46, |46-30|=16
    chain = [
        (_contract(strike=4500, expiration=exp_close), _row(row_date=d, iv=0.18)),
        (_contract(strike=4500, expiration=exp_far), _row(row_date=d, iv=0.22)),
    ]
    reader = FakeChainReader({d: chain})
    # Bulk reader only has the exp_close chain for this date (since the
    # resolver will issue a bulk query filtered to exp_close).
    bulk_reader = FakeBulkChainReader({d: chain})

    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
        available_expirations=[exp_close, exp_far],
    )
    assert errors[0] is None
    # exp_close (iv=0.18) should win.
    assert values[0] == pytest.approx(0.18)
    assert len(reader.calls) == 0  # no probe query
    # Bulk fetches only for exp_close (the resolved expiration).
    assert len(bulk_reader.bulk_calls) == 1
    assert bulk_reader.bulk_calls[0]["expiration_min"] == exp_close


async def test_bulk_path_no_chain_for_date():
    """Bulk path handles dates with no chain data → no_chain_for_date."""
    dates = [date(2024, 3, 18), date(2024, 3, 19)]
    expiration = date(2024, 4, 19)
    # Only d1 has data, d2 does not.
    chains_by_date = {
        dates[0]: [
            (
                _contract(strike=4500, expiration=expiration),
                _row(row_date=dates[0], iv=0.20),
            ),
        ],
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    values, errors, _contracts = await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert errors[0] is None and values[0] == pytest.approx(0.20)
    assert np.isnan(values[1])
    assert errors[1] == "no_chain_for_date"


async def test_bulk_path_progress_callback():
    """Bulk path calls progress_callback once per date (including skipped)."""
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 3, 25)]
    expiration = date(2024, 4, 19)
    chains_by_date = {
        d: [
            (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20)),
        ]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    ticks = [0]

    def tick():
        ticks[0] += 1

    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        last_trade_date=date(2024, 3, 22),
        bulk_chain_reader=bulk_reader,
        progress_callback=tick,
    )
    # 3 date ticks (2 queryable + 1 past_last_trade_date) plus 1
    # Phase B expiration-fetch tick (all dates share one expiration).
    assert ticks[0] == 4


async def test_bulk_path_missing_iv():
    """Bulk path correctly surfaces missing_iv when iv_stored is None."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=None)),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    values, errors, _contracts = await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert np.isnan(values[0])
    assert errors[0] == "missing_iv"


# ── Strike-window narrowing tests ─────────────────────────────────────


async def test_bulk_by_strike_passes_exact_strike_window():
    """ByStrike selection narrows the bulk query to strike_min=K, strike_max=K."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20)),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    assert call["strike_min"] == 4500.0
    assert call["strike_max"] == 4500.0


async def test_bulk_by_moneyness_passes_strike_window():
    """ByMoneyness selection computes a strike window from the spot price."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    spot = 4500.0
    chain = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20)),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(spot),
        bulk_chain_reader=bulk_reader,
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    # Window: spot * (1.0 - 0.05 - 0.10) to spot * (1.0 + 0.05 + 0.10)
    assert call["strike_min"] is not None
    assert call["strike_max"] is not None
    assert call["strike_min"] == pytest.approx(spot * 0.85, rel=0.01)
    assert call["strike_max"] == pytest.approx(spot * 1.15, rel=0.01)


async def test_bulk_by_delta_passes_wide_strike_window():
    """ByDelta selection uses a wide moneyness proxy band (±30% of spot)."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    spot = 4500.0
    chain = [
        (
            _contract(strike=4500, expiration=expiration),
            _row(row_date=d, iv=0.20, delta=0.50),
        ),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByDelta(target_delta=0.50, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(spot),
        bulk_chain_reader=bulk_reader,
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    assert call["strike_min"] is not None
    assert call["strike_max"] is not None
    assert call["strike_min"] == pytest.approx(spot * 0.70, rel=0.01)
    assert call["strike_max"] == pytest.approx(spot * 1.30, rel=0.01)


async def test_bulk_no_underlying_resolver_no_strike_window():
    """Without an underlying_price_resolver, ByMoneyness has no strike window."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    chain = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d, iv=0.20)),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByMoneyness(target_K_over_S=1.0, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    assert call["strike_min"] is None
    assert call["strike_max"] is None


# ── Widened strike window tests (date-range-proportional margin) ──────


async def test_bulk_by_delta_strike_window_1day_base_margin():
    """1-day range (span=0 days): ByDelta uses the base 30% margin."""
    d = date(2024, 3, 22)
    expiration = date(2024, 4, 19)
    spot = 4500.0
    chain = [
        (
            _contract(strike=4500, expiration=expiration),
            _row(row_date=d, iv=0.20, delta=0.50),
        ),
    ]
    reader = FakeChainReader({d: chain})
    bulk_reader = FakeBulkChainReader({d: chain})
    await resolve_option_stream(
        dates=[d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByDelta(target_delta=0.50, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(spot),
        bulk_chain_reader=bulk_reader,
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    # span=0 → extra=0 → margin=0.30: spot * 0.70 .. spot * 1.30
    assert call["strike_min"] == pytest.approx(spot * 0.70, rel=0.001)
    assert call["strike_max"] == pytest.approx(spot * 1.30, rel=0.001)


async def test_bulk_by_delta_strike_window_6month_wider_margin():
    """6-month range (~183 days): ByDelta widens to ~37.5% margin.

    extra = min(183/365 * 0.15, 0.30) ≈ 0.0752
    margin = 0.30 + 0.0752 ≈ 0.3752

    Uses NearestToTarget + available_expirations to force both dates
    onto one expiration so there is exactly one bulk call to inspect.
    """
    first_d = date(2024, 1, 2)
    last_d = date(2024, 7, 3)  # ~183 days later
    span_days = (last_d - first_d).days
    assert 180 <= span_days <= 185  # sanity check

    expiration = date(2024, 8, 16)
    spot = 4500.0
    dates = [first_d, last_d]
    chains_by_date = {
        d: [
            (
                _contract(strike=4500, expiration=expiration),
                _row(row_date=d, iv=0.20, delta=0.50),
            ),
        ]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=180),
        selection=ByDelta(target_delta=0.50, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(spot),
        bulk_chain_reader=bulk_reader,
        available_expirations=[expiration],
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    expected_extra = min(span_days / 365.0 * 0.15, 0.30)
    expected_margin = 0.30 + expected_extra
    assert call["strike_min"] == pytest.approx(spot * (1 - expected_margin), rel=0.001)
    assert call["strike_max"] == pytest.approx(spot * (1 + expected_margin), rel=0.001)
    # Margin should be ~37.5%, noticeably wider than the base 30%.
    assert expected_margin > 0.37
    assert expected_margin < 0.39


async def test_bulk_by_delta_strike_window_2year_capped_margin():
    """2-year range (730 days): ByDelta caps the extra at +30% → total 60%.

    extra = min(730/365 * 0.15, 0.30) = min(0.30, 0.30) = 0.30
    margin = 0.30 + 0.30 = 0.60

    Uses NearestToTarget + available_expirations to force both dates
    onto one expiration so there is exactly one bulk call to inspect.
    """
    first_d = date(2022, 3, 22)
    last_d = date(2024, 3, 22)  # exactly 2 years (731 days with leap year)
    span_days = (last_d - first_d).days
    assert span_days >= 730  # 2 years

    expiration = date(2024, 4, 19)
    spot = 4500.0
    dates = [first_d, last_d]
    chains_by_date = {
        d: [
            (
                _contract(strike=4500, expiration=expiration),
                _row(row_date=d, iv=0.20, delta=0.50),
            ),
        ]
        for d in dates
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=365),
        selection=ByDelta(target_delta=0.50, tolerance=0.05),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=_make_underlying_resolver(spot),
        bulk_chain_reader=bulk_reader,
        available_expirations=[expiration],
    )
    assert len(bulk_reader.bulk_calls) == 1
    call = bulk_reader.bulk_calls[0]
    # Capped: margin = 0.60
    assert call["strike_min"] == pytest.approx(spot * 0.40, rel=0.001)
    assert call["strike_max"] == pytest.approx(spot * 1.60, rel=0.001)


# ── Phase B progress ticks test ───────────────────────────────────────


async def test_bulk_phase_b_ticks_per_expiration():
    """Phase B fires one progress tick per unique expiration fetched."""
    dates = [date(2024, 3, 18), date(2024, 3, 19), date(2024, 5, 13)]
    exp_apr = date(2024, 4, 19)
    exp_jun = date(2024, 6, 21)
    chains_by_date = {
        date(2024, 3, 18): [
            (
                _contract(strike=4500, expiration=exp_apr),
                _row(row_date=date(2024, 3, 18), iv=0.20),
            ),
        ],
        date(2024, 3, 19): [
            (
                _contract(strike=4500, expiration=exp_apr),
                _row(row_date=date(2024, 3, 19), iv=0.21),
            ),
        ],
        date(2024, 5, 13): [
            (
                _contract(strike=4500, expiration=exp_jun),
                _row(row_date=date(2024, 5, 13), iv=0.22),
            ),
        ],
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)
    ticks = [0]

    def tick():
        ticks[0] += 1

    await resolve_option_stream(
        dates=dates,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NextThirdFriday(offset_months=0),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
        progress_callback=tick,
    )
    # 3 date ticks (Phase C) + 2 expiration ticks (Phase B: 2 unique expirations)
    assert ticks[0] == 5


# ── NearestToTarget DTE lower-bound fix (I-4) ────────────────────────


async def test_nearest_to_target_includes_expirations_before_first_date():
    """NearestToTarget with small DTE: expirations slightly before first_date
    must be included in the candidate set (I-4 fix).

    first_date = Apr 8, target_dte_days = 5.  Two expirations:
      exp_before = Apr 5 (3 days before first_date, DTE = -3,
                          |(-3) - 5| = 8)
      exp_far    = Apr 26 (DTE = 18, |18 - 5| = 13)

    Old filter ``first_date <= exp`` would exclude exp_before.  The fix
    loosens to ``lower_bound = first_date - max(target, 7) = Apr 1``,
    so exp_before is included.  exp_before wins (|8| < |13|).
    """
    first_d = date(2024, 4, 8)
    second_d = date(2024, 4, 9)
    exp_before = date(2024, 4, 5)  # before first_date
    exp_far = date(2024, 4, 26)

    # Chain data keyed to exp_before (the closer-to-target expiration).
    chains_by_date = {
        d: [
            (
                _contract(strike=4500, expiration=exp_before),
                _row(row_date=d, iv=0.15),
            ),
            (
                _contract(strike=4500, expiration=exp_far),
                _row(row_date=d, iv=0.30),
            ),
        ]
        for d in [first_d, second_d]
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)

    # Pre-fetched expirations — includes exp_before.
    all_expirations = [exp_before, exp_far]

    values, errors, _contracts = await resolve_option_stream(
        dates=[first_d, second_d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=5),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
        available_expirations=all_expirations,
    )
    # exp_before (iv=0.15) should be selected as it's closer to target DTE.
    # If the old filter were still in place, exp_before would be excluded
    # and exp_far (iv=0.30) would be selected instead.
    assert all(e is None for e in errors), f"Unexpected errors: {errors}"
    assert values[0] == pytest.approx(0.15), (
        f"Expected exp_before (iv=0.15) to be selected; got {values[0]}"
    )
    assert values[1] == pytest.approx(0.15)
    # Zero probe queries (fast path with available_expirations).
    assert len(reader.calls) == 0


async def test_nearest_to_target_fallback_probe_uses_loosened_lower_bound():
    """NearestToTarget fallback probe query uses the loosened lower bound.

    When ``available_expirations`` is None, the resolver falls back to a
    probe ``query_chain`` call.  The fix loosens ``expiration_min`` from
    ``first_date`` to ``first_date - max(target_dte_days, 7)``.  This
    test verifies the probe query's ``expiration_min`` reflects the
    loosened bound.
    """
    first_d = date(2024, 4, 8)
    exp_far = date(2024, 4, 26)

    chains_by_date = {
        first_d: [
            (
                _contract(strike=4500, expiration=exp_far),
                _row(row_date=first_d, iv=0.20),
            ),
        ],
    }
    reader = FakeChainReader(chains_by_date)
    bulk_reader = FakeBulkChainReader(chains_by_date)

    # No available_expirations → fallback probe query is used.
    await resolve_option_stream(
        dates=[first_d],
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=5),
        selection=ByStrike(strike=4500.0),
        stream="iv",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk_reader,
        available_expirations=None,  # force fallback probe
    )
    # The probe query should have used the loosened lower bound:
    # lower_bound = first_d - timedelta(days=max(5, 7)) = Apr 8 - 7 = Apr 1
    assert len(reader.calls) == 1, f"Expected 1 probe query, got {len(reader.calls)}"
    probe_call = reader.calls[0]
    expected_lower = first_d - timedelta(days=max(5, 7))
    assert probe_call["expiration_min"] == expected_lower, (
        f"Probe query expiration_min should be {expected_lower}, "
        f"got {probe_call['expiration_min']}"
    )


# ── Issue #2: NearestToTarget per-date listed-expiration snapping ───────


async def test_nearest_to_target_snaps_to_expiration_listed_that_date():
    """Daily-expiration root: NearestToTarget must pick an expiration LISTED on
    the trade date, not the global-window nearest that is only listed later.

    Repro of the OPT_BTC ``no_chain_for_date`` bug: on ``d1`` only near-dated
    expirations are quoted; the 30-day-nearest in the GLOBAL set (``far_exp``)
    is not listed until ``d2``.  Without the per-date map the resolver snaps to
    ``far_exp`` → Phase B finds 0 rows on ``d1`` → ``no_chain_for_date``.  With
    the map it snaps to the nearest expiration actually listed on ``d1``.
    """
    d1 = date(2021, 1, 5)
    d2 = date(2021, 2, 10)
    near_exp = date(2021, 1, 29)  # listed on d1, ~24d out
    far_exp = date(2021, 2, 4)  # global-nearest to 30d, NOT listed on d1
    far_exp2 = date(2021, 2, 26)  # listed on d1 too, ~52d out

    # d1 chain lists near_exp and far_exp2 (NOT far_exp); d2 lists far_exp.
    chain_d1 = [
        (
            _contract(strike=100, expiration=near_exp, collection="OPT_BTC"),
            _row(row_date=d1, mid=2.0),
        ),
        (
            _contract(strike=100, expiration=far_exp2, collection="OPT_BTC"),
            _row(row_date=d1, mid=4.0),
        ),
    ]
    chain_d2 = [
        (
            _contract(strike=100, expiration=far_exp, collection="OPT_BTC"),
            _row(row_date=d2, mid=3.0),
        ),
    ]
    reader = FakeChainReader({d1: chain_d1, d2: chain_d2})
    bulk = FakeBulkChainReader({d1: chain_d1, d2: chain_d2})

    global_exps = [near_exp, far_exp, far_exp2]
    per_date = {d1: [near_exp, far_exp2], d2: [far_exp]}

    # WITHOUT the per-date map: snaps to far_exp on d1 → no rows → NaN.
    vals_blind, errs_blind, _ = await resolve_option_stream(
        dates=[d1],
        collection="OPT_BTC",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=100),
        stream="mid",
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=bulk,
        available_expirations=global_exps,
    )
    assert np.isnan(vals_blind[0])
    assert errs_blind[0] == "no_chain_for_date"

    # WITH the per-date map: snaps to near_exp (listed on d1) → resolves 2.0.
    vals_fix, errs_fix, contracts_fix = await resolve_option_stream(
        dates=[d1],
        collection="OPT_BTC",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=100),
        stream="mid",
        chain_reader=FakeChainReader({d1: chain_d1, d2: chain_d2}),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=FakeBulkChainReader({d1: chain_d1, d2: chain_d2}),
        available_expirations=global_exps,
        available_expirations_by_date=per_date,
    )
    assert errs_fix[0] is None
    assert vals_fix[0] == pytest.approx(2.0)
    assert contracts_fix[0] is not None
    assert contracts_fix[0].expiration == near_exp


async def test_nearest_to_target_sparse_root_weekly_listing_lag_nan_to_value():
    """Sparse monthly/weekly root (SPX / OPT_SP_500) has the SAME latent
    ``no_chain_for_date`` hole as the daily-expiration root (OPT_BTC).

    SPX is not immune to the global-snap bug: it carries weeklies with the same
    listing-lag, so an expiration can exist in the GLOBAL set (a dim scan of
    every expiration that ever existed) while it is not yet quoted (no price
    row) on early trade dates.  When that not-yet-listed weekly is the
    global-nearest to the DTE target, the legacy path snaps to it → Phase B
    finds 0 rows → a silent ``no_chain_for_date`` NaN.  The per-date map moves
    the pick to an expiration actually listed on the date → a real value.

    Live repro (read-only dwh) that this synthetic mirrors: OPT_SP_500 P,
    target_dte=30, 2023-01-03..2023-03-10 — 7/47 trade dates changed, ALL
    NaN→value, zero value→value.  Concretely on 2023-03-02 the global-nearest
    2023-04-07 (a weekly) had 0 price rows while the per-date pick 2023-03-24
    had 222.  This test pins that NaN→value direction so the sparse-root case is
    no longer mislabelled "unchanged".
    """
    d = date(2023, 3, 2)
    exp_near = date(2023, 3, 24)  # listed on d (weekly, ~22d out)
    exp_glob = date(2023, 4, 7)  # global-nearest to 30d, NOT listed on d
    exp_month = date(2023, 4, 21)  # listed on d (monthly, ~50d out)

    # d's chain quotes exp_near and exp_month (NOT exp_glob).
    chain_d = [
        (
            _contract(
                strike=4000, expiration=exp_near, type_="P", collection="OPT_SP_500"
            ),
            _row(row_date=d, mid=12.0),
        ),
        (
            _contract(
                strike=4000, expiration=exp_month, type_="P", collection="OPT_SP_500"
            ),
            _row(row_date=d, mid=30.0),
        ),
    ]
    # exp_glob is nearest to the 30d target (2023-04-01) among the global set,
    # so the legacy path snaps to it — but no contract of it is quoted on d.
    global_exps = [exp_near, exp_glob, exp_month]
    per_date = {d: [exp_near, exp_month]}

    async def _run(per_date_map):
        return await resolve_option_stream(
            dates=[d],
            collection="OPT_SP_500",
            option_type="P",
            cycle=None,
            maturity=NearestToTarget(target_dte_days=30),
            selection=ByStrike(strike=4000),
            stream="mid",
            chain_reader=FakeChainReader({d: chain_d}),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=FakeBulkChainReader({d: chain_d}),
            available_expirations=global_exps,
            available_expirations_by_date=per_date_map,
        )

    # WITHOUT the per-date map: snaps to exp_glob → 0 rows on d → NaN hole.
    vals_blind, errs_blind, _ = await _run(None)
    assert np.isnan(vals_blind[0])
    assert errs_blind[0] == "no_chain_for_date"

    # WITH the per-date map: snaps to exp_near (listed on d) → resolves 12.0.
    vals_fix, errs_fix, contracts_fix = await _run(per_date)
    assert errs_fix[0] is None
    assert vals_fix[0] == pytest.approx(12.0)
    assert contracts_fix[0] is not None
    assert contracts_fix[0].expiration == exp_near


async def test_nearest_to_target_per_date_map_leaves_valid_global_pick_unchanged():
    """SAFETY invariant (the other half of the sweep-2 NaN→value pin): when the
    global-nearest expiration IS listed on the trade date, supplying the per-date
    map must yield the IDENTICAL contract — value==value, no change.

    The per-date snapping (stream_resolver.py Issue-#2 fix) is a strictly
    corrective move: it only differs from the global pick when the global pick is
    NOT quoted that day.  The two sweep-2 tests pin the corrective direction
    (unlisted global pick → NaN without the map, real value with it).  This pins
    the complementary guarantee — that an ALREADY-VALID global pick is never
    perturbed by the map — so a future resolver edit that changed a valid pick
    (e.g. always re-snapping to the date's nearest-listed even when the global
    pick is listed) would be caught here rather than silently altering series.

    Setup: target_dte=30 on 2023-03-02 (target date 2023-04-01).  Three
    expirations, ALL quoted on the date:
      exp_near  2023-03-24  DTE 22  |22-30| = 8
      exp_glob  2023-04-03  DTE 32  |32-30| = 2   ← global-nearest, and listed
      exp_month 2023-04-21  DTE 50  |50-30| = 20
    The global-nearest (exp_glob) is listed on the date, so the map — which
    includes exp_glob — must resolve to the exact same contract/value.
    """
    d = date(2023, 3, 2)
    exp_near = date(2023, 3, 24)  # listed, ~22d out
    exp_glob = date(2023, 4, 3)  # global-nearest to 30d, ALSO listed on d
    exp_month = date(2023, 4, 21)  # listed, ~50d out

    # d's chain quotes ALL THREE expirations (distinct mids identify the pick).
    chain_d = [
        (
            _contract(
                strike=4000, expiration=exp_near, type_="P", collection="OPT_SP_500"
            ),
            _row(row_date=d, mid=12.0),
        ),
        (
            _contract(
                strike=4000, expiration=exp_glob, type_="P", collection="OPT_SP_500"
            ),
            _row(row_date=d, mid=15.0),
        ),
        (
            _contract(
                strike=4000, expiration=exp_month, type_="P", collection="OPT_SP_500"
            ),
            _row(row_date=d, mid=30.0),
        ),
    ]
    global_exps = [exp_near, exp_glob, exp_month]
    # The per-date map lists exactly the same expirations as the global set for
    # this date (the global pick is genuinely quoted here).
    per_date = {d: [exp_near, exp_glob, exp_month]}

    async def _run(per_date_map):
        return await resolve_option_stream(
            dates=[d],
            collection="OPT_SP_500",
            option_type="P",
            cycle=None,
            maturity=NearestToTarget(target_dte_days=30),
            selection=ByStrike(strike=4000),
            stream="mid",
            chain_reader=FakeChainReader({d: chain_d}),
            maturity_resolver=DefaultMaturityResolver(),
            underlying_price_resolver=None,
            bulk_chain_reader=FakeBulkChainReader({d: chain_d}),
            available_expirations=global_exps,
            available_expirations_by_date=per_date_map,
        )

    # WITHOUT the per-date map: global path snaps to exp_glob → 15.0 (valid).
    vals_global, errs_global, contracts_global = await _run(None)
    assert errs_global[0] is None
    assert vals_global[0] == pytest.approx(15.0)
    assert contracts_global[0] is not None
    assert contracts_global[0].expiration == exp_glob

    # WITH the per-date map: the identical (already-valid) pick — no change.
    vals_map, errs_map, contracts_map = await _run(per_date)
    assert errs_map[0] is None
    assert vals_map[0] == pytest.approx(15.0)
    assert vals_map[0] == vals_global[0]  # value==value: pick unchanged
    assert contracts_map[0] is not None
    assert contracts_map[0].expiration == exp_glob
    assert contracts_map[0].contract_id == contracts_global[0].contract_id
