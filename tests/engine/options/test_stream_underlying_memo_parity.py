"""RESULT-INVARIANCE: memoized-underlying resolve == per-date resolve (VALUES).

The perf fix memoizes the Phase-C underlying lookup (one ranged fetch per distinct
future instead of one per trade date).  This test proves the memoization does NOT
change the resolver's output: it runs ``resolve_option_stream`` twice over the same
synthetic chain — once with a PER-DATE underlying resolver, once with a MEMOIZED one
serving the SAME underlying values from a cache — and asserts the ``values`` /
``error_codes`` / ``contracts`` are IDENTICAL, for BOTH ByMoneyness and ByDelta.

NOTE (correction to the perf diagnosis): only **ByMoneyness** reads the underlying PER
TRADE DATE in Phase C (the N+1 the memo collapses).  **ByDelta** matches on STORED
deltas and reads the underlying only ONCE (the strike-window probe), so the memo is a
no-op for it — but it must still not change ByDelta's values, which this test pins.

The underlying resolver is a plain ``Callable[[contract, date], float | None]``, so
the two variants differ ONLY in how many times they read the source map — the value
returned for any (contract, date) is identical by construction.  Identical inputs to
Phase C ⇒ identical outputs; this pins that the real Phase C code produces the same
series either way.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import ByDelta, ByMoneyness, NearestToTarget, RollOffset

from _stream_fakes import FakeBulkChainReader, FakeChainReader, _contract, _row


# One monthly expiration; a strip of trade dates in the month before it (all share
# the SAME underlying future → the exact case the memo collapses).
_EXP = date(2024, 3, 15)
_DATES = [
    d for d in (_EXP - timedelta(days=30 - i) for i in range(20)) if d.weekday() < 5
]

# Synthetic chain per date: a few strikes around 5000 with deltas, so ByMoneyness
# and ByDelta both have something to match.  underlying_price_stored stays None
# (non-BTC) so the injected underlying resolver is what drives K/S.
_STRIKES = [4800.0, 4900.0, 5000.0, 5100.0, 5200.0]
_DELTAS = {4800.0: 0.70, 4900.0: 0.60, 5000.0: 0.50, 5100.0: 0.40, 5200.0: 0.30}


def _chains():
    by_date = {}
    for d in _DATES:
        by_date[d] = [
            (
                _contract(strike=k, expiration=_EXP),
                _row(row_date=d, mid=10.0 + k / 1000, delta=_DELTAS[k]),
            )
            for k in _STRIKES
        ]
    return by_date


# The "true" underlying close per date (varies day to day — like a real future).
_UNDERLYING = {d: 5000.0 + i * 3.0 for i, d in enumerate(_DATES)}


def _per_date_resolver_factory():
    calls = {"n": 0}

    async def resolver(contract, row_date):
        calls["n"] += 1  # one read per (contract, date) — the N+1
        return _UNDERLYING.get(row_date)

    return resolver, calls


def _memoized_resolver_factory():
    """Mimics the adapter memo: fetch the whole map ONCE, serve from cache."""
    cache: dict = {}
    calls = {"n": 0}

    async def resolver(contract, row_date):
        if "map" not in cache:
            calls["n"] += 1  # ONE fetch for the whole window
            cache["map"] = dict(_UNDERLYING)
        return cache["map"].get(row_date)

    return resolver, calls


async def _run(selection, underlying_resolver):
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=selection,
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=FakeChainReader(_chains()),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=underlying_resolver,
        bulk_chain_reader=FakeBulkChainReader(_chains()),
        available_expirations=[_EXP],
    )


@pytest.mark.parametrize(
    "selection,per_date_underlying",
    [
        # ByMoneyness consults the underlying PER TRADE DATE in Phase C (the N+1
        # the memo collapses).
        (ByMoneyness(target_K_over_S=1.0, tolerance=0.05), True),
        # ByDelta matches on STORED deltas in Phase C — it does NOT read the
        # underlying per date (only the one strike-window probe).  So the memo
        # changes nothing for it; we still assert VALUE-IDENTITY (it must not
        # regress), but not a read-count reduction.
        (ByDelta(target_delta=0.50, tolerance=0.15, strict=False), False),
    ],
    ids=["by_moneyness", "by_delta"],
)
async def test_memoized_equals_per_date_values(selection, per_date_underlying):
    per_date_res, per_calls = _per_date_resolver_factory()
    memo_res, memo_calls = _memoized_resolver_factory()

    v1, e1, c1 = await _run(selection, per_date_res)
    v2, e2, c2 = await _run(selection, memo_res)

    # VALUES identical (NaN-aware) — the hard result-invariance requirement.
    np.testing.assert_array_equal(np.isnan(v1), np.isnan(v2))
    np.testing.assert_array_equal(v1[~np.isnan(v1)], v2[~np.isnan(v2)])
    # Diagnostics + selected contracts identical.
    assert e1 == e2
    assert [None if c is None else c.contract_id for c in c1] == [
        None if c is None else c.contract_id for c in c2
    ]
    # Sanity: at least some dates resolved to a real value (test is not vacuous).
    assert int(np.sum(~np.isnan(v1))) > 0

    if per_date_underlying:
        # ByMoneyness: per-date variant reads once PER date; memo reads once total.
        assert per_calls["n"] > 1
        assert memo_calls["n"] == 1
        assert memo_calls["n"] < per_calls["n"]
    else:
        # ByDelta: underlying is read only for the one probe (no per-date N+1);
        # both variants read it exactly once → memo is a no-op but still correct.
        assert per_calls["n"] == 1
        assert memo_calls["n"] == 1
