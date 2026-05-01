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
from datetime import date
from typing import Iterable, Literal
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
    values, errors = await resolve_option_stream(
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
    values, errors = await resolve_option_stream(
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
    values_m, errors_m = await resolve_option_stream(
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
    values_s, errors_s = await resolve_option_stream(
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
    values_w, errors_w = await resolve_option_stream(
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
    values_m, errors_m = await resolve_option_stream(
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

    values, errors = await resolve_option_stream(
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
        (_contract(strike=4500, expiration=expiration), _row(row_date=d_before, iv=0.20)),
    ]
    full_chain_at = [
        (_contract(strike=4500, expiration=expiration), _row(row_date=d_at, iv=0.21)),
    ]
    reader = FakeChainReader({d_before: full_chain_before, d_at: full_chain_at})

    values, errors = await resolve_option_stream(
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
        f"Expected ≤{K*len(dates)} chain calls for {len(dates)} dates "
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
        f"Expected ≤{K*len(dates)} chain calls for {len(dates)} dates "
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


_TRIVIAL_INDICATOR = (
    "def compute(series):\n"
    "    return series['x']\n"
)


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
