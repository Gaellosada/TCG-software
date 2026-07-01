"""FIX C — a Phase-B bulk-fetch failure degrades to per-date NaN, not a 500.

The bulk resolver groups dates by resolved expiration and issues one
``query_chain_bulk`` per expiration concurrently under ``asyncio.gather``
(``stream_resolver._resolve_bulk`` Phase B).  Before this fix a single
``query_chain_bulk`` raising (a transient dwh error, or a ``PoolTimeout`` under
contention) propagated out of the gather and aborted the WHOLE resolve — the
data layer then wrapped it as ``OptionsDataAccessError`` and the API returned a
hard 500 for the entire series, even though only ONE expiration's dates were
affected.  Phase C already degrades a missing/failed date to a per-date NaN +
diagnostic; Phase B should behave the same.

These tests pin: one expiration's fetch raising leaves THAT expiration's dates
NaN with a ``data_access_error`` diagnostic, while the other expirations resolve
normally — the resolve as a whole still succeeds.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Sequence

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByStrike,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    RollOffset,
)

from _stream_fakes import _contract, _row

# Two well-separated monthly expirations; one trade date ~30 DTE before each, so
# NearestToTarget(30) maps each date to a DISTINCT expiration → Phase B builds two
# independent fetch tasks (one per expiration).
_EXP_A = date(2024, 3, 15)
_EXP_B = date(2024, 6, 21)
_EXPIRATIONS = [_EXP_A, _EXP_B]
_DATE_A = _EXP_A - timedelta(days=30)
_DATE_B = _EXP_B - timedelta(days=30)
_DATES = [_DATE_A, _DATE_B]


class _OneExpirationRaisesBulkReader:
    """Bulk reader that RAISES for the ``_EXP_B`` fetch but serves ``_EXP_A``.

    Models a transient dwh failure / PoolTimeout hitting one expiration's query
    while another succeeds.
    """

    def __init__(self) -> None:
        self.calls: list[date] = []

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
        self.calls.append(expiration_min)
        if expiration_min == _EXP_B:
            raise RuntimeError("couldn't get a connection after 30.00 sec")
        return {
            d: [(_contract(strike=4500, expiration=_EXP_A), _row(row_date=d, mid=1.5))]
            for d in dates
        }


async def test_one_expiration_fetch_failure_degrades_to_nan_not_raise():
    """A raising Phase-B fetch must NOT abort the whole resolve."""
    reader = _OneExpirationRaisesBulkReader()

    # Must not raise (pre-fix: the RuntimeError propagated out of the gather).
    values, errors, contracts = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=_OneExpirationRaisesBulkReader(),  # probe path unused (ByStrike)
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=reader,
        available_expirations=_EXPIRATIONS,
    )

    # Index of each date.
    i_a, i_b = 0, 1
    # _EXP_A date resolved to a real value.
    assert not np.isnan(values[i_a]), "the healthy expiration must still resolve"
    assert values[i_a] == 1.5
    assert errors[i_a] is None
    assert contracts[i_a] is not None
    # _EXP_B date (the failed fetch) is NaN with a data-access diagnostic.
    assert np.isnan(values[i_b]), "the failed-fetch date must be NaN"
    assert errors[i_b] == "data_access_error", (
        f"expected a data_access_error diagnostic, got {errors[i_b]!r}"
    )
    assert contracts[i_b] is None


async def test_all_expirations_failing_still_returns_all_nan_not_raise():
    """If EVERY Phase-B fetch fails, the resolve still returns (all-NaN), not 500."""

    class _AllRaiseBulkReader:
        async def query_chain_bulk(self, **_kw):
            raise RuntimeError("dwh down")

    values, errors, contracts = await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="C",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByStrike(strike=4500.0),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=_AllRaiseBulkReader(),
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=None,
        bulk_chain_reader=_AllRaiseBulkReader(),
        available_expirations=_EXPIRATIONS,
    )
    assert all(np.isnan(v) for v in values)
    assert all(e == "data_access_error" for e in errors)
    assert all(c is None for c in contracts)
