"""Probe-query elimination: the strike-window spot is reproduced via a SYNTHETIC
routing contract, not a full-chain ``query_chain`` probe.

Wave 3b (perf/options-simulation). The old ``_strike_window_for`` fetched a full
single-expiration chain (~275 rows) solely to hand ONE contract to the
underlying-price resolver — which reads only four GROUP-INVARIANT fields
(``collection``, ``root_underlying``, ``underlying_ref``, ``expiration``) and
ignores the row + every per-strike field.  The resolver now builds a synthetic
``OptionContractDoc`` carrying exactly those four fields and calls the SAME
resolver, so the spot — and therefore the strike window and the selected contract
on every date — is byte-identical, at zero probe round-trips.

These tests are dwh-free: synthetic chains + a strike-HONOURING bulk reader + a
field-reading underlying resolver that mimics the production routing (spot keyed
on the contract's era/``expiration``), so a wrong synthetic field would shift the
window and change the selection — making the identity assertions load-bearing.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

import pytest

from tcg.engine.options.maturity.resolver import DefaultMaturityResolver
from tcg.engine.options.maturity.resolver import DefaultMaturityResolver as _DMR
from tcg.engine.options.series.stream_resolver import resolve_option_stream
from tcg.types.options import (
    ByDelta,
    EndOfMonth,
    NearestToTarget,
    OptionContractDoc,
    OptionDailyRow,
)

from _stream_fakes import FakeChainReader, _contract, _row

# ── Scenario: one resolve spanning two eras with very different spot ─────────
_EXP_EARLY = date(2006, 2, 17)
_EXP_LATE = date(2024, 9, 20)
_DATE_EARLY = date(2006, 1, 17)
_DATE_LATE = date(2024, 8, 16)
# Spot keyed on the option's EXPIRATION era (as the real option-on-future resolver
# effectively is — it reads the front future for that expiration).  A synthetic
# contract carrying the WRONG expiration would get the wrong era spot.
_SPOT_BY_EXP = {_EXP_EARLY: 1000.0, _EXP_LATE: 4000.0}
_ROOT_UNDERLYING = "IND_SP_500"

# x = K/S; put delta = -clamp(1.3333*x - 0.70).  -0.10 at x=0.60, -0.50 at x=0.90.
_XS = [0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.10, 1.20, 1.30]


def _put_delta(x: float) -> float:
    return max(-0.999, min(0.0, -(1.3333 * x - 0.70)))


def _chain_for(d: date, exp: date) -> list[tuple[OptionContractDoc, OptionDailyRow]]:
    spot = _SPOT_BY_EXP[exp]
    return [
        (
            _contract(strike=round(x * spot, 4), expiration=exp, type_="P"),
            _row(row_date=d, mid=5.0, iv=0.20, delta=_put_delta(x)),
        )
        for x in _XS
    ]


def _build_chains() -> dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]]:
    return {
        _DATE_EARLY: _chain_for(_DATE_EARLY, _EXP_EARLY),
        _DATE_LATE: _chain_for(_DATE_LATE, _EXP_LATE),
    }


class _RecordingFieldReadingResolver:
    """Underlying-price resolver that reads ONLY the four routing fields the real
    ``resolve_underlying_price`` reads, and records every contract it saw.

    Spot is keyed on the contract's ``expiration`` era; it also asserts the
    contract is NOT crypto/vix routed here (in-scope OPT_SP_500), matching the
    real Branch-3 futures path.  Any per-strike field is deliberately unused.
    """

    def __init__(self) -> None:
        self.seen: list[OptionContractDoc] = []

    async def __call__(self, contract: OptionContractDoc, d: date) -> float | None:
        self.seen.append(contract)
        # Uses collection + expiration (era) only — the group-invariant routing.
        assert contract.collection == "OPT_SP_500"
        assert contract.underlying_ref is None
        return _SPOT_BY_EXP.get(contract.expiration)


class _StrikeFilteringBulkReader:
    """Bulk reader that HONOURS strike_min/strike_max (records every call)."""

    def __init__(self, chains_by_date) -> None:
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
    ):
        self.bulk_calls.append(
            {
                "expiration_min": expiration_min,
                "strike_min": strike_min,
                "strike_max": strike_max,
                "dates": list(dates),
            }
        )
        result: dict[date, list[tuple[OptionContractDoc, OptionDailyRow]]] = {}
        for d in dates:
            filtered = [
                (c, r)
                for (c, r) in self._chains.get(d, [])
                if (c.type == type or type == "both")
                and expiration_min <= c.expiration <= expiration_max
                and (strike_min is None or c.strike >= strike_min)
                and (strike_max is None or c.strike <= strike_max)
            ]
            if filtered:
                result[d] = filtered
        return result


async def _run(target_delta: float, *, with_root_resolver: bool = True):
    chains = _build_chains()
    bulk = _StrikeFilteringBulkReader(chains)
    chain_reader = FakeChainReader(chains)
    ul = _RecordingFieldReadingResolver()
    root_calls: list[str] = []

    async def _root_resolver(coll: str) -> str:
        root_calls.append(coll)
        return _ROOT_UNDERLYING

    values, errors, contracts = await resolve_option_stream(
        dates=[_DATE_EARLY, _DATE_LATE],
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=NearestToTarget(target_dte_days=30),
        selection=ByDelta(target_delta=target_delta, tolerance=0.05, strict=False),
        stream="iv",
        chain_reader=chain_reader,
        maturity_resolver=DefaultMaturityResolver(),
        underlying_price_resolver=ul,
        root_underlying_resolver=_root_resolver if with_root_resolver else None,
        bulk_chain_reader=bulk,
        available_expirations=[_EXP_EARLY, _EXP_LATE],
        # Production NearestToTarget ALWAYS threads the per-date listing map
        # (see ``fetch_nearest_target_expirations_by_date``).  With it present the
        # strike-window existence gate short-circuits (map positively lists ``exp``
        # on the group's first date ⇒ provably ≥1 quoted contract ⇒ no probe query),
        # so ZERO ``query_chain`` calls are issued.
        available_expirations_by_date={
            _DATE_EARLY: [_EXP_EARLY],
            _DATE_LATE: [_EXP_LATE],
        },
    )
    return values, errors, contracts, bulk, chain_reader, ul, root_calls


# ── 1. The probe query is GONE ──────────────────────────────────────────────
async def test_no_probe_query_chain_issued():
    """A ByDelta resolve must issue ZERO ``query_chain`` (probe) calls — Phase A
    uses ``available_expirations`` and Phase B's window now uses a synthetic
    contract, so the per-date chain reader is never touched."""
    *_, chain_reader, _ul, _root = await _run(-0.10)
    assert chain_reader.calls == [], (
        f"probe eliminated → expected 0 query_chain calls, got {chain_reader.calls}"
    )


# ── 2. The resolver receives a SYNTHETIC contract with the 4 routing fields ──
async def test_resolver_receives_synthetic_contract_with_routing_fields():
    *_, bulk, _cr, ul, root_calls = await _run(-0.10)
    # One synthetic contract per expiration group (2 groups here).
    assert len(ul.seen) == 2
    seen_by_exp = {c.expiration: c for c in ul.seen}
    assert set(seen_by_exp) == {_EXP_EARLY, _EXP_LATE}
    for exp, c in seen_by_exp.items():
        assert c.collection == "OPT_SP_500"
        assert c.expiration == exp
        assert c.underlying_ref is None
        assert c.root_underlying == _ROOT_UNDERLYING  # threaded from the getter
        assert c.type == "P"
    # root_symbol getter resolved once (memoised across groups), keyed on collection.
    assert root_calls == ["OPT_SP_500"]


# ── 3. Selection identity across a delta matrix and two eras ─────────────────
@pytest.mark.parametrize(
    "target_delta, x_target",
    [(-0.10, 0.60), (-0.50, 0.90)],
)
async def test_selected_contract_matches_target_delta_per_era(target_delta, x_target):
    """The selected strike per date is the true target-delta strike (era spot ×
    x_target), proving the synthetic-contract spot reproduces the correct
    per-group window that admits it."""
    _values, errors, contracts, bulk, *_ = await _run(target_delta)
    assert errors == [None, None]
    early, late = contracts
    assert early is not None and late is not None
    assert early.strike == pytest.approx(x_target * _SPOT_BY_EXP[_EXP_EARLY])
    assert late.strike == pytest.approx(x_target * _SPOT_BY_EXP[_EXP_LATE])
    # Per-group window tracked each era's spot (not a single global band).
    early_call = next(c for c in bulk.bulk_calls if c["expiration_min"] == _EXP_EARLY)
    late_call = next(c for c in bulk.bulk_calls if c["expiration_min"] == _EXP_LATE)
    assert early_call["strike_min"] == pytest.approx(0.40 * 1000.0)
    assert late_call["strike_min"] == pytest.approx(0.40 * 4000.0)


# ── 4. Field-usage identity: synthetic ≡ any real group contract ─────────────
async def test_field_reading_resolver_identical_for_synthetic_and_real_contract():
    """A resolver that reads only {collection, root_underlying, underlying_ref,
    expiration} yields an IDENTICAL result for the synthesised routing contract and
    for a real full chain contract sharing those four fields but differing in every
    per-strike field — the byte-identity foundation of the probe removal."""

    async def four_field_resolver(contract: OptionContractDoc, d: date):
        # Mirrors resolve_underlying_price's routing surface.
        return (
            contract.collection,
            contract.root_underlying,
            contract.underlying_ref,
            contract.expiration,
        )

    real = _contract(strike=1234.0, expiration=_EXP_LATE, type_="P")  # rich per-strike
    synthetic = OptionContractDoc(
        collection=real.collection,
        contract_id="",
        root_underlying=real.root_underlying,
        underlying_ref=None,  # SQL reader forces None on BOTH
        underlying_symbol=None,
        expiration=real.expiration,
        expiration_cycle="",
        strike=0.0,
        type=real.type,
        contract_size=None,
        currency=None,
        provider="UNKNOWN",
        strike_factor_verified=False,
    )
    # Real contract's underlying_ref is non-None in the fixture, but the SQL reader
    # forces None in production — so compare against the production-shaped real.
    real_prod = OptionContractDoc(**{**real.__dict__, "underlying_ref": None})
    assert await four_field_resolver(
        synthetic, _DATE_LATE
    ) == await four_field_resolver(real_prod, _DATE_LATE)


# ── 5. None root-resolver (legacy/unit callers) still resolves via "" ────────
async def test_none_root_resolver_defaults_to_empty_and_still_selects():
    """A ``None`` root_underlying_resolver (legacy callers / unit tests) must not
    crash — root_underlying defaults to '' (correct routing for OPT_SP_500) and the
    selection is unchanged."""
    _values, errors, contracts, _bulk, _cr, ul, _root = await _run(
        -0.10, with_root_resolver=False
    )
    assert errors == [None, None]
    assert all(c.root_underlying == "" for c in ul.seen)
    assert contracts[1] is not None
    assert contracts[1].strike == pytest.approx(0.60 * _SPOT_BY_EXP[_EXP_LATE])


# ── 6. Empty repr_date ⇒ FULL CHAIN (old semantics), NOT a narrowed band ─────
# Regression for Wave-4b blocking finding B1.  When the group's FIRST date
# (``repr_date``) has NO price-quoted contract for the resolved expiration, the
# OLD ``_strike_window_for`` probe returned 0 rows → ``(None, None)`` = full chain
# for the WHOLE group.  The c4995af probe-removal always synthesised a spot from
# the futures close (which exists regardless of option quoting) and NARROWED to
# ``[0.40, 1.30]·spot`` — so a later in-group date whose true target strike falls
# OUTSIDE that band selects a DIFFERENT contract → byte-identity break.  Reachable
# via arithmetic maturities (EndOfMonth) which carry NO by-date listing map, so the
# existence gate must fall back to the cheap ``LIMIT 1`` probe.
_EOM_EXP = date(2023, 4, 21)
_EOM_D0 = date(2023, 3, 6)  # repr_date — NO quoted contract for _EOM_EXP
_EOM_D1 = date(2023, 3, 20)  # later date in the SAME EndOfMonth group — full chain
# (strike, put-delta) — the -0.50 target strike (2000) sits OUTSIDE the
# [0.40,1.30]·spot band that a repr-date spot of 1000 would produce ([400,1300]).
_EOM_CHAIN_D1 = [
    (500.0, -0.05),
    (1000.0, -0.20),
    (1300.0, -0.35),  # deepest strike admitted by the narrowed [400,1300] band
    (2000.0, -0.50),  # TRUE -0.50 target — EXCLUDED by the narrowed band
    (3000.0, -0.80),
]


async def test_empty_repr_date_falls_back_to_full_chain_eom():
    """EndOfMonth group whose repr_date is unquoted must select from the FULL
    chain (old behaviour), not a spot-narrowed band, so a later date's deep target
    strike outside the band is still admitted."""
    chains = {
        _EOM_D1: [
            (
                _contract(strike=k, expiration=_EOM_EXP, type_="P"),
                _row(row_date=_EOM_D1, mid=5.0, iv=0.20, delta=d),
            )
            for k, d in _EOM_CHAIN_D1
        ]
        # _EOM_D0 intentionally absent → 0 quoted contracts for _EOM_EXP.
    }
    bulk = _StrikeFilteringBulkReader(chains)
    chain_reader = FakeChainReader(chains)

    async def _spot_1000(contract: OptionContractDoc, d: date) -> float:
        return 1000.0  # → narrowed band would be [400, 1300]

    values, errors, contracts = await resolve_option_stream(
        dates=[_EOM_D0, _EOM_D1],
        collection="OPT_SP_500",
        option_type="P",
        cycle=None,
        maturity=EndOfMonth(),
        selection=ByDelta(target_delta=-0.50, tolerance=0.05, strict=False),
        stream="mid",
        chain_reader=chain_reader,
        maturity_resolver=_DMR(),
        underlying_price_resolver=_spot_1000,
        bulk_chain_reader=bulk,
        available_expirations=[_EOM_EXP],
        # EndOfMonth carries NO by-date map (arithmetic maturity) → the existence
        # gate must use the cheap LIMIT-1 probe.
    )
    # The later date selects the TRUE -0.50 strike (2000), only reachable from the
    # full chain.  On the buggy narrowed path it would be 1300 (nearest in-band).
    # (``errors[1]`` carries the EndOfMonth ``snapped_to:`` success-side note — the
    # value/contract array is the source of truth for selection, not that note.)
    assert contracts[1] is not None
    assert contracts[1].strike == pytest.approx(2000.0), (
        "empty repr_date must fall back to the FULL chain, not a narrowed band"
    )
    # The cheap existence probe (LIMIT 1) was issued at the unquoted repr_date for
    # exactly the resolved expiration — NOT a full-chain fetch.
    probes = [
        c
        for c in chain_reader.calls
        if c["date"] == _EOM_D0 and c["expiration_min"] == _EOM_EXP
    ]
    assert probes, (
        f"expected a LIMIT-1 existence probe at {_EOM_D0}, got {chain_reader.calls}"
    )
    assert all(c.get("limit") == 1 for c in probes), (
        f"existence probe must be row-limited (LIMIT 1), got {probes}"
    )
