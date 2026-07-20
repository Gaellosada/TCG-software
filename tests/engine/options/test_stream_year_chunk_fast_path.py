"""Wave 3 — year-chunk fast path (``query_chain_bulk_multi``) in ``_resolve_bulk``.

When the injected bulk reader advertises the optional multi-expiration
capability, Phase B collapses the per-expiration ``query_chain_bulk`` fan-out
into ONE ``query_chain_bulk_multi`` call PER CALENDAR YEAR (each expiration
date-restricted to its own window, Option A = no strike window).  These tests
pin:

  * the fast path is TAKEN when the reader supports it (one multi call per year,
    zero per-expiration ``query_chain_bulk`` calls);
  * year-bucketing groups expirations by ``expiration.year``;
  * PARITY: with ``underlying_price_resolver=None`` (so the OLD path also drops
    its strike window → full chain), the fast path yields byte-identical values
    and contracts to the per-expiration path;
  * a reader WITHOUT the capability falls back to the per-expiration path
    (byte-identical, existing behaviour);
  * failure isolation: a year-chunk raising re-runs that year per-expiration
    (recovers when the per-exp path works; degrades only that year's dates to
    ``data_access_error`` when it does not — other years survive, no 500);
  * candidate-set SUPERSET: the fast path fetches the full chain (all strikes),
    a strict superset of any windowed old-path fetch.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Literal, Sequence

import numpy as np

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.series.stream_resolver import (
    _CycleInjectingBulkReader,
    resolve_option_stream,
)

_RESOLVER_LOGGER = "tcg.engine.options.series.stream_resolver"
from tcg.types.options import (
    ByDelta,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    RollOffset,
)

from _stream_fakes import _contract, _cycle_matches, _row

# Two expirations in DIFFERENT calendar years → the fast path must bucket into
# two year-chunks.
_EXP_2023 = date(2023, 6, 16)
_EXP_2024 = date(2024, 6, 21)
_EXPIRATIONS = [_EXP_2023, _EXP_2024]

# Two trade dates per expiration (~30 / ~25 DTE), so NearestToTarget(30) maps
# both to their nearest expiration.
_D23 = [_EXP_2023 - timedelta(days=30), _EXP_2023 - timedelta(days=25)]
_D24 = [_EXP_2024 - timedelta(days=30), _EXP_2024 - timedelta(days=25)]
_DATES = _D23 + _D24

# Put strikes with a monotone delta ladder so ByDelta(-0.10) has a clear winner.
_STRIKE_DELTAS = [
    (4000.0, -0.05),
    (4300.0, -0.10),
    (4600.0, -0.25),
    (4900.0, -0.50),
]


def _chains_by_date() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
    for exp, dates in ((_EXP_2023, _D23), (_EXP_2024, _D24)):
        for d in dates:
            chains[d] = [
                (
                    _contract(strike=k, expiration=exp, type_="P"),
                    _row(row_date=d, mid=k / 1000.0, delta=dlt),
                )
                for k, dlt in _STRIKE_DELTAS
            ]
    return chains


class _FakeMultiBulkReader:
    """Bulk reader implementing BOTH the per-expiration and multi capabilities.

    ``supports_bulk_multi`` is advertised via the presence of
    ``query_chain_bulk_multi`` (the engine wrapper feature-detects it).  Records
    every multi/per-exp call so tests can assert which path ran.
    """

    def __init__(
        self,
        chains: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]],
        *,
        fail_years: frozenset[int] = frozenset(),
        fail_bulk: bool = False,
    ) -> None:
        self._chains = chains
        self._fail_years = fail_years
        self._fail_bulk = fail_bulk
        self.multi_calls: list[dict] = []
        self.bulk_calls: list[dict] = []

    async def query_chain_bulk_multi(
        self,
        *,
        root: str,
        type: Literal["C", "P", "both"],
        groups: Sequence[tuple[date, Sequence[date]]],
        strike_windows=None,
        expiration_cycle=None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        years = {exp.year for exp, _ in groups}
        self.multi_calls.append({"years": years, "n_exp": len(list(groups))})
        if years & self._fail_years:
            raise RuntimeError("simulated year-chunk dwh failure")
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for exp, dates in groups:
            for d in dates:
                result.setdefault(d, [])
                for c, r in self._chains.get(d, []):
                    if (
                        (c.type == type or type == "both")
                        and c.expiration == exp
                        and _cycle_matches(c.expiration_cycle, expiration_cycle)
                    ):
                        result[d].append((c, r))
        return result

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
        expiration_cycle=None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        self.bulk_calls.append({"expiration_min": expiration_min, "dates": list(dates)})
        if self._fail_bulk:
            raise RuntimeError("simulated per-expiration dwh failure")
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates:
            rows = [
                (c, r)
                for (c, r) in self._chains.get(d, [])
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and _cycle_matches(c.expiration_cycle, expiration_cycle)
            ]
            if rows:
                result[d] = rows
        return result


class _NoMultiBulkReader(_FakeMultiBulkReader):
    """Same data, but WITHOUT the multi capability (old path only)."""

    query_chain_bulk_multi = None  # type: ignore[assignment]


async def _resolve(reader, *, underlying=None):
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=underlying,
        bulk_chain_reader=reader,
        available_expirations=_EXPIRATIONS,
    )


async def test_fast_path_taken_one_multi_call_per_year_no_per_exp():
    reader = _FakeMultiBulkReader(_chains_by_date())
    values, errors, contracts = await _resolve(reader)
    # Two calendar years → two multi calls; NO per-expiration query_chain_bulk.
    assert len(reader.multi_calls) == 2
    assert reader.bulk_calls == []
    assert {frozenset(c["years"]) for c in reader.multi_calls} == {
        frozenset({2023}),
        frozenset({2024}),
    }
    # Each date resolved (the -0.10 ladder rung exists on every date).
    assert all(not np.isnan(v) for v in values)
    assert all(e is None for e in errors)
    assert all(c is not None for c in contracts)


async def test_year_bucketing_each_chunk_holds_only_its_year():
    reader = _FakeMultiBulkReader(_chains_by_date())
    await _resolve(reader)
    for call in reader.multi_calls:
        assert len(call["years"]) == 1, "a chunk must hold exactly one year"
        assert call["n_exp"] == 1  # one expiration per year in this scenario


async def test_parity_fast_path_equals_old_path():
    """underlying_price_resolver=None → the OLD path also drops its strike
    window (full chain), so the fast path must be byte-identical."""
    fast = _FakeMultiBulkReader(_chains_by_date())
    slow = _NoMultiBulkReader(_chains_by_date())

    v_fast, e_fast, c_fast = await _resolve(fast)
    v_slow, e_slow, c_slow = await _resolve(slow)

    # Sanity: the two readers really took different paths.
    assert fast.multi_calls and not fast.bulk_calls
    assert slow.bulk_calls and not slow.multi_calls

    np.testing.assert_array_equal(v_fast, v_slow)
    assert e_fast == e_slow
    assert [None if c is None else c.contract_id for c in c_fast] == [
        None if c is None else c.contract_id for c in c_slow
    ]
    # The chosen strike is the -0.10 rung (4300) on every date.
    assert all(c is not None and c.strike == 4300.0 for c in c_fast)


def test_none_capability_flag_is_gated_off_via_callable_not_hasattr():
    """GATE assertion (finding 1): a reader that DISABLES the capability with
    ``query_chain_bulk_multi = None`` must report ``supports_bulk_multi is
    False``.  The wrapper uses ``callable(...)``, not ``hasattr`` — ``hasattr``
    is True for a None attribute and would drive the fast path into a TypeError
    + silent slow fallback.  This asserts the gate DIRECTLY, so it FAILS if the
    check reverts to ``hasattr``."""
    disabled = _CycleInjectingBulkReader(_NoMultiBulkReader(_chains_by_date()), None)
    assert disabled.supports_bulk_multi is False
    # A real method still reads as capable.
    capable = _CycleInjectingBulkReader(_FakeMultiBulkReader(_chains_by_date()), None)
    assert capable.supports_bulk_multi is True


async def test_reader_without_capability_falls_back_to_per_expiration(caplog):
    """A ``None``-disabled reader must fall back via CLEAN GATING (fast path
    never entered), NOT via the exception fallback.  The no-year-chunk-WARNING
    assertion is the discriminator: under a ``hasattr`` gate the fast path is
    wrongly entered → TypeError → fallback WARNING fires → this test fails."""
    reader = _NoMultiBulkReader(_chains_by_date())
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        values, errors, _ = await _resolve(reader)
    assert reader.multi_calls == []
    assert reader.bulk_calls, "old per-expiration path must run"
    assert all(not np.isnan(v) for v in values)
    assert not [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "year-chunk" in r.getMessage()
    ], "no-capability reader must be gated off cleanly, not via the fallback"


async def test_year_failure_recovers_via_per_expiration_fallback():
    """A year-chunk raising re-runs that year per-expiration; when the per-exp
    path works, the year's dates recover (no NaN)."""
    reader = _FakeMultiBulkReader(_chains_by_date(), fail_years=frozenset({2023}))
    values, errors, contracts = await _resolve(reader)
    # 2023 multi failed → its two expirations retried via query_chain_bulk.
    assert any(2023 in c["years"] for c in reader.multi_calls)
    assert reader.bulk_calls, "the failed year must fall back to per-expiration"
    # ALL dates resolved (2023 via fallback, 2024 via multi).
    assert all(not np.isnan(v) for v in values), values
    assert all(e is None for e in errors)


