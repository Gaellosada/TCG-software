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
    ByMoneyness,
    ByStrike,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
    RollOffset,
)

from tcg.data._sql.options import symbol_delta_rank

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
        expiration_cycle=None,
        delta_pushdown=None,
    ) -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
        years = {exp.year for exp, _ in groups}
        self.multi_calls.append(
            {
                "years": years,
                "n_exp": len(list(groups)),
                "delta_pushdown": delta_pushdown,
            }
        )
        if years & self._fail_years:
            raise RuntimeError("simulated year-chunk dwh failure")
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for exp, dates in groups:
            for d in dates:
                result.setdefault(d, [])
                matched = [
                    (c, r)
                    for c, r in self._chains.get(d, [])
                    if (c.type == type or type == "both")
                    and c.expiration == exp
                    and _cycle_matches(c.expiration_cycle, expiration_cycle)
                ]
                if delta_pushdown is not None:
                    # Simulate the SQL delta rank via the SHARED reference (the
                    # single source of truth for the symbol-granular top-k — no
                    # private copy of the rank; audit_d3 INV-1).
                    target, k = delta_pushdown
                    matched = symbol_delta_rank(matched, target, k)
                result[d].extend(matched)
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


async def _resolve(reader, *, underlying=None, selection=None, hold=False):
    kw: dict = {}
    if hold:
        # HOLD needs the per-date listed-expiration map + a roll-info sink; build
        # a map that lists each date's own target expiration (NearestToTarget(30)
        # snaps each date to its expiration).
        by_date = {
            d: [e] for e, ds in ((_EXP_2023, _D23), (_EXP_2024, _D24)) for d in ds
        }
        kw = {
            "hold_between_rolls": True,
            "hold_roll_info_out": {},
            "available_expirations_by_date": by_date,
        }
    return await resolve_option_stream(
        dates=_DATES,
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=selection
        or ByDelta(target_delta=-0.10, tolerance=0.05, strict=False),
        stream="mid",
        roll_offset=RollOffset(),
        chain_reader=reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=underlying,
        bulk_chain_reader=reader,
        available_expirations=_EXPIRATIONS,
        **kw,
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


async def test_bydelta_engages_pushdown_k8():
    """Wave-8 gate: STORED-delta ByDelta drives every year-chunk with the
    delta-pushdown (target, k=8)."""
    reader = _FakeMultiBulkReader(_chains_by_date())
    await _resolve(reader, selection=ByDelta(target_delta=-0.10, tolerance=0.05))
    assert reader.multi_calls, "fast path must run"
    assert all(c["delta_pushdown"] == (-0.10, 8) for c in reader.multi_calls), (
        reader.multi_calls
    )


async def test_hold_bydelta_does_not_take_pushdown():
    """HOLD is incompatible with a per-(exp,date) top-k (the frozen held contract
    drifts off target delta and falls outside the top-k on non-roll days -> NaN).
    A hold ByDelta leg MUST stay on the full-chain year-chunk path
    (delta_pushdown None) so the whole chain is fetched every bar."""
    reader = _FakeMultiBulkReader(_chains_by_date())
    await _resolve(reader, hold=True)
    assert reader.multi_calls, "fast path must still run for hold (full chain)"
    assert all(c["delta_pushdown"] is None for c in reader.multi_calls), (
        "hold must NOT engage the delta pushdown"
    )


async def test_bystrike_takes_full_chain_multi_not_pushdown():
    """ByStrike ranks on strike, not delta → full-chain multi (delta_pushdown
    None), NOT the pushdown, and NOT the legacy per-expiration path."""
    reader = _FakeMultiBulkReader(_chains_by_date())
    _v, errors, contracts = await _resolve(reader, selection=ByStrike(strike=4300.0))
    assert reader.multi_calls and reader.bulk_calls == []
    assert all(c["delta_pushdown"] is None for c in reader.multi_calls)
    assert all(c is not None and c.strike == 4300.0 for c in contracts)


async def test_bymoneyness_takes_full_chain_multi_not_pushdown():
    """ByMoneyness needs spot, not delta → full-chain multi (delta_pushdown
    None)."""
    reader = _FakeMultiBulkReader(_chains_by_date())

    async def _spot(_contract_doc, _d):
        return 5000.0

    _v, _e, contracts = await _resolve(
        reader,
        underlying=_spot,
        selection=ByMoneyness(target_K_over_S=0.86, tolerance=0.01),
    )
    assert reader.multi_calls and reader.bulk_calls == []
    assert all(c["delta_pushdown"] is None for c in reader.multi_calls)
    # 4300/5000 = 0.86 is the covered rung.
    assert all(c is not None and c.strike == 4300.0 for c in contracts)


async def test_pushdown_topk_picks_same_contract_as_full_chain():
    """PARITY: the pushdown top-k candidates (fake applies the identical rank)
    select the same contract as the full-chain path would — byte-identical."""
    pushdown = _FakeMultiBulkReader(_chains_by_date())  # honors delta_pushdown
    full = _NoMultiBulkReader(_chains_by_date())  # per-exp full chain

    v_push, e_push, c_push = await _resolve(pushdown)
    v_full, e_full, c_full = await _resolve(full)

    assert pushdown.multi_calls and pushdown.multi_calls[0]["delta_pushdown"] == (
        -0.10,
        8,
    )
    np.testing.assert_array_equal(v_push, v_full)
    assert e_push == e_full
    assert [None if c is None else c.contract_id for c in c_push] == [
        None if c is None else c.contract_id for c in c_full
    ]
    assert all(c is not None and c.strike == 4300.0 for c in c_push)


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