async def test_year_fallback_emits_warning(caplog):
    """Finding 2: a year-chunk failing must surface at WARNING (with year +
    expiration count) plus a summary ratio, so a silent mass-fallback that
    quietly erases the speedup is visible in the logs."""
    reader = _FakeMultiBulkReader(_chains_by_date(), fail_years=frozenset({2023}))
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        await _resolve(reader)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # Per-chunk WARNING names the year and expiration count.
    assert any(
        "FELL BACK to per-expiration for year=2023" in m and "expirations" in m
        for m in warnings
    ), warnings
    # Summary WARNING reports the fallback ratio (1 of the 2 year-chunks).
    assert any("1/2 year-chunk" in m for m in warnings), warnings


async def test_year_failure_isolated_when_fallback_also_fails():
    """When BOTH the year-chunk AND the per-exp fallback fail, only that year's
    dates degrade to data_access_error — the other year still resolves, no 500."""
    reader = _FakeMultiBulkReader(
        _chains_by_date(), fail_years=frozenset({2023}), fail_bulk=True
    )
    values, errors, contracts = await _resolve(reader)
    i23a, i23b = 0, 1  # _D23 dates
    i24a, i24b = 2, 3  # _D24 dates
    # 2023 dates NaN + diagnostic.
    assert np.isnan(values[i23a]) and np.isnan(values[i23b])
    assert errors[i23a] == "data_access_error"
    assert errors[i23b] == "data_access_error"
    # 2024 dates unaffected.
    assert not np.isnan(values[i24a]) and not np.isnan(values[i24b])
    assert errors[i24a] is None and errors[i24b] is None
